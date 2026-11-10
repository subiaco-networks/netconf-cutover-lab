#!/usr/bin/env python3
"""Probe SR Linux NETCONF confirmed-commit semantics with raw ncclient (no Ansible).

T1  confirmed commit, session stays open, never confirmed  -> time until running reverts
T2  confirmed commit, then <close-session>                 -> does it survive or roll back, and when
T3  confirmed commit, then TCP/SSH dropped without close   -> same question (what a killed process does)
T4  confirmed commit with <persist>, then session closed   -> survives? confirm from a new session with persist-id
Uses a harmless leaf: interface ethernet-1/12 description on csr1.
"""
import sys, time, uuid
from ncclient import manager
from ncclient.xml_ import to_ele
HOST = sys.argv[1] if len(sys.argv) > 1 else "172.30.40.11"
NS = "urn:nokia.com:srlinux:chassis:interfaces"

def conn():
    return manager.connect(host=HOST, port=830, username="ansible", password="Ansible123!",
                           hostkey_verify=False, look_for_keys=False, allow_agent=False, timeout=30)

def cfg(desc):
    op = 'nc:operation="remove"' if desc is None else ""
    body = "" if desc is None else desc
    # <config> must be in the NETCONF base namespace or SR Linux answers "Unknown namespace"
    return (f'<nc:config xmlns:nc="urn:ietf:params:xml:ns:netconf:base:1.0"><interface xmlns="{NS}">'
            f'<name>ethernet-1/12</name><description {op}>{body}</description></interface></nc:config>')

def candidate_desc(m):
    r = m.get_config("candidate", filter=("subtree", f'<interface xmlns="{NS}"><name>ethernet-1/12</name><description/></interface>'))
    e = r.data_ele.find(f".//{{{NS}}}description")
    return None if e is None else e.text

def running_desc(m):
    r = m.get_config("running", filter=("subtree", f'<interface xmlns="{NS}"><name>ethernet-1/12</name><description/></interface>'))
    e = r.data_ele.find(f".//{{{NS}}}description")
    return None if e is None else e.text

def wait_revert(watcher, marker, limit):
    t0 = time.monotonic()
    while time.monotonic() - t0 < limit:
        if running_desc(watcher) != marker:
            return time.monotonic() - t0
        time.sleep(0.2)
    return None

def stage(m, marker, timeout, persist=None):
    # A session-close rollback leaves the rolled-back edit in the shared candidate, and
    # <lock> then fails with "candidate has been modified" - so start from a clean candidate.
    m.discard_changes()
    m.lock("candidate")
    m.edit_config(target="candidate", config=cfg(marker))
    m.validate("candidate")
    t = time.monotonic()
    m.commit(confirmed=True, timeout=str(timeout), persist=persist)
    return t

watcher = conn()
# clean slate
with conn() as m:
    m.edit_config(target="candidate", config=cfg(None)); m.commit()
print("baseline running description:", running_desc(watcher))

# T1
m = conn(); mk = "T1-" + uuid.uuid4().hex[:6]
t = stage(m, mk, 20)
print(f"T1 committed confirm-timeout=20s; running now = {running_desc(watcher)!r}")
dt = wait_revert(watcher, mk, 60)
print(f"T1 session open, unconfirmed: running reverted {dt:.1f}s after commit" if dt is not None else "T1 NOT reverted within 60s")
try: m.unlock("candidate"); m.close_session()
except Exception as e: print("   (T1 cleanup:", type(e).__name__, str(e).strip()[:80], ")")

# T2
m = conn(); mk = "T2-" + uuid.uuid4().hex[:6]
stage(m, mk, 60)
m.close_session()
dt = wait_revert(watcher, mk, 90)
print(f"T2 <close-session> after confirmed commit (timeout 60s): running reverted after {dt:.1f}s" if dt is not None else "T2 survived close-session for 90s (not reverted)")
print(f"   after the rollback: running={running_desc(watcher)!r} candidate={candidate_desc(watcher)!r} (stale edit left in candidate?)")

# T3
m = conn(); mk = "T3-" + uuid.uuid4().hex[:6]
stage(m, mk, 60)
m._session._transport.close()          # drop SSH without <close-session>, like a SIGKILL'd client
dt = wait_revert(watcher, mk, 90)
print(f"T3 transport dropped after confirmed commit (timeout 60s): running reverted after {dt:.1f}s" if dt is not None else "T3 survived dropped transport for 90s")

# T4
m = conn(); mk = "T4-" + uuid.uuid4().hex[:6]; pid = "cut-" + uuid.uuid4().hex[:6]
try:
    stage(m, mk, 60, persist=pid)
    m.unlock("candidate"); m.close_session()
    time.sleep(3)
    print(f"T4 persist={pid}, session closed; 3s later running = {running_desc(watcher)!r}")
    with conn() as m2:
        m2.commit(persist_id=pid)
    time.sleep(65)
    print(f"T4 confirmed from a NEW session with persist-id; 65s later running = {running_desc(watcher)!r}")
except Exception as e:
    print("T4 persist not supported / failed:", type(e).__name__, str(e).strip()[:200])

with conn() as m:
    m.edit_config(target="candidate", config=cfg(None)); m.commit()
watcher.close_session()
