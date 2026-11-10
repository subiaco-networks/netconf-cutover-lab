#!/usr/bin/env python3
"""nc_get.py HOST running|candidate|state [SUBTREE_XML]  - dump config/state XML (subtree filter)."""
import sys
from ncclient import manager
host, src = sys.argv[1:3]
flt = ("subtree", sys.argv[3]) if len(sys.argv) > 3 else None
with manager.connect(host=host, port=830, username="ansible", password="Ansible123!",
                     hostkey_verify=False, look_for_keys=False, allow_agent=False) as m:
    r = m.get(flt) if src == "state" else m.get_config(source=src, filter=flt)
    print(r.xml)
