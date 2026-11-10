#!/usr/bin/env python3
"""Kill the playbook right after the confirmed commit and time the device-side rollback.

    ~/nokia_lab/cutover-venv/bin/python bin/kill_test.py persist     # default playbook (<persist>)
    ~/nokia_lab/cutover-venv/bin/python bin/kill_test.py nopersist   # -e persist_commit=false

Change used: phase=drain on uplink A (IS-IS metric 10 -> 1000 on csr1 and agg-01), confirm-timeout 60 s,
with the playbook told to sit for 600 s after the commit so the kill lands between commit and confirm.
Independent NETCONF sessions poll *running* on both nodes every 0.25 s and record when the metric is
back to 10. Afterwards it reports leftover ansible-connection processes, candidate state and lock state.
"""
import os, re, signal, subprocess, sys, time
from pathlib import Path
from ncclient import manager

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cutover_test as ct  # noqa: E402  (traffic + reporting helpers)

MODE = sys.argv[1] if len(sys.argv) > 1 else "persist"
TIMEOUT = 60
NODES = {"csr1": "172.30.40.11", "agg-01": "172.30.40.21"}
NS_NI = "urn:nokia.com:srlinux:net-inst:network-instance"
NS_ISIS = "urn:nokia.com:srlinux:isis:isis"
FLT = (f'<network-instance xmlns="{NS_NI}"><name>default</name><protocols><isis xmlns="{NS_ISIS}"><instance>'
       f'<name>i1</name><interface><interface-name>ethernet-1/1.100</interface-name><level><level-number>2</level-number>'
       f'<metric/></level></interface></instance></isis></protocols></network-instance>')


def conn(ip):
    return manager.connect(host=ip, port=830, username="ansible", password="Ansible123!", hostkey_verify=False,
                           look_for_keys=False, allow_agent=False, timeout=30)


def metric(m, src="running"):
    e = m.get_config(source=src, filter=("subtree", FLT)).data_ele.find(f".//{{{NS_ISIS}}}metric")
    return None if e is None else int(e.text)


def main():
    name = f"kill-{MODE}"
    log = open(ct.RES / f"{name}.ansible.log", "w")
    ct.reset(log)
    watch = {n: conn(ip) for n, ip in NODES.items()}
    print("before:", {n: metric(m) for n, m in watch.items()})

    ct.ping_start()
    extra = f"-e confirm_timeout={TIMEOUT} -e pause_after_commit=600" + (" -e persist_commit=false" if MODE == "nopersist" else "")
    cmd = (f"cd {ct.ANS} && exec {ct.VENV}/ansible-playbook uplink_cutover.yml -e link=a -e phase=drain "
           f"-e apply_change=true {extra}")
    env = {**os.environ, "PATH": f"{ct.VENV}:{os.environ['PATH']}", "PYTHONUNBUFFERED": "1", "ANSIBLE_FORCE_COLOR": "0"}
    t_start = time.time()
    p = subprocess.Popen(cmd, shell=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    for line in p.stdout:
        log.write(line)
        if "TASK [7. Pause after commit" in line:
            break
    t_commit = time.time()   # both hosts have committed: the pause task only starts after task 7 on all hosts
    seen = {n: metric(m) for n, m in watch.items()}
    print(f"committed (playbook reached the pause) {t_commit - t_start:.1f}s after start; running now {seen}")
    time.sleep(1)
    os.kill(p.pid, signal.SIGKILL)
    t_kill = time.time()
    p.wait()
    log.write(f"\n### SIGKILL sent to ansible-playbook pid {p.pid} at +{t_kill - t_commit:.1f}s after commit\n")
    print(f"SIGKILL ansible-playbook pid {p.pid} at {t_kill - t_commit:.1f}s after commit")
    conns = ct.sh("pgrep -af '[a]nsible_connection_cli_stub' || true").stdout.strip()
    orphans = ct.sh("pgrep -f 'ansible-playbook uplink_cutover[.]yml .*pause_after_commit' || true").stdout.split()
    print("connection daemons alive after the kill:", len(conns.splitlines()), "| orphaned playbook workers:", len(orphans))

    reverted = {}
    conn_gone = None
    while len(reverted) < len(watch) and time.time() - t_commit < TIMEOUT + 90:
        for n, m in watch.items():
            if n not in reverted and metric(m) == 10:
                reverted[n] = time.time() - t_commit
        if conn_gone is None and not ct.sh("pgrep -f '[a]nsible_connection_cli_stub' || true").stdout.strip():
            conn_gone = time.time() - t_commit
        time.sleep(0.25)
    if conn_gone is None and not ct.sh("pgrep -f '[a]nsible_connection_cli_stub' || true").stdout.strip():
        conn_gone = time.time() - t_commit
    flows = ct.ping_stop()
    # clean up the orphaned forked workers SIGKILL left behind (they sit in the 600 s pause)
    ct.sh("pkill -f 'ansible-playbook uplink_cutover[.]yml .*pause_after_commit'; pkill -f AnsiballZ_wait_fo[r]", check=False)

    after = {}
    for n, m in watch.items():
        after[n] = {"running": metric(m), "candidate": metric(m, "candidate")}
        try:
            m.lock("candidate"); m.unlock("candidate"); after[n]["lock"] = "lock OK"
        except Exception as e:
            after[n]["lock"] = "lock refused: " + str(e).strip().split("\n")[0][:80]
        m.close_session()

    # what does the next normal run do?
    nxt = ct.playbook("a", "drain", log=log)
    nxt_msg = "next run (no discard flag): rc=%d" % nxt["rc"]
    if nxt["rc"]:
        tail = open(ct.RES / f"{name}.ansible.log").read()
        m = re.search(r"Lock refused:[^\"\n]*", tail)
        nxt_msg += " - " + (m.group(0)[:160] if m else "failed, see log")

    notes = [
        f"mode: {MODE}  ({'-e persist_commit=false' if MODE == 'nopersist' else 'default playbook, <persist>'}), confirm-timeout {TIMEOUT}s",
        f"change: phase=drain link=a (metric 10 -> 1000 on csr1 + agg-01); playbook SIGKILLed {t_kill - t_commit:.1f}s after commit",
        "time from commit until running metric was back to 10 (polled every 0.25 s from separate NETCONF sessions):",
        *[f"   {n}: {reverted[n]:.1f}s" if n in reverted else f"   {n}: NOT reverted within {TIMEOUT + 90}s" for n in watch],
        f"persistent connection daemons (ansible_connection_cli_stub, they hold the NETCONF sessions) alive right after kill: "
        f"{len(conns.splitlines())}; " + (f"all gone at {conn_gone:.1f}s after commit" if conn_gone is not None else "still running at end"),
        f"orphaned ansible-playbook worker processes left by SIGKILL of the main pid: {len(orphans)} (killed by the test afterwards)",
        f"after rollback: {after}",
        nxt_msg,
    ]
    ct.report(name, flows, [{"step": "drain a (killed after commit)", "rc": -9, "start": t_start,
                             "end": time.time(), "secs": round(time.time() - t_start, 1), "recap": []}],
              notes=notes, paths=ct.load_paths())


if __name__ == "__main__":
    main()
