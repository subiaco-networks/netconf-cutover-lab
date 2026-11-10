#!/usr/bin/env python3
"""Generate SR Linux startup configs (flat CLI) for the cutover lab."""
NODES = {
    "csr1": {"sys": "10.0.0.1", "net": "49.0001.0100.0000.0001.00",
             "links": [("ethernet-1/1", 100, "172.20.1.1/31"),    # uplink A
                       ("ethernet-1/2", 200, "172.20.2.1/31")],   # uplink B
             "spares": ["ethernet-1/11", "ethernet-1/12"],
             "lan": ("ethernet-1/3", "10.10.1.1/24"), "h2": None},
    "agg-01": {"sys": "10.0.0.11", "net": "49.0001.0100.0000.0011.00",
               "links": [("ethernet-1/1", 100, "172.20.1.0/31"),  # uplink A
                         ("ethernet-1/2", 300, "172.20.3.0/31")], # inter-agg
               "spares": ["ethernet-1/11"],
               "lan": ("ethernet-1/3", "172.20.11.0/31"), "h2": "172.20.11.1"},
    "agg-02": {"sys": "10.0.0.12", "net": "49.0001.0100.0000.0012.00",
               "links": [("ethernet-1/1", 200, "172.20.2.0/31"),  # uplink B
                         ("ethernet-1/2", 300, "172.20.3.1/31")], # inter-agg
               "spares": ["ethernet-1/11"],
               "lan": ("ethernet-1/3", "172.20.12.0/31"), "h2": "172.20.12.1"},
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

def gen(name, n):
    out = [COMMON.format(name=name)]
    a = out.append
    ni = "set / network-instance default"
    isis = f"{ni} protocols isis instance i1"
    a(f"set / interface system0 subinterface 0 ipv4 admin-state enable")
    a(f"set / interface system0 subinterface 0 ipv4 address {n['sys']}/32")
    a(f"{ni} type default")
    a(f"{ni} interface system0.0")
    for port, vlan, ip in n["links"]:
        sub = f"set / interface {port} subinterface {vlan}"
        a(f"set / interface {port} admin-state enable")
        a(f"set / interface {port} vlan-tagging true")
        a(f"{sub} vlan encap single-tagged vlan-id {vlan}")
        a(f"{sub} ipv4 admin-state enable")
        a(f"{sub} ipv4 address {ip}")
        a(f"{ni} interface {port}.{vlan}")
        a(f"{isis} interface {port}.{vlan} circuit-type point-to-point")
        a(f"{isis} interface {port}.{vlan} ipv4-unicast admin-state enable")
        a(f"{isis} interface {port}.{vlan} level 2 metric 10")
    for port in n["spares"]:          # "new 10G" ports: cabled, up, unconfigured
        a(f"set / interface {port} admin-state enable")
        a(f"set / interface {port} vlan-tagging true")
    port, ip = n["lan"]
    a(f"set / interface {port} admin-state enable")
    a(f"set / interface {port} subinterface 0 ipv4 admin-state enable")
    a(f"set / interface {port} subinterface 0 ipv4 address {ip}")
    a(f"{ni} interface {port}.0")
    a(f"{isis} admin-state enable")
    a(f"{isis} level-capability L2")
    a(f"{isis} max-ecmp-paths 8")
    a(f"{isis} net [ {n['net']} ]")
    a(f"{isis} ipv4-unicast admin-state enable")
    a(f"{isis} level 2 metric-style wide")
    a(f"{isis} interface system0.0 passive true")
    a(f"{isis} interface system0.0 ipv4-unicast admin-state enable")
    if n["h2"] is None:               # csr1: advertise the h1 LAN
        a(f"{isis} interface {port}.0 passive true")
        a(f"{isis} interface {port}.0 ipv4-unicast admin-state enable")
    else:                             # aggs: static /32 to h2's loopback, exported into IS-IS
        a(f"{ni} next-hop-groups group h2 nexthop 1 ip-address {n['h2']}")
        a(f"{ni} static-routes route 10.20.1.2/32 next-hop-group h2")
        a(f"{ni} static-routes route 10.20.1.2/32 admin-state enable")
        a("set / routing-policy policy export-static default-action policy-result reject")
        a("set / routing-policy policy export-static statement 10 match protocol static")
        a("set / routing-policy policy export-static statement 10 action policy-result accept")
        a(f"{isis} export-policy export-static")
    return "\n".join(out) + "\n"

if __name__ == "__main__":
    import pathlib
    d = pathlib.Path(__file__).resolve().parent.parent / "configs"
    for name, n in NODES.items():
        (d / f"{name}.cfg").write_text(gen(name, n))
        print("wrote", d / f"{name}.cfg")
