# Findings: make-before-break fiber uplift over NETCONF on SR Linux

Tested 2026-11-10 on SR Linux 26.7.2-519 in containerlab 0.78.2, with ansible-core 2.19.15,
ansible.netcommon 8.7.1 and ncclient. Every number below comes from a run whose raw output is in `results/`.
Nothing is reported as passing unless I ran it. Loss is always given as "sent N, delivered N (lost N)".

The setup (README has the details): each cell-site router has **one fiber** to the telco carrying **two VLANs**.
The telco hands the odd VLAN to agg-01 and the even VLAN to agg-02, over **one MTSO fiber per agg** that every
site's VLANs are muxed onto. csr1 is the site being changed; csr2 is a second site on the same MTSO fibers, with
its own traffic, measured in every run as a bystander.

## Headline

| Test | csr1 (site being changed) | csr2 (bystander) | Result file |
|---|---|---|---|
| **Make-before-break** (turnup → swing → decom), run 1 | sent 91,927, delivered 91,927 (**lost 0**) | sent 45,928, delivered 45,928 (lost 0) | `results/mbb-1.txt` |
| run 2 | sent 83,378, delivered 83,378 (**lost 0**) | sent 41,537, delivered 41,537 (lost 0) | `results/mbb-2.txt` |
| run 3 | sent 86,642, delivered 86,642 (**lost 0**) | sent 43,230, delivered 43,230 (lost 0) | `results/mbb-3.txt` |
| **Break-before-make** (remove old, then turn up new), run 1 | sent 27,631, delivered 18,262 (**lost 9,369**) | sent 13,450, delivered 13,450 (lost 0) | `results/bbm-1.txt` |
| run 2 | sent 32,345, delivered 23,691 (**lost 8,654**) | sent 15,995, delivered 15,995 (lost 0) | `results/bbm-2.txt` |
| run 3 | sent 32,042, delivered 23,211 (**lost 8,831**) | sent 15,830, delivered 15,830 (lost 0) | `results/bbm-3.txt` |
| **Full uplift**: csr1 and csr2 turnup/swing/decom, then re-home all 4 VLANs onto the original port (18 commits) | sent 347,098, delivered 347,098 (**lost 0**) | sent 172,875, delivered 172,875 (lost 0) | `results/full-uplift.txt` |

**Measured difference:** the same fiber change lost nothing in each of 3 make-before-break runs, and 8,654–9,369
packets in each of 3 break-before-make runs. In break-before-make **every one of the 8 flows** had a single hole of
**17.4–19.3 s**: the site has one fiber, so removing its circuits isolates it until the new ones are up.

That 17–19 s is the *best case*: the two playbook runs back to back, with the new fiber already patched and lit.
It is the decom commit, the turnup run, the IS-IS adjacencies forming and SPF. A real break-before-make adds the
time it takes someone to physically move the fiber.

### How the traffic was measured

- **Flows:** 8 ICMP flows from h1 behind csr1 (10.10.1.2–.9) and 4 from h3 behind csr2 (10.10.2.2–.5) to h2
  (10.20.1.2), each `ping -i 0.01`. The real rate was about 70 pps per flow, so one lost packet is roughly a
  15 ms hole.
- **Path coverage:** with 8 sources, csr1's flows hash across both VLANs. Per-subinterface counters
  (`results/paths.json`) show 6 flows leaving csr1 on the odd VLAN and 2 on the even one, and the return split
  3 via agg-01 and 5 via agg-02. Every flow uses the fiber being replaced, so a zero is not "no traffic on that link".
- **Loss:** lost = sent − delivered. Packets still in flight when ping is stopped are not counted as sent. Each lost
  packet is timed from the replies around it and assigned to the playbook step that was running then.
- **Which circuit carries the traffic:** after every step, the harness samples the out-pps of every uplink
  subinterface on every router over 11 s. Those tables are in each result file, under
  "out-pps per uplink subinterface after each step".

### The counters show the swing happening

From `results/full-uplift.txt`, csr1's circuits (out-pps, both ends):

| After | old VLAN 101 (csr1 / agg-01) | old VLAN 102 (csr1 / agg-02) | new VLAN 111 | new VLAN 112 |
|---|---|---|---|---|
| baseline | 538 / 280 | 178 / 464 | - | - |
| turnup | 565 / 282 | 187 / 464 | 0 / 0 | 0 / 0 |
| swing | 0 / 0 | 0 / 0 | 590 / 297 | 197 / 490 |
| decom | - | - | 397 / 199 | 131 / 329 |

The playbook makes the same measurement itself over NETCONF before it confirms (12 s sample after a 6 s settle).
In all three make-before-break runs it measured exactly 0.0 pps on the new circuits on every end after turnup,
0.0 pps on the old circuits on every end after the swing, and 0.0 pps again in the decom pre-check
(`results/mbb-N.ansible.log`).

## The guards

| Test | What happened | csr1 | csr2 | Result file |
|---|---|---|---|---|
| **swing before turnup** | Refused on all three nodes in 2.1 s before anything was locked: "no IS-IS adjacency up on the new circuits". | sent 21,999, delivered 21,999 (lost 0) | sent 10,947, delivered 10,947 (lost 0) | `results/swing-before-turnup.txt` |
| **decom without swing** | After turnup, decom was refused in the pre-check: the old circuits were still carrying 463.6 / 153.0 pps (csr1) and 232.9 / 385.6 pps (aggs) against a threshold of 5. | | | `results/decom-without-swing.txt` |
| same decom, guard skipped (lab knob) | Removed the old circuits while they carried the site. The new circuits were up at metric 1000, so traffic moved to them after SPF. | sent 74,096, delivered 73,632 (**lost 464**, in the skipped-guard step) | sent 37,025, delivered 37,025 (lost 0) | same |
| **new fiber dark** | The telco port facing csr1's new fiber was shut out of band before turnup (csr1 e1-11 oper-down). Turnup committed, no adjacency came up in the 60 s wait, and all three nodes ran `<cancel-commit>`; 88.0 s in total. The old circuits were never touched (metric 10 throughout). | sent 63,268, delivered 63,268 (lost 0) | sent 31,574, delivered 31,574 (lost 0) | `results/new-fiber-dark.txt` |

**What the decom guard prevents:** 464 packets, which is 56–60 on *every* flow, a hole of 1.02–1.05 s each.
That is about the same 1 s hole as the v1 no-drain cut, and fits SR Linux's IS-IS `timers spf initial-wait` default of 1000 ms.
Here it hits every flow rather than only those returning via one agg, because both VLANs are removed at once
and every router has to recompute. As in v1, that explanation fits the numbers but I did not prove it (no rerun
with a different SPF timer).

## Kill during swing

Method (`bin/kill_test.py`, `results/kill-swing-persist.txt`): baseline, traffic on, csr1 turnup confirmed
normally. Then swing on csr1 with `confirm-timeout` 60 s and the playbook told to sit after the commit. **SIGKILL to
the playbook's process group** 1.4 s after all three nodes had committed. Separate NETCONF sessions polled running
on csr1, agg-01 and agg-02 every 0.25 s.

| | Result |
|---|---|
| **Rollback** (new circuit metric back to 1000) | **csr1 60.0 s, agg-01 60.0 s, agg-02 60.1 s** after the commit: the confirm timer, because the commit used `<persist>` |
| **Loss** | csr1 sent 84,769, delivered 84,768 (**lost 1**, a 40 ms hole when the swing rolled back). csr2 sent 42,241, delivered 42,241 (lost 0). |
| **Processes after the group kill** | The 3 `ansible_connection_cli_stub.py` daemons were still alive right after the kill and gone by 31.1 s (their 30 s idle timeout). **3 `ansible-playbook` worker processes (one per host) also survived the process-group SIGKILL.** |
| **After the rollback** | Running was restored on all three nodes, but each candidate still held the rolled-back swing (metric 10), and `<lock>` was refused: "Lock denied - candidate has been modified". |
| **The next normal swing run** | Stopped at step 2 on all three nodes, by design ("Candidate is not clean - refuse"). It needs `-e discard_stale_candidate=true`. |

**Why the workers survived:** in ansible-core 2.19, `WorkerProcess._detach()` calls `os.setsid()`
(`ansible/executor/process/worker.py`). Every worker runs in its own session, so killing the playbook's process
group cannot reach them. To stop a run for real you have to kill the workers by name as well. The workers cannot run
another task without the main process, and with `<persist>` the devices roll back on their own timer anyway, so here
it changed nothing. It does make "kill the process group" (my v1 advice) wrong, and the README now says so.

## What I had to correct along the way

1. **The topology.** I first built the uplift for the v1 layout (two separate uplinks from csr1, one to each agg).
   You corrected it: one fiber per site with an odd and an even VLAN, a telco mux, and one MTSO fiber per agg shared
   by all sites. I rebuilt the lab: telco bridge, second site, VLANs per fiber, and the playbook targeting a site
   (both VLANs) or one VLAN of a site.
2. **A pre-check could let the other end commit alone (bug carried over from v1).** A node that fails a pre-check
   drops out of the Ansible play, and the other nodes carried on to lock, edit and commit by themselves. v1's
   "other uplink up" guard ran only on csr1, so a failure there would have left the agg changing alone. It was never
   triggered in v1. There is now a gate after the pre-checks: no node locks unless every node passed. The
   swing-before-turnup and decom-without-swing refusals both show all three nodes stopping.
3. **Counter refresh.** SR Linux in this lab refreshes per-subinterface `out-packets` only about every 5 s (measured:
   jumps of about 300 packets every 4–6 s for one 64 pps flow). A rate over "a few seconds" can read 0 on a busy
   circuit. The playbook therefore waits 6 s after a commit and samples over 12 s. The same coarseness explains
   why the pps tables vary by ±30 % from step to step for the same traffic.
4. **The break-before-make knob was wrong, so the first BBM run is not a BBM result.** `skip_decom_checks` skipped
   the decom pre-checks but not decom's post-commit check that the new circuits are up. In BBM they don't exist yet,
   so the playbook waited 60 s, then cancelled its own commit and put the old circuits back. That run lost
   41,162 of 77,791 (83.5 s hole per flow). It shows the post-check catching an unguarded decom, but only after
   the full adjacency wait. Kept as `results/superseded/bbm-1-postcheck-rollback.*`. The knob now skips both checks.
5. **My harness padded the BBM outage.** The next BBM run sampled circuit counters (5 s settle + 11 s) between the decom
   and the turnup, which sat inside the outage: 17,542 lost of 41,252, a 35.7 s hole. Kept as
   `results/superseded/bbm-1-with-snapshot-pause.*`. BBM steps now run back to back; bbm-1 was rerun.
6. **Truncated interface names in the harness.** The SR Linux CLI adjacency table cuts long names
   (`ethernet-1/11.111` shows as `ethernet-`), so the "final adjacencies" notes in `full-uplift.txt` and `mbb-1.txt`
   show truncated names. The harness now reads adjacencies over NETCONF. The playbook always used NETCONF, so its
   checks were not affected.
7. **Process-group kill.** See above: killing the group does not stop Ansible's workers.

## What was not tested

- **The rescue on a wrong re-home port.** The `port_override` knob is still there, but I did not rerun that case in
  the new topology (v1 result: `results/v1-port-move/rescue-wrong-port.txt`).
- **Kill without `<persist>`**: only the default playbook was killed this time (v1 has the no-persist case).
- **The "other VLAN up" guard for re-home failing.** It ran on all 12 re-home commits and always passed; I did not
  take the other VLAN down to watch it refuse.
- **The telco side.** The telco's VLAN provisioning is static in the topology. Nothing checks it before turnup, so a
  missing VLAN at the telco would show up as the new-fiber-dark case (no adjacency, rollback). I did not test that
  variant separately.
- **Losing the management network mid-change** and **two operators at once**.

## What this lab can't show

- **No real optics or carrier.** Links are veth pairs and the telco is a Linux bridge. There is no light, loss of
  signal, optic insertion, carrier ticket or real mux. The "new fiber" is already patched; moving a fiber is outside
  NETCONF.
- **The physical re-home is not hitless in real life.** In the lab the original port and the new port are both cabled,
  so the re-home is drain, move the VLAN in config, restore, one VLAN at a time, with 0 loss. On a real single-fiber
  site, moving the new fiber from the new port back to the original port takes both VLANs down while it is unplugged.
  This lab models the logical re-home only.
- **Timing isn't hardware timing.** Control-plane and FIB timing in containerised SR Linux is not ASIC timing, and the
  counters refresh every ~5 s here.
- **Measurement resolution.** ICMP at about 70 pps per flow, so about 15 ms resolution. No line-rate or TCP traffic.
- **No BFD** is configured, so the 1 s holes are "IS-IS without BFD".
- **Sample sizes:** 3 runs each of make-before-break and break-before-make, 1 full uplift, and 1 run of each guard and
  kill case.
- **Image:** I used the SR Linux image already on the server (26.7.2-519), because the server disk was 99% full
  (2.7–2.8 GB free) and couldn't take a fresh pull. I did not check for anything newer.
