# Make-before-break fiber uplift over NETCONF (SR Linux + Ansible)

Replaces a cell site's uplink fiber with continuous traffic running, the way a real uplift is done: the new fiber
is turned up next to the old one, traffic is swung onto it, and the old circuit is removed only once it carries
nothing. Every step is one NETCONF transaction per router: candidate datastore, `<validate>`, a confirmed commit
with `<persist>`, checks, and automatic rollback if the checks fail. Measured results are in
[FINDINGS.md](FINDINGS.md); raw output is in `results/`. The earlier port-move version of this lab and its results
are in `results/v1-port-move/`.

- **NOS:** Nokia SR Linux **26.7.2-519** (`ghcr.io/nokia/srlinux:26.7.2`, node type `ixr-d2l`). I used the image
  already on the server, because the server disk was 99% full and couldn't take a fresh pull.
- **Ansible:** ansible-core 2.19.15 with ansible.netcommon 8.7.1, plus ncclient, all in a venv on the lab server.
- **Containerlab:** 0.78.2 on the lab server (`<lab-server>`).

## Topology: one fiber per site, two VLANs, muxed by the telco

```
                         h2  10.20.1.2/32 (lo), ECMP default via both aggs
                        /  \
               [agg-01]------[agg-02]          MTSO. inter-agg VLAN 300, 172.20.3.0/31
          e1-1 |                  | e1-1
   MTSO fiber, ODD VLANs   MTSO fiber, EVEN VLANs     (every site's VLANs muxed onto these two fibers)
          101 111 201 211        102 112 202 212
               \                  /
              eth5 [  telco  ] eth6            L2 VLAN-aware bridge (the carrier)
          eth1  eth2       eth3  eth4
           |     :          |     :            |  = old fiber (in service)
           |     :          |     :            :  = new fiber (cabled, lit, unconfigured until turnup)
       e1-1 e1-11       e1-1  e1-11
         [csr1]            [csr2]               cell-site routers, IS-IS L2, one adjacency per VLAN
           | e1-3            | e1-3
          h1 (8 flows)      h3 (4 flows, the "bystander" site)
```

| Site | Circuit | CSR port | VLAN to agg-01 (odd) | VLAN to agg-02 (even) | IPs (agg .0 / csr .1) |
|---|---|---|---|---|---|
| csr1 | old fiber | e1-1 | 101 | 102 | 172.20.1.0/31, 172.20.2.0/31 |
| csr1 | new fiber | e1-11 | 111 | 112 | 172.20.21.0/31, 172.20.22.0/31 |
| csr2 | old fiber | e1-1 | 201 | 202 | 172.20.31.0/31, 172.20.32.0/31 |
| csr2 | new fiber | e1-11 | 211 | 212 | 172.20.41.0/31, 172.20.42.0/31 |

- **Odd VLAN = uplink `a`** (to agg-01), **even VLAN = uplink `b`** (to agg-02). Both ride the site's one fiber, so
  losing that fiber loses both.
- **On the MTSO side nothing moves.** The telco hands the new VLANs to the aggs on the same MTSO fiber, so on the aggs
  the new circuit is just another subinterface on `ethernet-1/1`, next to the other sites' subinterfaces.
- **The telco** provisions each site's new VLANs on both its old and new port, so the later re-home onto the original
  CSR port needs no telco change.
- **Precondition: the new port must already be lit and admin-up.** The playbook never changes port admin-state, so it
  can't undo a shutdown someone made on purpose.
- Loopbacks: csr1 10.0.0.1, csr2 10.0.0.2, agg-01 10.0.0.11, agg-02 10.0.0.12. The aggs redistribute a static /32
  to h2, so each CSR reaches h2 over two equal-cost paths (one per VLAN).

NETCONF runs on port 830 in the mgmt network-instance (`system ssh-server mgmt-netconf` +
`system netconf-server mgmt`). The Ansible user is `ansible` / `Ansible123!` (lab only) with role `netconf-ops`.
That role is limited to get, get-config, get-schema, edit-config, lock, unlock, validate, commit, cancel-commit,
discard-changes and close-session (no copy-config, delete-config or kill-session).

## The uplift

One run = one phase = one confirmed commit on every node of the run.

IS-IS metric on each circuit of csr1 after each step (the agg ends carry the same metric; their port never changes):

```
                     csr1 e1-1 (old fiber)   csr1 e1-11 (new fiber)   site traffic on
step                 VLAN 101   VLAN 102     VLAN 111   VLAN 112
baseline                10         10            -          -         old fiber
1 turnup                10         10          1000       1000        old fiber      change window
2 swing               1000       1000            10         10        new fiber
3 decom                  -          -            10         10        new fiber
------------------------------------------------------------------------------------------------
later window: the new circuits move back onto the original port, one VLAN at a time
                     VLAN 111   VLAN 112     VLAN 111   VLAN 112
                     on e1-1    on e1-1      on e1-11   on e1-11
4a rehome-drain          -          -          1000         10        VLAN 112
4a rehome-cut         1000          -             -         10        VLAN 112
4a rehome-restore       10          -             -         10        both
4b rehome-drain         10          -             -       1000        VLAN 111
4b rehome-cut           10       1000             -          -        VLAN 111
4b rehome-restore       10         10             -          -        both, on e1-1
```

| Phase | Nodes | What it changes | Checks before the lock | Checks before confirming |
|---|---|---|---|---|
| `turnup` | csr + both aggs | adds the new circuits at metric 1000 | - | new adjacencies up; old adjacencies still up; new circuits idle on every end; old circuits still carrying traffic |
| `swing` | csr + both aggs | new circuits to 10, old to 1000, in one commit per node | new adjacencies up, or refuse | old circuits idle on every end; new circuits carrying traffic |
| `decom` | csr + both aggs | removes the old circuits | new adjacencies up, and old circuits idle on every end, or refuse | new adjacencies still up |
| `rehome-drain` | csr + one agg | that VLAN's new circuit to 1000 | the site's other VLAN is up | adjacency up; other VLAN still up |
| `rehome-cut` | csr + one agg | moves that VLAN from e1-11 to e1-1 (same VLAN and IP); no-op on the agg | other VLAN up; this VLAN drained | adjacency up on e1-1; other VLAN still up |
| `rehome-restore` | csr + one agg | back to 10 on e1-1 | other VLAN up | adjacency up; other VLAN still up |
| `baseline` | csr + both aggs | lab reset: old circuits at 10, new circuits removed | - | old adjacencies up |

Traffic checks read the out-packets counter of every subinterface over NETCONF twice, `traffic_sample_secs` (12 s)
apart, after `traffic_settle_secs` (6 s). "Idle" is at most `circuit_idle_pps` (5) on **every** end; "carrying
traffic" is at least `circuit_busy_pps` (20) **summed** over the ends, because ECMP can leave one end's egress on a
circuit at zero. All four are in `ansible/group_vars/srl.yml`. The 12 s window is set by SR Linux refreshing these
counters only about every 5 s in this lab (see FINDINGS).

## What one run does

The playbook targets `site_<csr>` (both VLANs: the csr and both aggs) or `site_<csr>_<a|b>` (one VLAN: the csr and
that agg). It holds one NETCONF session per node for the whole run (Ansible's persistent connection), and that
session is what keeps the lock.

1. **Pre-checks** (table above). A node that fails a pre-check drops out of the play, so a **gate** then fails
   every other node unless every node passed. Without the gate, the other ends would carry on and commit alone.
2. **`<lock>`** the candidate. If it is refused because the candidate holds uncommitted changes, stop and explain;
   `-e discard_stale_candidate=true` discards them first.
3. **`<edit-config>`** with target candidate only. Running is never written directly.
4. **`<validate>`** with source candidate.
5. **Diff:** `<get-config>` of running and candidate for every port the site uses and the network-instance, printed
   as a unified diff. On the aggs that port is shared with the other sites, so a stray change to another site's
   subinterface would show up here.
6. **Dry run** (the default): `<discard-changes>`, `<unlock>`, stop.
7. **`<commit>`** with `<confirmed/>`, `<confirm-timeout>` (120 s) and `<persist>` (a per-run id).
8. **Verify** (table above). Each node sets `checks_passed`; a gate fails *every* node unless *every* node passed.
9. **Confirm:** `<commit>` with `<persist-id>`, then `<unlock>`.

**Rescue** (any failure after the lock): `<cancel-commit>` with persist-id if a commit is pending, then
`<discard-changes>` and `<unlock>`, then fail loudly with the failing task and reason.

## Files

```
cutover.clab.yml            topology (mgmt 172.30.40.0/24), including the telco bridge's VLAN plan
configs/*.cfg               SR Linux startup configs = baseline (generated by bin/gen_configs.py)
ansible/
  ansible.cfg  requirements.yml
  inventory/hosts.yml       nodes + groups site_<csr> and site_<csr>_<a|b>
  group_vars/srl.yml        NETCONF connection, metrics, timers, traffic thresholds, phase definitions
  host_vars/<node>.yml      uplinks[site][link]: old/new port, VLAN, IP, home port - this node's side only
  templates/uplink.xml.j2   edit-config payload for one phase on one node
  tasks/measure.yml         two counter reads -> out-pps per subinterface
  filter_plugins/srl.py     phase planner, adjacency/metric/counter parsing, running vs candidate diff
  uplink_cutover.yml        the playbook
bin/
  gen_configs.py            write configs/*.cfg
  nc_hello.py               print the capabilities each node advertises in its <hello>
  nc_get.py                 dump config or state over NETCONF with a subtree filter
  nc_semantics.py           raw ncclient probes of confirmed-commit behaviour (v1; results in results/v1-port-move)
  cutover_test.py           traffic + scenarios (below)
  kill_test.py              kill the playbook's process group after the swing commit and time the rollback
results/                    one .txt summary, .json raw, .ansible.log full playbook output and .out console per run
results/superseded/         runs that are not valid results, with the reason in FINDINGS
results/v1-port-move/       the earlier port-move lab
```

## Reproduce

Everything runs on the lab server. From the Mac (if `ssh` hangs at public-key auth, the macOS SSH agent is stuck:
add `-o IdentityAgent=none`, e.g. `rsync -e "ssh -o IdentityAgent=none" ...`):

```bash
rsync -a --exclude 'clab-*' --exclude 'ansible/collections' ~/code/Nokia_Lab/cutover/ <lab-server>:nokia_lab/cutover/
ssh <lab-server>
cd ~/nokia_lab/cutover
```

On the server:

```bash
# 1. lab
docker tag ghcr.io/nokia/srlinux:latest ghcr.io/nokia/srlinux:26.7.2   # or: docker pull ghcr.io/nokia/srlinux:26.7.2
containerlab deploy -t cutover.clab.yml

# 2. tooling (one time)
python3 -m venv ~/nokia_lab/cutover-venv
~/nokia_lab/cutover-venv/bin/pip install 'ansible-core>=2.17,<2.20' ncclient
source ~/nokia_lab/cutover-venv/bin/activate
(cd ansible && ansible-galaxy collection install -r requirements.yml -p ./collections)

# 3. NETCONF hello: candidate, confirmed-commit:1.1, validate:1.1
python bin/nc_hello.py

# 4. the uplift of csr1. Dry run by default: lock, edit candidate, validate, diff, discard, unlock.
cd ansible
ansible-playbook uplink_cutover.yml -e site=csr1 -e phase=turnup
ansible-playbook uplink_cutover.yml -e site=csr1 -e phase=turnup -e apply_change=true
ansible-playbook uplink_cutover.yml -e site=csr1 -e phase=swing  -e apply_change=true
ansible-playbook uplink_cutover.yml -e site=csr1 -e phase=decom  -e apply_change=true
# later window: re-home onto the original port, one VLAN at a time
for l in a b; do for p in rehome-drain rehome-cut rehome-restore; do
  ansible-playbook uplink_cutover.yml -e site=csr1 -e link=$l -e phase=$p -e apply_change=true
done; done
cd ..

# 5. the measured tests (each resets both sites to the old fibers first). Run them detached: they take minutes.
python bin/cutover_test.py paths                  # which VLAN each csr1 flow uses, each direction
python bin/cutover_test.py full-uplift            # csr1 then csr2: turnup/swing/decom, then re-home all 4 VLANs
python bin/cutover_test.py mbb-1 bbm-1            # make-before-break vs break-before-make (repeat as you like)
python bin/cutover_test.py decom-without-swing new-fiber-dark swing-before-turnup
python bin/kill_test.py                           # SIGKILL the playbook's process group after the swing commit
python bin/cutover_test.py reset                  # both sites back to the old fibers

containerlab destroy -t cutover.clab.yml --cleanup
```

The lab-only `-e` knobs the tests use are listed at the top of `ansible/uplink_cutover.yml`. They exist to measure
what the guards prevent; never use them in production.

## Operational notes (from the tests; details in FINDINGS.md)

- **Kill a run cleanly.** Killing the playbook's process group is not enough: ansible-core's workers call `setsid()`
  and run in their own sessions, so they survive it (measured: 3 of 3). Kill them by name, e.g.
  `pkill -KILL -f 'ansible-playbook uplink_cutover.yml'`. The persistent NETCONF connection daemons
  (`ansible_connection_cli_stub.py`) also survive, and idle out after `[persistent_connection] connect_timeout` (30 s).
- **Pending commit vs. session.** With the default `<persist>`, a pending commit doesn't depend on any Ansible
  process: it rolls back when `confirm_timeout` expires. To roll back sooner from another session, use
  `<cancel-commit><persist-id>…</persist-id></cancel-commit>`. The id is printed in the `7. CONFIRMED-COMMIT-SENT` task.
- **Stale candidate after a rollback.** When a commit is rolled back after its session is gone, the next run can stop
  at step 2 with "candidate has been modified". Check what is in the candidate, then rerun with
  `-e discard_stale_candidate=true`.
