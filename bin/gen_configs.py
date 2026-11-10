#!/usr/bin/env python3
"""Generate SR Linux startup configs (flat CLI) for the uplift lab.

Each cell-site router (csr1, csr2) has ONE fiber to the telco with two VLANs on it: the odd VLAN is handed to
agg-01 and the even VLAN to agg-02, each over the telco's single MTSO fiber per agg, muxed with the other sites.
Baseline = the old fibers only; the new-fiber ports are cabled, admin-up and unconfigured.
"""
CSRS = {
    #        system      NET                         old fiber circuits (vlan, csr IP)      new port       LAN
    "csr1": ("10.0.0.1", "49.0001.0100.0000.0001.00", [(101, "172.20.1.1/31"), (102, "172.20.2.1/31")],
             "ethernet-1/11", ("ethernet-1/3", "10.10.1.1/24")),
    "csr2": ("10.0.0.2", "49.0001.0100.0000.0002.00", [(201, "172.20.31.1/31"), (202, "172.20.32.1/31")],
             "ethernet-1/11", ("ethernet-1/3", "10.10.2.1/24")),
}
AGGS = {
    #          system       NET                          MTSO-fiber circuits (vlan, agg IP)    inter-agg       h2 link
    "agg-01": ("10.0.0.11", "49.0001.0100.0000.0011.00", [(101, "172.20.1.0/31"), (201, "172.20.31.0/31")],
               (300, "172.20.3.0/31"), ("172.20.11.0/31", "172.20.11.1")),
    "agg-02": ("10.0.0.12", "49.0001.0100.0000.0012.00", [(102, "172.20.2.0/31"), (202, "172.20.32.0/31")],
               (300, "172.20.3.1/31"), ("172.20.12.0/31", "172.20.12.1")),
}
COMMON = """\
set / system name host-name {name}
set / system ssh-server mgmt-netconf admin-state enable
set / system ssh-server mgmt-netconf network-instance mgmt
set / system ssh-server mgmt-netconf port 830
set / system ssh-server mgmt-netconf disable-shell true
set / system netconf-server mgmt admin-state enable
set / system netconf-server mgmt ssh-server mgmt-netconf
set / system aaa authorization role netconf-ops services [ netconf ]
set / system aaa authorization role netconf-ops netconf allowed-operations [ get get-config get-schema edit-config lock unlock validate commit cancel-commit discard-changes close-session ]
set / system configuration role netconf-ops rule / action write
set / system aaa authentication user ansible password Ansible123!
set / system aaa authentication user ansible role [ netconf-ops ]
"""
NI = "set / network-instance default"
ISIS = f"{NI} protocols isis instance i1"


def isis_base(a, sysip, net):
    a(f"set / interface system0 subinterface 0 ipv4 admin-state enable")
    a(f"set / interface system0 subinterface 0 ipv4 address {sysip}/32")
    a(f"{NI} type default")
    a(f"{NI} interface system0.0")
    a(f"{ISIS} admin-state enable")
    a(f"{ISIS} level-capability L2")
    a(f"{ISIS} max-ecmp-paths 8")
    a(f"{ISIS} net [ {net} ]")
    a(f"{ISIS} ipv4-unicast admin-state enable")
    a(f"{ISIS} level 2 metric-style wide")
    a(f"{ISIS} interface system0.0 passive true")
    a(f"{ISIS} interface system0.0 ipv4-unicast admin-state enable")


def circuit(a, port, vlan, ip):
    sub = f"set / interface {port} subinterface {vlan}"
    a(f"set / interface {port} admin-state enable")
    a(f"set / interface {port} vlan-tagging true")
    a(f"{sub} vlan encap single-tagged vlan-id {vlan}")
    a(f"{sub} ipv4 admin-state enable")
    a(f"{sub} ipv4 address {ip}")
    a(f"{NI} interface {port}.{vlan}")
    a(f"{ISIS} interface {port}.{vlan} circuit-type point-to-point")
    a(f"{ISIS} interface {port}.{vlan} ipv4-unicast admin-state enable")
    a(f"{ISIS} interface {port}.{vlan} level 2 metric 10")


def lan(a, port, ip, passive):
    a(f"set / interface {port} admin-state enable")
    a(f"set / interface {port} subinterface 0 ipv4 admin-state enable")
    a(f"set / interface {port} subinterface 0 ipv4 address {ip}")
    a(f"{NI} interface {port}.0")
    if passive:
        a(f"{ISIS} interface {port}.0 passive true")
        a(f"{ISIS} interface {port}.0 ipv4-unicast admin-state enable")


def gen_csr(name, sysip, net, circuits, new_port, lanp):
    out = [COMMON.format(name=name)]
    a = out.append
    isis_base(a, sysip, net)
    for vlan, ip in circuits:                       # both VLANs on the one old fiber
        circuit(a, "ethernet-1/1", vlan, ip)
    a(f"set / interface {new_port} admin-state enable")   # new fiber: cabled, lit, unconfigured
    a(f"set / interface {new_port} vlan-tagging true")
    lan(a, *lanp, passive=True)
    return "\n".join(out) + "\n"


def gen_agg(name, sysip, net, circuits, interagg, h2):
    out = [COMMON.format(name=name)]
    a = out.append
    isis_base(a, sysip, net)
    for vlan, ip in circuits:                       # every site's VLAN on the one MTSO fiber from the telco
        circuit(a, "ethernet-1/1", vlan, ip)
    circuit(a, "ethernet-1/2", *interagg)
    lan(a, "ethernet-1/3", h2[0], passive=False)
    a(f"{NI} next-hop-groups group h2 nexthop 1 ip-address {h2[1]}")
    a(f"{NI} static-routes route 10.20.1.2/32 next-hop-group h2")
    a(f"{NI} static-routes route 10.20.1.2/32 admin-state enable")
    a("set / routing-policy policy export-static default-action policy-result reject")
    a("set / routing-policy policy export-static statement 10 match protocol static")
    a("set / routing-policy policy export-static statement 10 action policy-result accept")
    a(f"{ISIS} export-policy export-static")
    return "\n".join(out) + "\n"


if __name__ == "__main__":
    import pathlib
    d = pathlib.Path(__file__).resolve().parent.parent / "configs"
    for f in d.glob("*.cfg"):
        f.unlink()
    for name, n in CSRS.items():
        (d / f"{name}.cfg").write_text(gen_csr(name, *n)); print("wrote", d / f"{name}.cfg")
    for name, n in AGGS.items():
        (d / f"{name}.cfg").write_text(gen_agg(name, *n)); print("wrote", d / f"{name}.cfg")
