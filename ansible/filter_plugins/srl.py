"""Small helpers to read SR Linux NETCONF replies, plan an uplift phase and diff two datastores."""
import difflib
from lxml import etree

ISIS = "urn:nokia.com:srlinux:isis:isis"
INTF = "urn:nokia.com:srlinux:chassis:interfaces"


def _root(xml):
    if isinstance(xml, bytes):
        return etree.fromstring(xml)
    return etree.fromstring(xml.encode() if isinstance(xml, str) else xml)


def isis_adjacencies(xml):
    """{subinterface: state} for every IS-IS interface that has an adjacency."""
    out = {}
    for intf in _root(xml).iter(f"{{{ISIS}}}interface"):
        name = intf.findtext(f"{{{ISIS}}}interface-name")
        for adj in intf.findall(f"{{{ISIS}}}adjacency"):
            out[name] = adj.findtext(f"{{{ISIS}}}state")
    return out


def adj_up(xml):
    """Sorted list of subinterfaces with an IS-IS adjacency in state up."""
    return sorted(k for k, v in isis_adjacencies(xml).items() if v == "up")


def isis_metrics(xml):
    """{subinterface: level-2 metric} from an IS-IS config subtree."""
    out = {}
    for intf in _root(xml).iter(f"{{{ISIS}}}interface"):
        name = intf.findtext(f"{{{ISIS}}}interface-name")
        for lvl in intf.findall(f"{{{ISIS}}}level"):
            if lvl.findtext(f"{{{ISIS}}}level-number") == "2" and lvl.findtext(f"{{{ISIS}}}metric"):
                out[name] = int(lvl.findtext(f"{{{ISIS}}}metric"))
    return out


def out_packets(xml):
    """{subinterface: out-packets} from an interfaces state subtree."""
    out = {}
    for intf in _root(xml).iter(f"{{{INTF}}}interface"):
        name = intf.findtext(f"{{{INTF}}}name")
        for sub in intf.findall(f"{{{INTF}}}subinterface"):
            v = sub.findtext(f"{{{INTF}}}statistics/{{{INTF}}}out-packets")
            if v is not None:
                out[f"{name}.{sub.findtext(f'{{{INTF}}}index')}"] = int(v)
    return out


def out_pps(xml0, xml1, secs):
    """{subinterface: out pps} between two counter reads `secs` apart."""
    a, b = out_packets(xml0), out_packets(xml1)
    secs = float(secs) or 1.0
    return {k: round(max(b[k] - a.get(k, b[k]), 0) / secs, 1) for k in b}


def end_rates(hosts, pps_list, subs_list):
    """{"host sub": out-pps} for each host's list of subinterfaces (0 if the subinterface does not exist)."""
    out = {}
    for h, pps, subs in zip(hosts, pps_list, subs_list):
        for s in subs or []:
            out[f"{h} {s}"] = float((pps or {}).get(s, 0))
    return out


def site_plan(site_uplinks, links, spec, metric_normal, metric_drained, port_override="", metric_override=""):
    """Resolve a phase spec (group_vars `phases`) against this node's side of the given links of one site.

    Returns {"ensure": [{link, circuit, port, vlan, ip, metric, sub}], "remove": [{link, circuit, port, vlan, sub}]}.
    port_override replaces the 'home' port (rescue test); metric_override forces the metric of every ensure.
    """
    metrics = {"normal": int(metric_normal), "drained": int(metric_drained)}
    plan = {"ensure": [], "remove": []}
    for link in links:
        u = site_uplinks[link]
        ports = {"old": u["old"]["port"], "new": u["new"]["port"], "home": port_override or u["home_port"]}
        for e in spec.get("ensure", []):
            c = u[e["circuit"]]
            p = ports[e["port"]]
            m = metrics[metric_override or e["metric"]]
            plan["ensure"].append({"link": link, "circuit": e["circuit"], "port": p, "vlan": c["vlan"],
                                   "ip": c["ip"], "metric": m, "sub": f"{p}.{c['vlan']}", "peer": u["peer"]})
    ensured = {e["sub"] for e in plan["ensure"]}
    for link in links:
        u = site_uplinks[link]
        ports = {"old": u["old"]["port"], "new": u["new"]["port"], "home": port_override or u["home_port"]}
        for r in spec.get("remove", []):
            c = u[r["circuit"]]
            sub = f"{ports[r['port']]}.{c['vlan']}"
            if sub in ensured or any(x["sub"] == sub for x in plan["remove"]):
                continue                                # never remove what this phase ensures
            plan["remove"].append({"link": link, "circuit": r["circuit"], "port": ports[r["port"]],
                                   "vlan": c["vlan"], "sub": sub})
    return plan


def site_subs(site_uplinks, links, circuit):
    """This node's subinterface names for `circuit` (old | new | homed) on the given links of one site."""
    out = []
    for link in links:
        u = site_uplinks[link]
        if circuit == "homed":
            out.append(f"{u['home_port']}.{u['new']['vlan']}")
        else:
            out.append(f"{u[circuit]['port']}.{u[circuit]['vlan']}")
    return out


def site_ports(site_uplinks, links, port_override=""):
    """Every port the given links of one site can live on, on this node."""
    ps = set()
    for link in links:
        u = site_uplinks[link]
        ps |= {u["old"]["port"], u["new"]["port"], port_override or u["home_port"]}
    return sorted(ps)


def _lines(xml):
    root = _root(xml)
    data = root.find("{urn:ietf:params:xml:ns:netconf:base:1.0}data")
    node = data if data is not None else root
    text = "".join(etree.tostring(c, pretty_print=True).decode() for c in node)
    return [l.rstrip() for l in text.splitlines() if l.strip()]


def xml_diff(running, candidate, context=4):
    """Unified diff running -> candidate, as a list of lines (empty list = no change)."""
    return list(difflib.unified_diff(_lines(running), _lines(candidate),
                                     "running", "candidate", n=context, lineterm=""))


class FilterModule:
    def filters(self):
        return {"isis_adjacencies": isis_adjacencies, "adj_up": adj_up, "isis_metrics": isis_metrics,
                "xml_diff": xml_diff, "out_packets": out_packets, "out_pps": out_pps, "end_rates": end_rates,
                "site_plan": site_plan, "site_subs": site_subs, "site_ports": site_ports}
