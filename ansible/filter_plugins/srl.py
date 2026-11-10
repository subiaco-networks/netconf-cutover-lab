"""Small helpers to read SR Linux NETCONF replies and diff two datastores."""
import difflib
from lxml import etree

ISIS = "urn:nokia.com:srlinux:isis:isis"


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


def isis_metrics(xml):
    """{subinterface: level-2 metric} from an IS-IS config subtree."""
    out = {}
    for intf in _root(xml).iter(f"{{{ISIS}}}interface"):
        name = intf.findtext(f"{{{ISIS}}}interface-name")
        for lvl in intf.findall(f"{{{ISIS}}}level"):
            if lvl.findtext(f"{{{ISIS}}}level-number") == "2" and lvl.findtext(f"{{{ISIS}}}metric"):
                out[name] = int(lvl.findtext(f"{{{ISIS}}}metric"))
    return out


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
        return {"isis_adjacencies": isis_adjacencies, "isis_metrics": isis_metrics, "xml_diff": xml_diff}
