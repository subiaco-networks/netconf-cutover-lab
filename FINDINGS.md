# Findings: uplink cutover over NETCONF on SR Linux

Tested 2026-11-10 on SR Linux 26.7.2-519 in containerlab 0.78.2, with ansible-core 2.19.15,
ansible.netcommon 8.7.1 and ncclient. Every number below comes from a run whose raw output is in `results/`.
Nothing is reported as passing unless I ran it.

## Headline

| Test | Packets lost | Sent | Result file |
|---|---|---|---|
| **Full drain → cut → restore on uplink A, then on uplink B** | **0** | 50,858 | `results/full.txt` |
| (same, earlier run, before the template fix below) | 0 | 49,128 | `results/superseded/full.txt` |
| **Cut of uplink A, drained first**, run 1 / 2 / 3 | **0 / 0 / 0** | 18,863 / 16,989 / 18,966 | `results/drained-cut-N.txt` |
| **Cut of uplink A, no drain**, run 1 / 2 / 3 | **179 / 177 / 175** | 11,225 / 10,414 / 11,270 | `results/nodrain-N.txt` |
| Broken cut, wrong port on csr1 (drained) | 0 | 61,625 | `results/rescue-wrong-port.txt` |
| Broken cut, far end shut (drained) | 0 | 62,743 | `results/rescue-far-end-shut.txt` |
| Playbook killed after confirmed commit (drain change) | 0 | 41,671 | `results/kill-persist.txt` |

**Measured difference:** the same port move lost 0 packets in each of 3 runs with the drain, and 175–179 packets in
each of 3 runs without it. Without the drain, every flow whose return path went through agg-01 saw a hole of
**1.02–1.05 s**.

### How the traffic was measured

- **Flows:** 8 ICMP flows from h1 (sources 10.10.1.2–.9) to h2 (10.20.1.2), each `ping -i 0.01`. The real rate
  was about 64 pps per flow and 460–590 pps in aggregate, so one lost packet is roughly a 15 ms hole.
- **Path coverage:** with 8 sources the flows hash across both uplinks. Per-subinterface counters
  (`results/paths.json`) show 6 flows going out on A and 2 on B, and returns split 3 on A and 5 on B. Seven of the
  eight flows touch uplink A in at least one direction, so a zero is not just "no traffic on that link".
- **Loss:** a lost packet is a sequence number with replies on both sides of it. Packets still in flight when ping
  is stopped are excluded (see the harness corrections below). Each lost packet is timed from the replies around
  it and assigned to the playbook step that was running then.

### Why the no-drain cut loses about 1 second

Per-flow results, consistent across all 3 runs:

| Flow path | Loss per flow |
|---|---|
| Return path via agg-01 → uplink A (3 flows) | 56–61 packets, about 1.03 s gap |
| Forward via uplink A only, return via B (4 flows) | 0–2 packets, 30–56 ms gap |
| Uplink B both ways (1 flow) | 0 |

My explanation fits these numbers, but I did not prove it:

- **csr1 side:** csr1 already has an ECMP sibling through agg-02 in its FIB, so removing the A next-hop is
  near-instant.
- **agg-01 side:** agg-01 has no equal-cost alternative to h1's subnet. It has to run SPF to find the 20-metric
  detour through agg-02.
- **The timer:** SR Linux's IS-IS `timers spf initial-wait` defaults to **1000 ms** (read from state on agg-01),
  which matches the 1.02–1.05 s holes.
- **Untested:** rerunning with a lower SPF initial-wait, or with BFD, would confirm it.
- **With the drain:** the metric change moves agg-01 to the detour *before* the link goes away, so the
  cut changes nothing in anyone's forwarding.

## Rollback and session behaviour

### Does a confirmed commit survive the NETCONF session? It depends on `<persist>`

**Raw ncclient probes** (`bin/nc_semantics.py`, `results/nc_semantics.txt`):

| Probe | Result |
|---|---|
| T1: confirmed commit, 20 s timer, session left open, never confirmed | running reverted **19.9 s** after the commit (20.3 s in an earlier run) |
| T2: confirmed commit, then `<close-session>` | **dies with the session**: reverted in < 0.1 s |
| T3: confirmed commit, then SSH transport dropped with no close (what a SIGKILLed client does) | **dies with the session**: reverted in 0.3 s |
| T4: confirmed commit with `<persist>`, then session closed | **survives**: still in running 3 s later; a *new* session confirmed it with `<persist-id>`, and it was still in running 65 s later |

So the answer is that a plain confirmed commit (RFC 6241 without persist) **dies with the session**, immediately.
With `<persist>` it **survives**, and only the timer or `<cancel-commit>` with `<persist-id>` rolls it back.

**The playbook uses `<persist>` by default.** A pending commit should not hinge on the lifetime of an Ansible
connection process. Persist makes rollback deterministic (the timer, on both ends) and lets an operator confirm or
cancel from any session.

### Killing the playbook right after the confirmed commit

Method (`bin/kill_test.py`):

- **The change:** `phase=drain` on uplink A, `confirm-timeout` 60 s.
- **The kill:** SIGKILL to `ansible-playbook` 1.1 s after both ends had committed.
- **The measurement:** separate NETCONF sessions polled running on both nodes every 0.25 s.

| Mode | Rollback after commit | What triggered it |
|---|---|---|
| default playbook (`<persist>`) | **csr1 60.1 s, agg-01 60.1 s** | the confirm timer. The NETCONF sessions were gone by 30.8 s, yet the change stayed until the timer. |
| `-e persist_commit=false` | **csr1 30.8 s, agg-01 30.9 s** | the session closing. The Ansible persistent-connection daemons (`ansible_connection_cli_stub.py`, one per host) survive SIGKILL of the playbook and hold the NETCONF session until their 30 s idle timeout (`[persistent_connection] connect_timeout`). They were gone at 31.0 s. |

Both kill runs lost 0 packets, because rolling back a drain is harmless.

**What you see after a kill:**

- **Orphaned workers.** SIGKILL of the main `ansible-playbook` PID left **2 orphaned worker processes** (one per
  host) still running their task. The test killed them afterwards. To really stop a run, kill the process group.
- **Dirty candidate.** After the rollback, **running is restored but the shared candidate still holds the
  rolled-back edit** (metric 1000), and `<lock>` is refused with `Lock denied - candidate has been modified`. The
  same thing happened after T2's close-session rollback.
- **The next run stops cleanly.** The next normal run failed at step 2 by design, with a message saying so. Rerun
  with `-e discard_stale_candidate=true` to clear it. I chose not to auto-discard, because a dirty shared candidate
  can also be someone else's work in progress.

### The rescue path: deliberately broken cuts

Both tests start from uplink A drained, with traffic running.

| Break | What happened | Loss | End state |
|---|---|---|---|
| **Wrong port:** csr1 told to cut to `ethernet-1/12`, which is cabled to agg-02; agg-01 moved to its correct port | No adjacency on csr1 `ethernet-1/12.100`. After the 60 s adjacency wait, step 8 failed on csr1. The gate then failed agg-01 too. Both ran `<cancel-commit>` with persist-id (ok on both), then discard and unlock, then failed loudly (rc=2). | **0** | Both ends back on e1-1.100, metric 1000 (drained), adjacency up. Uplink B up throughout. |
| **Far end shut:** agg-01 `ethernet-1/11` admin-disabled out of band, both ends oper-down, before the cut | No adjacency on e1-11.100, the same rescue on both ends, rc=2. agg-01 e1-11 was still admin-disable afterwards. | **0** | Back on e1-1.100 drained, uplink B up throughout. |

The rescue rolls back on `<cancel-commit>` well before the 120 s timer would have. Each failed cut took about
88 s, nearly all of it the 60 s adjacency wait. That is also why the drain matters for the failure case: during
those 60 s the broken link carries no traffic.

### Other checks that ran

- **Hello:** all 3 nodes advertise `candidate:1.0`, `confirmed-commit:1.1`, `validate:1.1`,
  `rollback-on-error:1.0` and `base:1.1` (`bin/nc_hello.py`).
- **Dry run:** a dry run (the default) printed the diff, discarded the candidate and unlocked. Running was unchanged
  afterwards (metric still 10, checked from the CLI).
- **Commit timing:** the two ends commit about 5 ms apart (the Ansible hosts run in parallel), and both confirm
  about 2 s after the commit.

**Not observed directly:** the guard that stops a run when the other uplink is down. It runs on every run, and
every run in this report had the other uplink up, so it always passed. I did not run a test with the other uplink
down.

## What I had to correct along the way

1. **NETCONF config schema.** `system netconf-server-group ...` does not exist in 26.7. The working config is a
   named SSH server on port 830 (`system ssh-server mgmt-netconf`, `disable-shell true`) plus
   `system netconf-server mgmt ssh-server mgmt-netconf`.
2. **Role allow-list.** A role with only `services [ netconf ]` cannot even `<close-session>`
   (`PermissionDenied`). The role needs an explicit `netconf allowed-operations [...]` list.
3. **`export-policy`** under IS-IS is a single leaf in 26.7, not a list (`export-policy export-static`).
4. **No `:xpath` capability.** XPath filters return `Unknown attribute`, so everything uses subtree filters.
5. **ncclient wrapping.** A `<config>` element outside the NETCONF base namespace is rejected with
   `Unknown namespace`. Templates use `<nc:config xmlns:nc="urn:ietf:params:xml:ns:netconf:base:1.0">`.
6. **Template bug, found by the far-end-shut test.** The first version set `<admin-state>enable</admin-state>` on
   the target port, so the cut **re-enabled the port I had shut** and "succeeded". That first test is invalid; it is
   kept in `results/superseded/`. The template no longer touches port admin-state (the new port must already be
   lit), and the rerun is the result above. That port was already admin-up in the other runs, so they were not
   affected.
7. **Kill-test bookkeeping.** My first kill runs looked for `ansible-connection` processes, but in ansible-core 2.19
   the daemon is `ansible_connection_cli_stub.py`. So they wrongly reported "daemons gone at 1.3 s" and didn't
   explain the 30 s rollback. Rerun with the right process name; the old runs are in `results/superseded/`.
8. **Ping measurement.** iputils counts packets still in flight at SIGINT as lost. That produced 3 "lost" packets
   at the stop instant in drained-cut-1. Those are now excluded and reported per flow as "in flight at stop", and
   all saved runs were re-scored the same way (only drained-cut-1 changed, from 3 to 0). Also, `-i 0.01` gives
   about 64 pps per flow, not 100, so loss is timed from real reply timestamps, not assumed intervals.
9. **Node type.** `ixrd2l` is deprecated in clab 0.78; it is now `ixr-d2l`.
10. **Environment.** The Mac's launchd SSH agent hangs public-key auth to the server, which is also why the
    nokia-lab MCP server timed out at session start. `ssh -o IdentityAgent=none` works. This is not fixed;
    see the README.

## What this lab can't show

- **No real optics.** Links are veth pairs. There is no light, loss of signal, autonegotiation or optic
  insertion, and the "new 10G port" is the same virtual speed. A real cut also needs someone to move the fiber,
  which happens outside NETCONF.
- **Timing isn't hardware timing.** Control-plane and FIB-programming timing in a containerised SR Linux is not
  ASIC timing. The 1 s no-drain hole is set by protocol timers, so it should carry over to hardware, but I have not
  verified that.
- **Measurement resolution.** Traffic is ICMP at about 64 pps per flow, so resolution is about 15 ms. A hole
  shorter than that can show up as 0 or 1 lost packets. No line-rate or TCP traffic was tested.
- **No BFD** is configured, so the no-drain loss is "IS-IS without BFD".
- **Sample sizes:** 1 final full A/B run (plus 1 earlier, also 0 loss), 3 runs each for drained vs no-drain, and
  1 run per rescue and kill case.
- **Image:** I used the SR Linux image already on the server (26.7.2-519, built 2026-08-19), because the server
  disk was 99% full (about 3 GB free) and couldn't take a fresh pull. I did not check for anything newer.
- **Failure types not tested:** losing the management network mid-change (T3 covers a dropped transport at the
  NETCONF level, not inside Ansible), killing the whole Ansible process group, and running two operators at once.
