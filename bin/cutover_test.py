#!/usr/bin/env python3
"""Run the uplift test scenarios with continuous traffic and record packet loss.

Run on the lab server with the venv python:
    ~/nokia_lab/cutover-venv/bin/python bin/cutover_test.py <scenario> [...]
Scenarios: reset  paths  full-uplift  mbb-N  bbm-N  decom-without-swing  new-fiber-dark  swing-before-turnup

Traffic: 8 ping flows h1 (10.10.1.2-9, behind csr1, the site being changed) and 4 flows h3 (10.10.2.2-5, behind
csr2, a bystander site muxed onto the same MTSO fibers) -> h2 (10.20.1.2), ping -i 0.01 each.
A lost packet is a sequence number with replies on both sides of it; it is placed in time from the replies around
it and attributed to the playbook step that was running then. Packets still in flight when ping is stopped are
not counted. After every step the out-pps of every uplink subinterface is sampled: that is the evidence of which
circuit carries the traffic at each step.
"""
import json, os, re, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ANS = HERE / "ansible"
RES = HERE / "results"
VENV = Path.home() / "nokia_lab/cutover-venv/bin"
DST = "10.20.1.2"
INTERVAL = 0.01
FLOWS = {"csr1": ("clab-cutover-h1", [f"10.10.1.{i}" for i in range(2, 10)]),
         "csr2": ("clab-cutover-h3", [f"10.10.2.{i}" for i in range(2, 6)])}
NODES = ["csr1", "csr2", "agg-01", "agg-02"]
SAMPLE = 11          # s between counter reads (SR Linux refreshes these counters about every 5 s)


def sh(cmd, check=True, **kw):
    return subprocess.run(cmd, shell=True, check=check, text=True, capture_output=True, **kw)


def now():
    return time.time()


# ------------------------------------------------------------------ traffic
def ping_start():
    for site, (host, srcs) in FLOWS.items():
        sh(f"docker exec {host} sh -c 'pkill -INT ping; rm -f /tmp/p*.log'", check=False)
        for s in srcs:
            n = s.split(".")[-1]
            sh(f"docker exec -d {host} sh -c 'exec ping -D -i {INTERVAL} -W 1 -I {s} {DST} > /tmp/p{n}.log 2>&1'")
    time.sleep(2)
    return now()


def ping_stop():
    time.sleep(3)  # let trailing replies arrive
    for host, _ in FLOWS.values():
        sh(f"docker exec {host} pkill -INT ping", check=False)
    time.sleep(1.5)
    flows = {}
    for site, (host, srcs) in FLOWS.items():
        for s in srcs:
            log = sh(f"docker exec {host} cat /tmp/p{s.split('.')[-1]}.log").stdout
            m = re.search(r"(\d+) packets transmitted, (\d+) received", log)
            tx, rx = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
            got = {}
            for ts, seq in re.findall(r"\[(\d+\.\d+)\] \d+ bytes from .*icmp_seq=(\d+)", log):
                got[int(seq)] = float(ts)
            seqs = sorted(got)
            missing, longest = [], 0.0
            for a, b in zip(seqs, seqs[1:]):
                if b - a > 1:
                    step = (got[b] - got[a]) / (b - a)
                    missing += [got[a] + step * k for k in range(1, b - a)]
                    longest = max(longest, got[b] - got[a])
            # packets sent after the last reply were in flight when ping got SIGINT: a stop artefact, not loss
            inflight = tx - seqs[-1] if seqs else 0
            sent = tx - inflight
            dur = (got[seqs[-1]] - got[seqs[0]]) if len(seqs) > 1 else 0
            flows[s] = {"site": site, "sent": sent, "delivered": len(got), "lost": sent - len(got),
                        "gap_lost": len(missing), "inflight_at_stop": inflight,
                        "longest_gap_ms": round(longest * 1000) if longest else 0,
                        "pps": round(len(got) / dur, 1) if dur else 0, "secs": round(dur, 1), "lost_at": missing}
    return flows


# ------------------------------------------------------------------ ansible
def playbook(site, phase, *extra, link=None, log=None, timeout=900):
    args = " ".join(["-e", f"site={site}", "-e", f"phase={phase}", "-e", "apply_change=true",
                     *(["-e", f"link={link}"] if link else []), *extra])
    cmd = f"cd {ANS} && PYTHONUNBUFFERED=1 {VENV}/ansible-playbook uplink_cutover.yml {args}"
    t0 = now()
    p = subprocess.run(cmd, shell=True, text=True, capture_output=True, timeout=timeout,
                       env={**os.environ, "PATH": f"{VENV}:{os.environ['PATH']}", "ANSIBLE_FORCE_COLOR": "0"})
    t1 = now()
    if log is not None:
        log.write(f"\n===== ansible-playbook {args}  (rc={p.returncode}, {t1 - t0:.1f}s)\n{p.stdout}{p.stderr}")
        log.flush()
    why = re.findall(r"(REFUSED:.*|STOP:.*|\w+ FAILED on \S+ at task '[^']*')", p.stdout)
    label = f"{site} {link + ' ' if link else ''}{phase} {' '.join(x for x in extra if x != '-e')}".strip()
    return {"step": label, "rc": p.returncode, "start": t0, "end": t1, "secs": round(t1 - t0, 1),
            "why": sorted(set(w.strip()[:300] for w in why))}


def srl(node, cmd):
    return sh(f"docker exec clab-cutover-{node} sr_cli -d \"{cmd}\"", check=False).stdout


MGMT = {"csr1": "172.30.40.11", "csr2": "172.30.40.12", "agg-01": "172.30.40.21", "agg-02": "172.30.40.22"}
ADJ_FLT = ('<network-instance xmlns="urn:nokia.com:srlinux:net-inst:network-instance"><name>default</name><protocols>'
           '<isis xmlns="urn:nokia.com:srlinux:isis:isis"><instance><name>i1</name><interface><interface-name/>'
           '<adjacency><state/></adjacency></interface></instance></isis></protocols></network-instance>')


def adjacencies(node):
    """[(subinterface, state)] over NETCONF (the CLI table truncates long interface names)."""
    from ncclient import manager
    ns = "{urn:nokia.com:srlinux:isis:isis}"
    with manager.connect(host=MGMT[node], port=830, username="ansible", password="Ansible123!", hostkey_verify=False,
                         look_for_keys=False, allow_agent=False, timeout=30) as m:
        root = m.get(filter=("subtree", ADJ_FLT)).data_ele
    return sorted((i.findtext(f"{ns}interface-name"), a.findtext(f"{ns}state"))
                  for i in root.iter(f"{ns}interface") for a in i.findall(f"{ns}adjacency"))


def metrics(node):
    out = srl(node, "info flat network-instance default protocols isis instance i1 interface * level 2 metric")
    return dict(re.findall(r"interface (\S+) level 2 metric (\d+)", out))


# ------------------------------------------------------------------ circuit evidence
def _counters(node):
    out = srl(node, "info flat from state interface ethernet-1/* subinterface * statistics out-packets")
    return {f"{p}.{s}": int(v) for p, s, v in
            re.findall(r"interface (ethernet-1/1{1,2}) subinterface (\d+) statistics out-packets (\d+)", out)}


def circuit_rates(label):
    """out-pps of every uplink subinterface on every node, over SAMPLE seconds."""
    with ThreadPoolExecutor(4) as ex:
        a = dict(zip(NODES, ex.map(_counters, NODES)))
        t0 = now()
        time.sleep(SAMPLE)
        b = dict(zip(NODES, ex.map(_counters, NODES)))
        t1 = now()
    rates = {}
    for n in NODES:
        for k, v in b[n].items():
            if k in a[n]:
                rates[f"{n} {k}"] = round((v - a[n][k]) / (t1 - t0))
    return {"after": label, "t": t1, "pps": rates}


def fmt_rates(snap):
    # VLAN 1xx = csr1 site, 2xx = csr2 site; x0x = old circuit, x1x = new circuit; odd = to agg-01, even = to agg-02
    keys = sorted(snap["pps"], key=lambda k: (k.split(".")[-1], k))
    return f"  after {snap['after']}:\n" + "\n".join(
        "     " + "  ".join(f"{k.replace('ethernet-', 'e')}={snap['pps'][k]:>4}" for k in keys[i:i + 4])
        for i in range(0, len(keys), 4))


# ------------------------------------------------------------------ reporting
def attribute(flows, steps):
    per_step = {s["step"]: {"csr1": 0, "csr2": 0} for s in steps}
    outside = {"csr1": 0, "csr2": 0}
    for f in flows.values():
        for t in f["lost_at"]:
            for s in steps:
                if s["start"] - 0.5 <= t <= s["end"] + 0.5:
                    per_step[s["step"]][f["site"]] += 1
                    break
            else:
                outside[f["site"]] += 1
    return per_step, outside


def totals(flows, site):
    fs = [f for f in flows.values() if f["site"] == site]
    return sum(f["sent"] for f in fs), sum(f["delivered"] for f in fs), sum(f["lost"] for f in fs)


def report(name, flows, steps, snaps=(), notes=(), paths=None):
    per_step, outside = attribute(flows, steps)
    lines = [f"# {name}", f"run at {time.strftime('%Y-%m-%d %H:%M:%S %Z')}"]
    for site, role in (("csr1", "site being changed"), ("csr2", "bystander site, same MTSO fibers")):
        s, d, l = totals(flows, site)
        lines.append(f"{site} ({role}): sent {s:,}, delivered {d:,} (lost {l:,})  "
                     f"[{len(FLOWS[site][1])} flows, ping -i {INTERVAL}]")
    lines += ["lost = sent - delivered; packets still in flight when ping was stopped are not counted as sent",
              "", "## steps (lost packets placed in each step's time window: csr1 / csr2)"]
    for s in steps:
        ps = per_step[s["step"]]
        lines.append(f"  {s['step']:<62} rc={s['rc']:<3} {s['secs']:>6}s  lost {ps['csr1']} / {ps['csr2']}")
        for w in s.get("why", []):
            lines.append(f"      -> {w}")
    if any(outside.values()):
        lines.append(f"  (lost outside any step window: {outside['csr1']} / {outside['csr2']})")
    if snaps:
        lines += ["", "## out-pps per uplink subinterface after each step (evidence of which circuit carries traffic)",
                  "   VLAN 101/102 csr1 old, 111/112 csr1 new, 201/202 csr2 old, 211/212 csr2 new"]
        lines += [fmt_rates(sn) for sn in snaps]
    lines += ["", "## flows"]
    for src, f in flows.items():
        p = paths.get(src, {}) if paths else {}
        lines.append(f"  {f['site']} {src} -> {DST}: sent {f['sent']}, delivered {f['delivered']} (lost {f['lost']})"
                     f", longest gap {f['longest_gap_ms']} ms, {f['pps']} pps over {f['secs']} s"
                     f", {f['inflight_at_stop']} in flight at stop"
                     + (f"  [fwd via uplink-{p.get('fwd', '?')}, return via uplink-{p.get('rev', '?')}]" if p else ""))
    lines += ["", *notes]
    text = "\n".join(lines) + "\n"
    (RES / f"{name}.txt").write_text(text)
    (RES / f"{name}.json").write_text(json.dumps({"flows": flows, "steps": steps, "circuit_pps": list(snaps)}, indent=1))
    print(text)


# ------------------------------------------------------------------ path mapping
def paths():
    """Which VLAN each csr1 flow takes: forward = csr1 egress, return = agg egress toward csr1 (old circuits)."""
    res = {}
    host, srcs = FLOWS["csr1"]
    for s in srcs:
        a = {n: _counters(n) for n in ("csr1", "agg-01", "agg-02")}
        sh(f"docker exec {host} ping -q -i 0.002 -c 500 -I {s} {DST}", check=False)
        time.sleep(8)       # > one counter refresh
        b = {n: _counters(n) for n in ("csr1", "agg-01", "agg-02")}
        d = lambda n, k: b[n].get(k, 0) - a[n].get(k, 0)
        fa, fb = d("csr1", "ethernet-1/1.101"), d("csr1", "ethernet-1/1.102")
        ra, rb = d("agg-01", "ethernet-1/1.101"), d("agg-02", "ethernet-1/1.102")
        res[s] = {"fwd": "a" if fa > fb else "b", "rev": "a" if ra > rb else "b", "counts": [fa, fb, ra, rb]}
    (RES / "paths.json").write_text(json.dumps(res, indent=1))
    return res


def load_paths():
    p = RES / "paths.json"
    return json.loads(p.read_text()) if p.exists() else None


# ------------------------------------------------------------------ scenarios
def reset(log):
    """Both sites back to the old fibers at normal metric (no traffic measured)."""
    for site in ("csr1", "csr2"):
        r = playbook(site, "baseline", "-e", "discard_stale_candidate=true", log=log)
        if r["rc"]:
            sys.exit(f"reset of {site} failed, see log")
    time.sleep(15)
    print("reset done; csr1 adjacencies:", adjacencies("csr1"), "csr2:", adjacencies("csr2"))


def run_steps(name, plan, log, notes=(), settle=5, snap_between=True):
    """plan = list of (site, phase, link, extra...) run in order with traffic; snapshot circuits after each step
    (snap_between=False: only after the last step, so the steps run back to back)."""
    reset(log)
    steps, snaps = [], []
    ping_start()
    snaps.append(circuit_rates("start (baseline)"))
    for i, (site, phase, link, *extra) in enumerate(plan):
        steps.append(playbook(site, phase, *extra, link=link, log=log))
        if snap_between or i == len(plan) - 1:
            time.sleep(settle)
            snaps.append(circuit_rates(steps[-1]["step"] + f" (rc={steps[-1]['rc']})"))
    flows = ping_stop()
    report(name, flows, steps, snaps, paths=load_paths(), notes=[
        *notes, f"final csr1 adjacencies: {adjacencies('csr1')}", f"final csr1 IS-IS metrics: {metrics('csr1')}",
        f"final csr2 adjacencies: {adjacencies('csr2')}"])
    return steps


MBB = [("csr1", "turnup", None), ("csr1", "swing", None), ("csr1", "decom", None)]
BBM = [("csr1", "decom", None, "-e", "skip_decom_checks=true"),
       ("csr1", "turnup", None, "-e", "turnup_metric=normal")]


def telco_port(port, state):
    sh(f"docker exec clab-cutover-telco ip link set {port} {state}")


def scenario(name):
    RES.mkdir(exist_ok=True)
    log = open(RES / f"{name}.ansible.log", "w")
    if name == "paths":
        reset(log)
        print(json.dumps(paths(), indent=1)); return
    if name == "reset":
        reset(log); return

    if name == "full-uplift":
        plan = [*MBB, ("csr2", "turnup", None), ("csr2", "swing", None), ("csr2", "decom", None)]
        for site in ("csr1", "csr2"):
            for link in "ab":
                plan += [(site, "rehome-drain", link), (site, "rehome-cut", link), (site, "rehome-restore", link)]
        return run_steps(name, plan, log, notes=[
            "csr1 turnup -> swing -> decom, then csr2 the same; then re-home csr1 VLAN a, csr1 VLAN b, csr2 a, csr2 b",
            "(drain -> cut -> restore each) from ethernet-1/11 back onto ethernet-1/1."])

    if name.startswith("mbb"):
        return run_steps(name, MBB, log, notes=["make-before-break on csr1: turnup -> swing -> decom"])

    if name.startswith("bbm"):
        return run_steps(name, BBM, log, snap_between=False, notes=[
            "break-before-make on csr1 (lab knobs): remove the old circuits first (skip_decom_checks),",
            "then turn the new ones up straight at metric 10 (turnup_metric=normal), back to back with no pause.",
            "The outage is decom commit -> turnup run -> adjacencies up -> SPF: the fastest 'move the fiber' this lab",
            "can do. A real break-before-make adds the time to physically move the fiber."])

    if name == "decom-without-swing":
        return run_steps(name, [("csr1", "turnup", None), ("csr1", "decom", None),
                                ("csr1", "decom", None, "-e", "skip_decom_checks=true")], log, notes=[
            "turnup, then decom while the old circuits still carry the site (expected: refused),",
            "then the same decom with the guard skipped, to measure the loss the guard prevents."])

    if name == "swing-before-turnup":
        return run_steps(name, [("csr1", "swing", None)], log, notes=[
            "swing straight from baseline, no turnup (expected: refused before anything is locked)."])

    if name == "new-fiber-dark":
        reset(log)
        steps, snaps = [], []
        ping_start()
        snaps.append(circuit_rates("start (baseline)"))
        telco_port("eth2", "down")                       # the telco has not lit csr1's new fiber
        time.sleep(2)
        dark = srl("csr1", "info from state interface ethernet-1/11 oper-state").split()
        t = now(); steps.append({"step": "OOB: telco eth2 (csr1 new fiber) down", "rc": 0, "start": t, "end": t + 1,
                                 "secs": 1, "why": []})
        steps.append(playbook("csr1", "turnup", log=log)); time.sleep(5)
        snaps.append(circuit_rates(steps[-1]["step"] + f" (rc={steps[-1]['rc']})"))
        flows = ping_stop()
        after = {n: metrics(n) for n in ("csr1", "agg-01", "agg-02")}
        telco_port("eth2", "up")
        return report(name, flows, steps, snaps, paths=load_paths(), notes=[
            "telco port eth2 (far end of csr1 ethernet-1/11, the new fiber) taken down out of band before turnup.",
            "Expected: no adjacency on the new circuits -> rescue cancel-commit on all three nodes, old circuits untouched.",
            f"csr1 ethernet-1/11 after the telco shut: {' '.join(dark)}",
            f"IS-IS metrics after the attempt: {after}",
            f"csr1 adjacencies after: {adjacencies('csr1')}", "telco eth2 brought back up after the test."])

    sys.exit(f"unknown scenario {name}")


if __name__ == "__main__":
    for n in sys.argv[1:] or ["full-uplift"]:
        scenario(n)
