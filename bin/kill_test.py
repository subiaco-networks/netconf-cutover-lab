#!/usr/bin/env python3
"""Kill the playbook's whole process group right after the swing commit and time the device-side rollback.

    ~/nokia_lab/cutover-venv/bin/python bin/kill_test.py            # default playbook (<persist>)
    ~/nokia_lab/cutover-venv/bin/python bin/kill_test.py nopersist  # -e persist_commit=false

Setup: baseline, traffic on, csr1 turnup (confirmed normally). Then swing on csr1 with confirm-timeout 60 s and the
playbook told to sit for 600 s after the commit, so the kill lands between commit and confirm. SIGKILL goes to the
playbook's process group (the playbook, its forked workers, and anything else in the group).
Independent NETCONF sessions poll *running* on csr1, agg-01 and agg-02 every 0.25 s for the new circuit's IS-IS
metric: 10 = swing in place, 1000 = rolled back to the turnup state.
"""
import os, re, signal, subprocess, sys, time
from pathlib import Path
from ncclient import manager

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cutover_test as ct  # noqa: E402  (traffic + reporting helpers)

MODE = sys.argv[1] if len(sys.argv) > 1 else "persist"
TIMEOUT = 60
WATCH = {"csr1": ("172.30.40.11", "ethernet-1/11.111"), "agg-01": ("172.30.40.21", "ethernet-1/1.111"),
         "agg-02": ("172.30.40.22", "ethernet-1/1.112")}
NS_NI = "urn:nokia.com:srlinux:net-inst:network-instance"
NS_ISIS = "urn:nokia.com:srlinux:isis:isis"


def flt(sub):
    return (f'<network-instance xmlns="{NS_NI}"><name>default</name><protocols><isis xmlns="{NS_ISIS}"><instance>'
            f'<name>i1</name><interface><interface-name>{sub}</interface-name><level><level-number>2</level-number>'
            f'<metric/></level></interface></instance></isis></protocols></network-instance>')


def conn(ip):
    return manager.connect(host=ip, port=830, username="ansible", password="Ansible123!", hostkey_verify=False,
                           look_for_keys=False, allow_agent=False, timeout=30)


def metric(m, sub, src="running"):
    e = m.get_config(source=src, filter=("subtree", flt(sub))).data_ele.find(f".//{{{NS_ISIS}}}metric")
    return None if e is None else int(e.text)


def daemons():
    return ct.sh("pgrep -f '[a]nsible_connection_cli_stub' || true").stdout.split()


def main():
    name = f"kill-swing-{MODE}"
    log = open(ct.RES / f"{name}.ansible.log", "w")
    ct.reset(log)
    ct.ping_start()
    steps, snaps = [], [ct.circuit_rates("start (baseline)")]
    steps.append(ct.playbook("csr1", "turnup", log=log))
    snaps.append(ct.circuit_rates("csr1 turnup"))
    watch = {n: (conn(ip), sub) for n, (ip, sub) in WATCH.items()}
    print("before swing:", {n: metric(m, s) for n, (m, s) in watch.items()})

    extra = f"-e confirm_timeout={TIMEOUT} -e pause_after_commit=600" + (" -e persist_commit=false" if MODE == "nopersist" else "")
    cmd = (f"cd {ct.ANS} && exec {ct.VENV}/ansible-playbook uplink_cutover.yml -e site=csr1 -e phase=swing "
           f"-e apply_change=true {extra}")
    env = {**os.environ, "PATH": f"{ct.VENV}:{os.environ['PATH']}", "PYTHONUNBUFFERED": "1", "ANSIBLE_FORCE_COLOR": "0"}
    t_start = time.time()
    p = subprocess.Popen(cmd, shell=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                         start_new_session=True)
    for line in p.stdout:
        log.write(line)
        if "TASK [7. Pause after commit" in line:
            break
    t_commit = time.time()   # every host has committed: the pause task only starts after task 7 on all hosts
    seen = {n: metric(m, s) for n, (m, s) in watch.items()}
    print(f"committed {t_commit - t_start:.1f}s after start; running now {seen}")
    time.sleep(1)
    before_kill = daemons()
    os.killpg(p.pid, signal.SIGKILL)
    t_kill = time.time()
    p.wait()
    time.sleep(0.5)
    after_kill = daemons()
    survivors = ct.sh("pgrep -f 'ansible-playbook uplink_cutover[.]yml .*pause_after_commit' || true").stdout.split()
    log.write(f"\n### SIGKILL sent to process group {p.pid} at +{t_kill - t_commit:.1f}s after commit\n")
    print(f"killpg at {t_kill - t_commit:.1f}s; connection daemons before {len(before_kill)}, after {len(after_kill)}; "
          f"playbook processes left {len(survivors)}")
    t_kill_step = {"step": "csr1 swing (process group SIGKILLed after commit)", "rc": -9, "start": t_start,
                   "end": t_kill, "secs": round(t_kill - t_start, 1), "why": []}

    reverted, conn_gone = {}, None
    while len(reverted) < len(watch) and time.time() - t_commit < TIMEOUT + 90:
        for n, (m, s) in watch.items():
            if n not in reverted and metric(m, s) == 1000:
                reverted[n] = time.time() - t_commit
        if conn_gone is None and not daemons():
            conn_gone = time.time() - t_commit
        time.sleep(0.25)
    steps += [t_kill_step, {"step": "rollback wait", "rc": 0, "start": t_kill, "end": time.time(),
                            "secs": round(time.time() - t_kill, 1), "why": []}]
    snaps.append(ct.circuit_rates("rollback"))
    flows = ct.ping_stop()
    ct.sh("pkill -f 'ansible-playbook uplink_cutover[.]yml .*pause_after_commit'; pkill -f AnsiballZ_wait_fo[r]", check=False)

    after = {}
    for n, (m, s) in watch.items():
        after[n] = {"running": metric(m, s), "candidate": metric(m, s, "candidate")}
        try:
            m.lock("candidate"); m.unlock("candidate"); after[n]["lock"] = "lock OK"
        except Exception as e:
            after[n]["lock"] = "lock refused: " + str(e).strip().split("\n")[0][:80]
        m.close_session()

    nxt = ct.playbook("csr1", "swing", log=log)
    nxt_msg = f"next normal swing run (no discard flag): rc={nxt['rc']}" + (f" - {nxt['why']}" if nxt["why"] else "")
    if nxt["rc"]:
        m = re.search(r"Lock refused:[^\"\n]*", open(ct.RES / f"{name}.ansible.log").read())
        nxt_msg += " - " + (m.group(0)[:160] if m else "")

    ct.report(name, flows, steps, snaps, paths=ct.load_paths(), notes=[
        f"mode: {MODE} ({'-e persist_commit=false' if MODE == 'nopersist' else 'default playbook, <persist>'}), confirm-timeout {TIMEOUT}s",
        f"SIGKILL to the playbook's process group {t_kill - t_commit:.1f}s after the swing commit",
        f"ansible_connection_cli_stub daemons: {len(before_kill)} before the kill, {len(after_kill)} right after; "
        + (f"none left at {conn_gone:.1f}s after commit" if conn_gone is not None else "still running at end"),
        f"ansible-playbook processes left after the group kill: {len(survivors)}",
        "time from the swing commit until running was back to the turnup state (new circuit metric 1000):",
        *[f"   {n}: {reverted[n]:.1f}s" if n in reverted else f"   {n}: NOT reverted within {TIMEOUT + 90}s" for n in watch],
        f"after rollback: {after}", nxt_msg])


if __name__ == "__main__":
    main()
