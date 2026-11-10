#!/usr/bin/env python3
"""Print the NETCONF server capabilities each node advertises in its <hello>."""
import sys
from ncclient import manager
WANT = ["urn:ietf:params:netconf:capability:candidate:1.0",
        "urn:ietf:params:netconf:capability:confirmed-commit:1.1",
        "urn:ietf:params:netconf:capability:validate:1.1",
        "urn:ietf:params:netconf:capability:rollback-on-error:1.0",
        "urn:ietf:params:netconf:base:1.1"]
for host in sys.argv[1:] or ["172.30.40.11", "172.30.40.21", "172.30.40.22"]:
    with manager.connect(host=host, port=830, username="ansible", password="Ansible123!",
                         hostkey_verify=False, look_for_keys=False, allow_agent=False) as m:
        caps = list(m.server_capabilities)
        print(f"== {host} session-id {m.session_id}")
        for w in WANT:
            print(f"   {'YES' if any(c.startswith(w) for c in caps) else 'NO '}  {w}")
        if "-v" in sys.argv: print("\n".join("   " + c for c in caps if c.startswith("urn:ietf:params:netconf")))
