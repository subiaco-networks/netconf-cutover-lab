#!/usr/bin/env python3
"""Run the cutover test scenarios with continuous traffic and record packet loss.

Run on the lab server with the venv python:
    ~/nokia_lab/cutover-venv/bin/python bin/cutover_test.py <scenario> [...]
Scenarios: paths  full  nodrain  rescue-wrong-port  rescue-far-end-shut  kill  reset

Traffic: 8 ping flows h1 (10.10.1.2-9) -> h2 (10.20.1.2), 10 ms interval each (800 pps total).
Using 8 source addresses spreads the flows over both ECMP uplinks in both directions.
Loss per flow = transmitted - received (iputils summary). Each lost packet is placed in time
from its sequence number and attributed to the playbook step that was running then.
"""
import json, os, re, signal, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ANS = HERE / "ansible"
RES = HERE / "results"
VENV = Path.home() / "nokia_lab/cutover-venv/bin"
SRCS = [f"10.10.1.{i}" for i in range(2, 10)]
DST = "10.20.1.2"
INTERVAL = 0.01
H1 = "clab-cutover-h1"


def sh(cmd, check=True, **kw):
    return subprocess.run(cmd, shell=True, check=check, text=True, capture_output=True, **kw)


def now():
    return time.time()


# ------------------------------------------------------------------ traffic
def ping_start():
    sh(f"docker exec {H1} sh -c 'pkill -INT ping; rm -f /tmp/p*.log'", check=False)
    for s in SRCS:
        n = s.split(".")[-1]
        sh(f"docker exec -d {H1} sh -c 'exec ping -D -i {INTERVAL} -W 1 -I {s} {DST} > /tmp/p{n}.log 2>&1'")
    time.sleep(2)
    return now()


def ping_stop():
    time.sleep(3)  # let trailing replies arrive
    sh(f"docker exec {H1} pkill -INT ping", check=False)
    time.sleep(1.5)
    flows = {}
    for s in SRCS:
        log = sh(f"docker exec {H1} cat /tmp/p{s.split('.')[-1]}.log").stdout
        m = re.search(r"(\d+) packets transmitted, (\d+) received", log)
        tx, rx = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        got = {}
        for ts, seq in re.findall(r"\[(\d+\.\d+)\] \d+ bytes from .*icmp_seq=(\d+)", log):
            got[int(seq)] = float(ts)
        # place each lost packet in time by interpolating between the replies around it
        # (iputils does not hold the 10 ms interval exactly, so do not assume it)
        seqs = sorted(got)
        missing, longest = [], 0.0
        for a, b in zip(seqs, seqs[1:]):
            if b - a > 1:
                step = (got[b] - got[a]) / (b - a)
                missing += [got[a] + step * k for k in range(1, b - a)]
                longest = max(longest, got[b] - got[a])
        # packets sent after the last reply were still in flight when ping got SIGINT: iputils counts them
        # as lost, but they are a stop artefact, not loss. Report them separately and do not count them.
        inflight = tx - seqs[-1] if seqs else 0
        dur = (got[seqs[-1]] - got[seqs[0]]) if len(seqs) > 1 else 0
        flows[s] = {"tx": tx - inflight, "rx": rx, "lost": len(missing), "inflight_at_stop": inflight,
                    "longest_gap_ms": round(longest * 1000) if longest else 0,
                    "pps": round(rx / dur, 1) if dur else 0, "secs": round(dur, 1), "lost_at": missing}
    return flows


# ------------------------------------------------------------------ ansible
def playbook(link, phase, *extra, log=None, timeout=600):
    args = " ".join(["-e", f"link={link}", "-e", f"phase={phase}", "-e", "apply_change=true", *extra])
    cmd = f"cd {ANS} && PYTHONUNBUFFERED=1 {VENV}/ansible-playbook uplink_cutover.yml {args}"
    t0 = now()
    p = subprocess.run(cmd, shell=True, text=True, capture_output=True, timeout=timeout,
                       env={**os.environ, "PATH": f"{VENV}:{os.environ['PATH']}", "ANSIBLE_FORCE_COLOR": "0"})
    t1 = now()
    if log is not None:
        log.write(f"\n===== ansible-playbook {args}  (rc={p.returncode}, {t1 - t0:.1f}s)\n{p.stdout}{p.stderr}")
    recap = re.findall(r"^(\S+)\s+: ok=.*$", p.stdout, re.M)
    return {"step": f"{phase} {link} {' '.join(extra)}".strip(), "rc": p.returncode, "start": t0, "end": t1,
            "secs": round(t1 - t0, 1), "recap": re.findall(r"^\S+\s+: ok=.*$", p.stdout, re.M)}


def srl(node, cmd):
    return sh(f"docker exec clab-cutover-{node} sr_cli -d \"{cmd}\"", check=False).stdout


def adjacencies(node="csr1"):
    out = srl(node, "show network-instance default protocols isis adjacency")
    rows = [[c.strip() for c in l.strip().strip("|").split("|")] for l in out.splitlines() if l.startswith("| ethernet")]
    return [(r[0], r[5]) for r in rows if len(r) > 5]


# ------------------------------------------------------------------ reporting
def attribute(flows, steps):
    per_step = {s["step"]: 0 for s in steps}
    outside = 0
    for f in flows.values():
        for t in f["lost_at"]:
            for s in steps:
                if s["start"] - 0.5 <= t <= s["end"] + 0.5:
                    per_step[s["step"]] += 1
                    break
            else:
                outside += 1
    return per_step, outside


def report(name, flows, steps, notes=(), paths=None):
    tx = sum(f["tx"] for f in flows.values()); lost = sum(f["lost"] for f in flows.values())
    per_step, outside = attribute(flows, steps)
    lines = [f"# {name}", f"run at {time.strftime('%Y-%m-%d %H:%M:%S %Z')}",
             f"TOTAL: {lost} lost of {tx} sent ({100 * lost / tx if tx else 0:.4f}%) across {len(flows)} flows, "
             f"ping -i {INTERVAL} each (measured {sum(f.get('pps', 0) for f in flows.values()) or float('nan'):.0f} pps aggregate)",
             "a 'gap' is the time between the replies either side of the lost packets;",
             "packets still in flight when ping is stopped are excluded from tx and lost", "", "## steps"]
    for s in steps:
        lines.append(f"  {s['step']:<55} rc={s['rc']}  {s['secs']:>6}s  lost={per_step[s['step']]}")
    if outside:
        lines.append(f"  (lost outside any step window: {outside})")
    lines += ["", "## flows"]
    for src, f in flows.items():
        p = paths.get(src, {}) if paths else {}
        lines.append(f"  {src} -> {DST}: tx={f['tx']} rx={f['rx']} lost={f['lost']} longest_gap={f['longest_gap_ms']}ms ({f.get('pps', '?')} pps over {f.get('secs', '?')}s, {f.get('inflight_at_stop', 0)} in flight at stop - not counted)"
                     + (f"   path fwd=uplink-{p.get('fwd','?')} rev=uplink-{p.get('rev','?')}" if p else ""))
    lines += ["", *notes]
    text = "\n".join(lines) + "\n"
    (RES / f"{name}.txt").write_text(text)
    (RES / f"{name}.json").write_text(json.dumps({"flows": flows, "steps": steps}, indent=1))
    print(text)
    return lost


# ------------------------------------------------------------------ path mapping
def counters():
    c = {}
    for node, intf, sub in [("csr1", "ethernet-1/1", 100), ("csr1", "ethernet-1/11", 100),
                            ("csr1", "ethernet-1/2", 200), ("csr1", "ethernet-1/12", 200),
                            ("agg-01", "ethernet-1/1", 100), ("agg-01", "ethernet-1/11", 100),
                            ("agg-02", "ethernet-1/1", 200), ("agg-02", "ethernet-1/11", 200)]:
        out = srl(node, f"info from state interface {intf} subinterface {sub} statistics out-packets")
        m = re.search(r"out-packets (\d+)", out)
        c[(node, intf)] = int(m.group(1)) if m else 0
    return c


def paths():
    """Which uplink each flow takes, forward (csr1 egress) and reverse (agg egress toward csr1)."""
    res = {}
    for s in SRCS:
        a = counters()
        sh(f"docker exec {H1} ping -q -i 0.002 -c 500 -I {s} {DST}", check=False)
        time.sleep(4)
        b = counters()
        d = {k: b[k] - a[k] for k in a}
        fwd_a = d[("csr1", "ethernet-1/1")] + d[("csr1", "ethernet-1/11")]
        fwd_b = d[("csr1", "ethernet-1/2")] + d[("csr1", "ethernet-1/12")]
        rev_a = d[("agg-01", "ethernet-1/1")] + d[("agg-01", "ethernet-1/11")]
        rev_b = d[("agg-02", "ethernet-1/1")] + d[("agg-02", "ethernet-1/11")]
        res[s] = {"fwd": "a" if fwd_a > fwd_b else "b", "rev": "a" if rev_a > rev_b else "b",
                  "counts": [fwd_a, fwd_b, rev_a, rev_b]}
    (RES / "paths.json").write_text(json.dumps(res, indent=1))
    return res


def load_paths():
    p = RES / "paths.json"
    return json.loads(p.read_text()) if p.exists() else None


# ------------------------------------------------------------------ scenarios
def reset(log):
    """Back to the original ports at normal metric on both links (no traffic measured)."""
    for link in "ab":
        r = playbook(link, "baseline", "-e", "discard_stale_candidate=true", log=log)
        if r["rc"]:
            sys.exit(f"reset of link {link} failed, see log")
    time.sleep(15)
    print("reset done; csr1 adjacencies:", adjacencies())


def scenario(name):
    RES.mkdir(exist_ok=True)
    log = open(RES / f"{name}.ansible.log", "w")
    pmap = load_paths()
    if name == "paths":
        r = paths(); print(json.dumps(r, indent=1)); return
    if name == "reset":
        reset(log); return

    if name == "full":
        reset(log)
        steps = []
        ping_start()
        for link in "ab":
            for phase in ("drain", "cut", "restore"):
                steps.append(playbook(link, phase, log=log))
                time.sleep(5)   # let IS-IS settle and traffic rebalance between steps
        flows = ping_stop()
        return report(name, flows, steps, paths=pmap,
                      notes=[f"final csr1 adjacencies: {adjacencies()}"])

    if name.startswith("nodrain"):
        reset(log)
        steps = []
        ping_start()
        steps.append(playbook("a", "cut", "-e", "skip_drain=true", log=log))
        time.sleep(5)
        flows = ping_stop()
        return report(name, flows, steps, paths=pmap,
                      notes=["cut of uplink A from ethernet-1/1 to ethernet-1/11 at normal metric, NO drain first",
                             f"final csr1 adjacencies: {adjacencies()}"])

    if name.startswith("drained-cut"):
        # same single move as nodrain, but drained first; only the cut itself is the comparable step
        reset(log)
        steps = []
        ping_start()
        steps.append(playbook("a", "drain", log=log)); time.sleep(5)
        steps.append(playbook("a", "cut", log=log)); time.sleep(5)
        flows = ping_stop()
        return report(name, flows, steps, paths=pmap,
                      notes=["drain then cut of uplink A (restore not run)", f"final csr1 adjacencies: {adjacencies()}"])

    if name == "rescue-wrong-port":
        reset(log)
        steps = []
        ping_start()
        steps.append(playbook("a", "drain", log=log)); time.sleep(5)
        steps.append(playbook("a", "cut", "-e", "'{\"port_override\": {\"csr1\": \"ethernet-1/12\"}}'", log=log))
        time.sleep(5)
        flows = ping_stop()
        cfg = srl("csr1", "info flat network-instance default protocols isis instance i1 interface * level 2 metric")
        return report(name, flows, steps, paths=pmap, notes=[
            "csr1 cut to the WRONG port (ethernet-1/12, cabled to agg-02 e1-11) while agg-01 moved to e1-11 correctly.",
            "Expected: adjacency never comes up -> rescue cancel-commit on both ends -> back to drained e1-1.",
            f"csr1 IS-IS metrics after: {cfg.strip()}", f"csr1 adjacencies after: {adjacencies()}"])

    if name == "rescue-far-end-shut":
        reset(log)
        steps = []
        ping_start()
        steps.append(playbook("a", "drain", log=log)); time.sleep(5)
        sh("echo 'enter candidate private\nset / interface ethernet-1/11 admin-state disable\ncommit now' | "
           "docker exec -i clab-cutover-agg-01 sr_cli -ed")
        shut_state = (srl("agg-01", "info from state interface ethernet-1/11 admin-state oper-state").split()
                      + ["| csr1 e1-11:"] + srl("csr1", "info from state interface ethernet-1/11 oper-state").split())
        t = now(); steps.append({"step": "OOB: shut agg-01 ethernet-1/11 (far end)", "rc": 0, "start": t, "end": t + 1, "secs": 1, "recap": []})
        steps.append(playbook("a", "cut", log=log)); time.sleep(5)
        flows = ping_stop()
        post = " ".join(srl("agg-01", "info interface ethernet-1/11 admin-state").split())
        sh("echo 'enter candidate private\nset / interface ethernet-1/11 admin-state enable\ncommit now' | "
           "docker exec -i clab-cutover-agg-01 sr_cli -ed")
        cfg = srl("csr1", "info flat network-instance default protocols isis instance i1 interface * level 2 metric")
        return report(name, flows, steps, paths=pmap, notes=[
            "agg-01 ethernet-1/11 administratively shut out of band before the cut (simulates far end not patched / shut).",
            "Expected: adjacency on e1-11.100 never comes up -> rescue cancel-commit on both ends.",
            f"state right after the shut: agg-01 e1-11: {' '.join(shut_state)}",
            f"agg-01 e1-11 admin-state after the cut attempt (before the test re-enables it): {post}",
            f"csr1 IS-IS metrics after: {cfg.strip()}", f"csr1 adjacencies after: {adjacencies()}",
            "agg-01 ethernet-1/11 re-enabled after the test."])

    sys.exit(f"unknown scenario {name}")


def rereport(name):
    """Re-apply the in-flight rule to a saved run: losses after the last step + 3 s are the ping stop artefact."""
    d = json.loads((RES / f"{name}.json").read_text())
    cutoff = max(st["end"] for st in d["steps"]) + 3
    for f in d["flows"].values():
        if "inflight_at_stop" not in f:
            tail = [t for t in f["lost_at"] if t > cutoff]
            f["lost_at"] = [t for t in f["lost_at"] if t <= cutoff]
            f["inflight_at_stop"] = len(tail); f["lost"] = len(f["lost_at"]); f["tx"] -= len(tail)
    txt = (RES / f"{name}.txt").read_text()
    notes = txt.split("\n\n")[-1].strip().splitlines() if "\n\n" in txt else []
    notes = [l for l in notes if not l.startswith("  ")]
    report(name, d["flows"], d["steps"], notes=notes, paths=load_paths())


if __name__ == "__main__":
    if sys.argv[1:2] == ["rereport"]:
        for n in sys.argv[2:]:
            rereport(n)
        sys.exit()
    for n in sys.argv[1:] or ["full"]:
        scenario(n)
