#!/usr/bin/python
"""PT NGFW 1.11.1 lab policy. Independent implementation of its API v2 contract.

No third-party runtime dependencies outside Ansible. Never deletes objects, touches
management interfaces, retries writes, or publishes a snapshot during reconcile.
"""
from __future__ import annotations

import copy
import hashlib
import http.cookiejar
import ipaddress
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

DOCUMENTATION = r'''
---
module: pt_ngfw_policy
short_description: Reconcile an isolated PT NGFW 1.11.1 ACL and NAT test policy
description:
  - Creates lab zones, IPv4 objects, services, ACL and NAT rules using MNGT API v2.
  - Checks exact device, interface and ownership boundaries before mutations.
options:
  config:
    description: Non-secret lab configuration; see docs/ngfw-policy.md.
    type: dict
    required: true
  mode:
    description: Plan offline, discover inventory, check remotely, reconcile candidate, or publish explicitly.
    type: str
    choices: [plan, discover, prepare, check, apply, publish]
    default: plan
  login:
    description: MNGT account supplied privately by Ansible.
    type: str
  password:
    description: MNGT password supplied privately by Ansible.
    type: str
  inventory:
    description: Saved discover inventory for the explicitly selected single-device lab preparation.
    type: dict
author: ["Repository maintainers"]
'''
EXAMPLES = r'''
- name: Plan a large isolated policy without contacting MNGT
  pt_ngfw_policy:
    mode: plan
    config: {acl_count: 1000, nat_count: 200}
'''
RETURN = r'''
summary:
  description: Counts, lifecycle state and number of proposed or completed changes.
  returned: always
  type: dict
plan:
  description: Non-secret desired objects and representative packet tuples.
  returned: always
  type: dict
discovery:
  description: Allowlisted inventory for manual UUID selection; not an apply-readiness verdict.
  returned: when mode is discover
  type: dict
'''

DEFAULTS = {
    "prefix": "auditd-lab", "acl_count": 1000, "nat_count": 200,
    "client": "10.77.10.10", "server": "10.77.20.10", "dnat_vip": "10.77.20.100",
    "left_cidr": "10.77.10.1/24", "right_cidr": "10.77.20.1/24",
    "management_url": "https://192.168.1.88:8443", "ca_file": None,
    "device_group_id": "", "device_group_name": "ngfw-auditd-lab",
    "logical_device_id": "", "physical_device_id": "",
    "left_interface_id": "", "right_interface_id": "",
    "parent_device_group_id": "", "virtual_context_id": "",
    "log_mode": "NO_LOG", "dedicated_mngt_confirmed": False,
    "request_interval": 0.2, "job_timeout": 300,
}
KINDS = {
    "Zone": ("ListZones", "zones"),
    "NetworkObject": ("ListNetworkObjects", "addresses"),
    "Service": ("ListServices", "services"),
    "SecurityRule": ("ListSecurityRules2", "items"),
    "NatRule": ("ListNatRules2", "items"),
}
RULES = {"SecurityRule", "NatRule"}


class PolicyError(Exception):
    """Operator-safe error, never contains an API response or credentials."""


def require(condition, message):
    if not condition:
        raise PolicyError(message)


def configure(raw, live=False, preparing=False):
    require(not (set(raw) - set(DEFAULTS)), "Unknown policy configuration key")
    c = {**DEFAULTS, **raw}
    require(re.fullmatch(r"[a-z][a-z0-9-]{2,23}", c["prefix"]), "Invalid lab prefix")
    for key, lower, upper in [("acl_count", 16, 10000), ("nat_count", 2, 2000)]:
        require(type(c[key]) is int and lower <= c[key] <= upper, f"{key} outside lab bounds")
    require(c["nat_count"] % 2 == 0, "nat_count must be even (equal SNAT/DNAT sets)")
    require(c["log_mode"] in {"NO_LOG", "AT_SESSION_END"}, "Unsupported log mode")
    require(type(c["dedicated_mngt_confirmed"]) is bool, "Confirmation must be boolean")
    require(0.1 <= c["request_interval"] <= 10, "Request interval must be 0.1..10 seconds")
    require(10 <= c["job_timeout"] <= 900, "Job timeout must be 10..900 seconds")
    left, right = (ipaddress.ip_interface(c[k]) for k in ("left_cidr", "right_cidr"))
    private = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
    require(left.version == right.version == 4 and
            all(any(v.network.subnet_of(n) for n in private) for v in (left, right)),
            "Only private IPv4 lab networks are supported")
    require(not left.network.overlaps(right.network), "Lab networks overlap")
    for key, net in [("client", left.network), ("server", right.network), ("dnat_vip", right.network)]:
        addr = ipaddress.ip_address(c[key])
        require(addr in net and addr not in {net.network_address, net.broadcast_address, left.ip, right.ip},
                f"{key} is not a usable lab address")
    require(c["server"] != c["dnat_vip"], "DNAT VIP must differ from receiver")
    try:
        url = urllib.parse.urlsplit(c["management_url"])
        host, port = ipaddress.ip_address(url.hostname), 443 if url.port is None else url.port
    except (ValueError, TypeError):
        raise PolicyError("MNGT must use an approved HTTPS IP origin") from None
    require(url.scheme == "https" and url.username is None and url.password is None and
            not url.query and not url.fragment and url.path in {"", "/"},
            "MNGT must use a plain HTTPS origin without credentials, query or fragment")
    require((host in ipaddress.ip_network("10.77.0.0/24") and port == 443) or
            (str(host) == "192.168.1.88" and port == 8443),
            "MNGT must use isolated 10.77.0.0/24:443 or the approved 192.168.1.88:8443 TLS proxy")
    require(not left.network.overlaps(ipaddress.ip_network("10.77.0.0/24")) and
            not right.network.overlaps(ipaddress.ip_network("10.77.0.0/24")), "Data networks overlap management")
    if live or preparing:
        keys = ["logical_device_id", "physical_device_id", "left_interface_id", "right_interface_id"]
        if not preparing or c["device_group_id"]:
            keys.insert(0, "device_group_id")
        if preparing:
            keys.extend(["parent_device_group_id", "virtual_context_id"])
        for key in keys:
            try:
                uuid.UUID(c[key])
            except (ValueError, TypeError, AttributeError):
                raise PolicyError(f"Set explicit {key} from the lab MNGT") from None
        require(c["left_interface_id"] != c["right_interface_id"], "Lab interface IDs must differ")
    return c


def selection(*refs, user=False):
    return {"kind": ("RULE_USER_KIND_" if user else "RULE_KIND_") + ("LIST" if refs else "ANY"),
            "objects": {"array": list(refs)} if refs else {}}


def build_plan(c):
    """Symbolic references are resolved only after ownership/preflight checks."""
    prefix = c["prefix"]
    owner = f"managed-by=ansible-ngfw-policy; namespace={prefix}; schema=1"
    objects = {k: [] for k in KINDS}

    def add(kind, suffix, **fields):
        name = f"{prefix}-{suffix}"
        objects[kind].append({"name": name, "description": owner, **fields})
        return {"ref": name}

    left = add("Zone", "left")
    right = add("Zone", "right")
    client, server, vip = [add("NetworkObject", key, value={"inet": {"inet": c[key] + "/32"}})
                           for key in ("client", "server", "dnat_vip")]

    def service(suffix, ports, protocol=6):
        return add("Service", suffix, protocol=protocol, srcPorts=[],
                   dstPorts=[{"singlePort": {"port": p}} if isinstance(p, int) else
                             {"portRange": {"from": p[0], "to": p[1]}} for p in ports])

    def acl(suffix, svc, action="ALLOW", dest=None):
        return add("SecurityRule", suffix, sourceZone=selection(left), destinationZone=selection(right),
                   sourceAddr=selection(client), destinationAddr=selection(*(dest or [server])),
                   sourceUser=selection(user=True), service=selection(*svc), application=selection(),
                   urlCategory=selection(), action="SECURITY_RULE_ACTION_" + action,
                   logMode="SECURITY_RULE_LOG_MODE_" + c["log_mode"], enabled=True)

    for label, proto, port in [("iperf-tcp", 6, 5201), ("iperf-udp", 17, 5201),
                               ("http", 6, 8080), ("short-tcp", 6, 9000), ("dns", 17, 5353)]:
        acl("base-" + label, [service(label, [port], proto)])
    half = c["nat_count"] // 2
    acl("allow-snat", [service("snat-range", [(20000, 20000 + half - 1)])])
    acl("allow-dnat", [service("dnat-range", [(30000, 30000 + half - 1), 8080])], dest=[server, vip])
    probes = []
    scale = c["acl_count"] - 8
    for i in range(scale):
        port = 10000 + i
        action = "DROP" if i % 10 == 9 else "ALLOW"
        ref = acl(f"acl-{i:05d}", [service(f"tcp-{port}", [port])], action)
        if i in {0, scale // 2, scale - 1, 9}:
            probes.append({"rule": ref["ref"], "kind": "acl", "target": c["server"],
                           "port": port, "expect": action.lower(), "peer": c["client"]})
    acl("deny-rest", [], "DROP", dest=[server, vip])
    for kind in ("snat", "dnat"):
        for i in range(half):
            port = (20000 if kind == "snat" else 30000) + i
            svc = service(f"tcp-{port}", [port])
            fields = dict(sourceZone=selection(left), destinationZone=selection(right),
                          sourceAddr=selection(client), destinationAddr=selection(server if kind == "snat" else vip),
                          service=selection(svc), enabled=True,
                          srcTranslationType="NAT_SOURCE_TRANSLATION_TYPE_" + ("DYNAMIC_IP_PORT" if kind == "snat" else "NONE"),
                          srcTranslationAddrType="NAT_SOURCE_TRANSLATION_ADDRESS_TYPE_" + ("INTERFACE" if kind == "snat" else "NONE"),
                          dstTranslationType="NAT_DESTINATION_TRANSLATION_TYPE_" + ("NONE" if kind == "snat" else "ADDRESS_POOL"))
            if kind == "snat":
                fields["srcTranslatedPort"] = {"portRange": {"from": 1024, "to": 65535}}
            else:
                fields.update(dstTranslatedAddress=[server], dstTranslatedPort=8080)
            ref = add("NatRule", f"{kind}-{i:05d}", **fields)
            if i in {0, half // 2, half - 1}:
                probes.append({"rule": ref["ref"], "kind": kind,
                               "target": c["server" if kind == "snat" else "dnat_vip"], "port": port,
                               "expect": "allow", "peer": str(ipaddress.ip_interface(c["right_cidr"]).ip) if kind == "snat" else c["client"]})
    return {"format": 1, "owner": owner, "counts": {k: len(v) for k, v in objects.items()},
            "objects": objects, "probes": probes,
            "receiver_ranges": [[10000, 10000 + scale - 1], [20000, 20000 + half - 1]],
            "traffic": {"client": c["client"], "receiver": c["server"], "dnat_vip": c["dnat_vip"]},
            "warning": "Candidate configuration is not installed policy or packet-hit evidence."}


def resolve(value, ids):
    if isinstance(value, dict):
        if set(value) == {"ref"}:
            return ids[value["ref"]]
        return {k: resolve(v, ids) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, ids) for v in value]
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise PolicyError("MNGT redirect refused")


class API:
    def __init__(self, c):
        self.url = c["management_url"].rstrip("/") + "/api/v2/"
        self.interval = c["request_interval"]
        self.last = 0.0
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=c["ca_file"])),
            urllib.request.HTTPCookieProcessor(self.cookies))

    def call(self, operation, body):
        time.sleep(max(0, self.interval - (time.monotonic() - self.last)))
        request = urllib.request.Request(self.url + operation, json.dumps(body).encode(),
                                         {"Content-Type": "application/json"}, method="POST")
        try:
            with self.opener.open(request, timeout=15) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
            require(len(raw) <= 16 * 1024 * 1024, f"{operation}: response too large")
            result = json.loads(raw)
            require(isinstance(result, dict), f"{operation}: invalid response")
            require(not result.get("error") and not result.get("code"), f"{operation}: API error (body withheld)")
            return result
        except urllib.error.HTTPError as exc:
            raise PolicyError(f"{operation}: HTTP {exc.code}; no automatic write retry") from None
        except (urllib.error.URLError, OSError, ValueError):
            raise PolicyError(f"{operation}: transport/TLS/JSON failure; no automatic write retry") from None
        finally:
            self.last = time.monotonic()


def pages(api, operation, key, body=None, cursor=False):
    result, seen_ids, seen_cursors = [], set(), set()
    request = {**(body or {}), "limit": 200}
    if not cursor:
        request["offset"] = 0
    for _ in range(1000):
        data = api.call(operation, request)
        items = data.get(key)
        require(isinstance(items, list), f"{operation}: missing {key}")
        for row in items:
            require(isinstance(row, dict) and row.get("id") and row["id"] not in seen_ids,
                    f"{operation}: duplicate/missing ID or unstable pagination")
            seen_ids.add(row["id"])
        result.extend(items)
        if cursor:
            token = data.get("nextCursor")
            if not token:
                return result
            require(token not in seen_cursors, f"{operation}: cursor loop")
            seen_cursors.add(token)
            request["cursor"] = token
        else:
            if len(items) < request["limit"]:
                return result
            request["offset"] += len(items)
    raise PolicyError(f"{operation}: page limit exceeded")


def normalized(kind, row):
    out = copy.deepcopy(row)
    if kind == "NetworkObject":
        out["value"] = {"inet": {"inet": str(ipaddress.ip_network(row["inet"], strict=False))}}
    if kind == "Service":
        out.setdefault("srcPorts", [])
        out.setdefault("dstPorts", [])
    if kind in RULES:
        for field in ("sourceZone", "sourceAddr", "sourceUser", "destinationZone", "destinationAddr", "service", "application", "urlCategory"):
            if field in out:
                v = out[field]
                if isinstance(v.get("objects"), list):
                    ids = [x["id"] for x in v["objects"]]
                    v["objects"] = {"array": ids} if ids else {}
        for field in ("srcTranslatedAddress", "dstTranslatedAddress"):
            if field in out:
                out[field] = [x["id"] if isinstance(x, dict) else x for x in out[field]]
    return out


def differs(kind, current, wanted):
    def canonical(value):
        if isinstance(value, dict):
            return {k: canonical(v) for k, v in value.items()}
        if isinstance(value, list):
            return sorted((canonical(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
        return value
    actual = normalized(kind, current)
    # These are sets of object references/ports, not the ordered rule table.
    return any(canonical(actual.get(k)) != canonical(v) for k, v in wanted.items())


def flatten(groups):
    for group in groups:
        yield group
        yield from flatten(group.get("subgroups", []))


def discover(api, c):
    """Read inventory without selecting targets or exposing full API responses."""
    groups = list(flatten(api.call("GetDeviceGroupsTree", {}).get("groups", [])))
    devices = pages(api, "ListPhysicalDevices", "physicalDevices")
    contexts = pages(api, "ListVirtualContexts", "virtualContexts")
    interfaces = pages(api, "ListVirtualInterfaces", "virtualInterfaces")

    def pick(row, *keys):
        return {k: copy.deepcopy(row[k]) for k in keys if k in row}

    def ref(row, key):
        return pick(row.get(key) or {}, "id", "name")

    inventory = {
        "format": 1,
        "device_groups": [pick(g, "id", "name", "parentId") for g in groups],
        "physical_devices": [
            {**pick(d, "id", "name", "address", "productVersion", "softwareVersion", "connectionState"),
             "logicalDevice": ref(d, "logicalDevice")} for d in devices],
        "virtual_contexts": [
            {**pick(v, "id", "name", "isDefault", "defaultIpsProfileId"),
             "deviceGroup": ref(v, "deviceGroup"), "logicalDevice": ref(v, "logicalDevice")}
            for v in contexts],
        "virtual_interfaces": [
            {**pick(v, "id", "name", "enabled", "mode", "inet", "inet6"),
             "virtualContext": ref(v, "virtualContext"),
             "deviceGroup": ref(v.get("virtualContext") or {}, "deviceGroup"),
             "zone": ref(v, "zone"), "virtualRouter": ref(v, "virtualRouter")}
            for v in interfaces],
    }
    notices = ["Inventory only: select UUIDs explicitly, then run check; no policy readiness or packet proof."]
    if not any(g.get("name") == c["device_group_name"] and g.get("parentId") for g in groups):
        notices.append("Named non-root lab group is absent; discovery never creates a group or moves a context.")
    roots = {g["id"] for g in groups if not g.get("parentId")}
    if any((v.get("deviceGroup") or {}).get("id") in roots for v in contexts):
        notices.append("A context belongs to a root group; apply requires a dedicated non-root lab group. Preserve existing baseline.")
    if any(v.get("defaultIpsProfileId") for v in contexts):
        notices.append("A context has a default IPS profile; inspect it before selecting the baseline.")
    if len(devices) != 1:
        notices.append("Inventory does not contain exactly one physical device; global publish is not permitted.")
    inventory["notices"] = notices
    return inventory


def one(rows, message):
    require(len(rows) == 1, message)
    return rows[0]


def select_lab_config(c, inventory):
    """Resolve only the agreed AuditD lab; never select an arbitrary first device."""
    require(isinstance(inventory, dict) and inventory.get("format") == 1, "Provide saved discovery inventory")
    device = one(inventory.get("physical_devices", []), "Preparation requires exactly one discovered NGFW")
    require(device.get("name") == "pt-ngfw-auditd" and device.get("address") == "10.77.0.20",
            "Preparation is restricted to pt-ngfw-auditd at 10.77.0.20")
    context = one(inventory.get("virtual_contexts", []), "Preparation requires exactly one discovered context")
    require(context.get("name") == "Default" and context.get("isDefault") is True and
            context.get("logicalDevice", {}).get("id") == device.get("logicalDevice", {}).get("id"),
            "Discovered Default context/device mismatch")
    groups = inventory.get("device_groups", [])
    parent = one([g for g in groups if g.get("name") == "Global" and not g.get("parentId")],
                 "Select the discovered Global root explicitly")
    target = [g for g in groups if g.get("name") == c["device_group_name"]]
    require(len(target) <= 1, "Ambiguous discovered lab group")
    selected = {"parent_device_group_id": parent["id"], "virtual_context_id": context["id"],
                "physical_device_id": device["id"], "logical_device_id": device["logicalDevice"]["id"],
                "device_group_id": target[0]["id"] if target else ""}
    for side in ("left", "right"):
        iface = one([v for v in inventory.get("virtual_interfaces", [])
                     if v.get("inet") == [c[f"{side}_cidr"]] and
                     v.get("virtualContext", {}).get("id") == context["id"]],
                    f"Select the unique discovered {side} interface by exact lab CIDR/context")
        selected[f"{side}_interface_id"] = iface["id"]
    for key, value in selected.items():
        require(not c[key] or c[key] == value, f"Configured {key} conflicts with discovery")
    # Preparation never carries a standing authorization for a GLOBAL push.
    return configure({**c, **selected, "dedicated_mngt_confirmed": False}, preparing=True)


def topology(api, c, allowed_groups):
    devices = pages(api, "ListPhysicalDevices", "physicalDevices")
    ours = [d for d in devices if d["id"] == c["physical_device_id"]]
    require(len(ours) == 1 and ours[0].get("logicalDevice", {}).get("id") == c["logical_device_id"],
            "Physical/logical device identity mismatch")
    version = ours[0].get("productVersion", "") or ours[0].get("softwareVersion", "")
    require(re.match(r"^1\.11\.1(?:\D|$)", version), "Adapter requires PT NGFW 1.11.1")
    interfaces = pages(api, "ListVirtualInterfaces", "virtualInterfaces", {"logicalDeviceId": c["logical_device_id"]})
    bindings = {}
    for side in ("left", "right"):
        rows = [v for v in interfaces if v["id"] == c[f"{side}_interface_id"]]
        require(len(rows) == 1, f"Missing {side} lab interface")
        v = rows[0]
        addresses = {str(ipaddress.ip_interface(x)) for x in v.get("inet", [])}
        require(addresses == {str(ipaddress.ip_interface(c[f"{side}_cidr"]))} and not v.get("inet6") and
                v.get("enabled") is True and v.get("mode") == "VIRTUAL_INTERFACE_MODE_ROUTING",
                f"{side} interface must already have only its intended lab IPv4 address, enabled in routing mode")
        require(v.get("virtualContext", {}).get("deviceGroup", {}).get("id") in allowed_groups,
                f"{side} interface does not belong to the selected lab device group")
        require(not c["virtual_context_id"] or v.get("virtualContext", {}).get("id") == c["virtual_context_id"],
                f"{side} interface context identity mismatch")
        require(not v.get("virtualContext", {}).get("defaultIpsProfileId"),
                f"{side} lab context has a default IPS profile; baseline must be explicitly agreed first")
        bindings[side] = v
    router = bindings["left"].get("virtualRouter", {}).get("id")
    require(router and router == bindings["right"].get("virtualRouter", {}).get("id"), "Lab interfaces must share an existing virtual router")
    return bindings, devices, interfaces


def parent_rules(api, group_id):
    result = {}
    for kind in sorted(RULES):
        operation, key = KINDS[kind]
        rows = pages(api, operation, key, {"deviceGroupId": group_id}, cursor=True)
        result[kind] = {r["id"]: {k: v for k, v in r.items() if k not in {
            "metrics", "createdAt", "updatedAt", "globalPosition"}} for r in rows}
    return result


def interface_fingerprint(interfaces):
    fields = ("id", "name", "enabled", "mode", "inet", "inet6", "ports", "macAddress", "vlanId",
              "isTcpAdjustMssEnabled", "isInheritMtu", "mtu")
    return {v["id"]: {**{k: v.get(k) for k in fields},
                       **{k: (v.get(k) or {}).get("id") for k in ("zone", "virtualRouter", "virtualContext")}}
            for v in interfaces}


def prepare(api, c, plan, write=False):
    """Create an owned child and move only the agreed context. Never publishes."""
    groups = list(flatten(api.call("GetDeviceGroupsTree", {}).get("groups", [])))
    parent = one([g for g in groups if g["id"] == c["parent_device_group_id"]], "Missing parent group")
    require(parent.get("name") == "Global" and not parent.get("parentId"), "Expected Global root changed")
    targets = [g for g in groups if g.get("name") == c["device_group_name"]]
    require(len(targets) <= 1, "Duplicate laboratory group name")
    target = targets[0] if targets else None
    require(not c["device_group_id"] or (target and target["id"] == c["device_group_id"]), "Lab group identity changed")
    if target:
        require(target.get("parentId") == parent["id"] and target.get("description") == plan["owner"] and
                not target.get("subgroups"), "Existing lab group is not an owned, isolated direct child")
    allowed = {parent["id"]} | ({target["id"]} if target else set())
    bindings, devices, interfaces = topology(api, c, allowed)
    require(len(devices) == 1 and devices[0].get("name") == "pt-ngfw-auditd" and
            devices[0].get("address") == "10.77.0.20", "Live single-device lab identity changed")
    contexts = pages(api, "ListVirtualContexts", "virtualContexts")
    context = one(contexts, "Live MNGT must contain exactly the agreed context")
    require(context.get("id") == c["virtual_context_id"] and context.get("name") == "Default" and
            context.get("isDefault") is True and context.get("logicalDevice", {}).get("id") == c["logical_device_id"] and
            context.get("deviceGroup", {}).get("id") in allowed and not context.get("defaultIpsProfileId"),
            "Live Default context identity/group/IPS changed")
    require(all(v.get("virtualContext", {}).get("id") == context["id"] for v in interfaces),
            "Unexpected interface context; preparation stopped")
    # Before creating or moving anything, refuse policy that can shadow the lab.
    before = parent_rules(api, parent["id"])
    for kind in RULES:
        rg = api.call("List" + kind + "Groups", {"deviceGroupId": parent["id"]}).get("ruleGroups", [])
        pre = {g["id"] for g in rg if g.get("precedence") == "RULE_PRECEDENCE_PRE"}
        require(not any(r.get("enabled") and r.get("ruleGroupId") in pre for r in before[kind].values()),
                f"{kind}: active parent PRE policy must be reviewed before preparation")
    if target:
        preflight(api, {**c, "device_group_id": target["id"]}, plan,
                  allowed_groups=allowed)
    changes = []
    if not target:
        changes.append({"kind": "DeviceGroup", "action": "create", "name": c["device_group_name"]})
        if write:
            created = api.call("CreateDeviceGroup", {"name": c["device_group_name"],
                               "parentId": parent["id"], "description": plan["owner"]})
            target_id = created.get("id")
            require(target_id, "CreateDeviceGroup: missing ID; inspect before retry")
            groups = list(flatten(api.call("GetDeviceGroupsTree", {}).get("groups", [])))
            target = one([g for g in groups if g["id"] == target_id], "Created group missing in read-back")
            require(target.get("name") == c["device_group_name"] and target.get("parentId") == parent["id"] and
                    target.get("description") == plan["owner"], "Created group differs in read-back")
            preflight(api, {**c, "device_group_id": target_id}, plan, allowed_groups=allowed)
    target_id = target["id"] if target else ""
    if context["deviceGroup"]["id"] != target_id:
        changes.append({"kind": "VirtualContext", "action": "move-to-lab-group", "id": context["id"]})
        if write:
            api.call("UpdateVirtualContext", {"id": context["id"], "deviceGroupId": target_id})
    if write:
        updated = one(pages(api, "ListVirtualContexts", "virtualContexts"), "Context read-back changed")
        require(updated.get("deviceGroup", {}).get("id") == target_id, "Context move not confirmed in read-back")
        for key in ("id", "name", "description", "isDefault", "defaultIpsProfileId"):
            require(updated.get(key) == context.get(key), "Context fields changed unexpectedly; inspect MNGT")
        _, _, after_interfaces = topology(api, {**c, "device_group_id": target_id}, {target_id})
        require(interface_fingerprint(after_interfaces) == interface_fingerprint(interfaces),
                "Interface configuration changed during preparation; policy NOT published")
        require(parent_rules(api, parent["id"]) == before, "Parent baseline changed; policy NOT published")
        inherited = parent_rules(api, target_id)
        require(all(all(inherited[k].get(i) == row for i, row in rows.items()) for k, rows in before.items()),
                "Parent baseline is not inherited unchanged; policy NOT published")
        preflight(api, {**c, "device_group_id": target_id}, plan)
    return changes, {**c, "device_group_id": target_id}, {
        "parent_rule_counts": {k: len(v) for k, v in before.items()},
        "baseline_sha256": hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(),
        "interfaces_unchanged_verified": write, "baseline_inheritance_verified": write}


def preflight(api, c, plan, allowed_groups=None):
    groups = list(flatten(api.call("GetDeviceGroupsTree", {}).get("groups", [])))
    group = [g for g in groups if g.get("id") == c["device_group_id"]]
    require(len(group) == 1 and group[0].get("name") == c["device_group_name"] and group[0].get("parentId"),
            "Select the named, non-root laboratory device group explicitly")
    bindings, devices, _ = topology(api, c, allowed_groups if allowed_groups is not None else {c["device_group_id"]})
    collections, rule_groups = {}, {}
    for kind, (operation, key) in KINDS.items():
        body = {} if kind == "Zone" else {"deviceGroupId": c["device_group_id"]}
        if kind == "NetworkObject":
            body["objectKinds"] = ["OBJECT_NETWORK_KIND_IPV4_ADDRESS"]
        if kind == "Service":
            body.update(objectKinds=["OBJECT_KIND_OBJECT"])
        rows = pages(api, operation, key, body, cursor=kind in RULES)
        if kind in RULES:
            rg = api.call("List" + kind + "Groups", {"deviceGroupId": c["device_group_id"]}).get("ruleGroups", [])
            own = [r for r in rg if r.get("deviceGroupId") == c["device_group_id"] and r.get("precedence") == "RULE_PRECEDENCE_PRE"]
            require(len(own) == 1, f"{kind}: missing unique laboratory PRE group")
            rule_groups[kind] = own[0]["id"]
            inherited_pre = {r["id"] for r in rg if r.get("precedence") == "RULE_PRECEDENCE_PRE" and r["id"] != own[0]["id"]}
            require(not any(r.get("enabled") and r.get("ruleGroupId") in inherited_pre for r in rows),
                    f"{kind}: inherited PRE rules could shadow the lab policy")
        names = {}
        desired = {x["name"] for x in plan["objects"][kind]}
        for row in rows:
            is_own = row.get("ruleGroupId") == rule_groups.get(kind) if kind in RULES else row.get("deviceGroupId") == c["device_group_id"]
            prefixed = row.get("name", "").startswith(c["prefix"] + "-")
            if kind in RULES and is_own:
                require(prefixed, f"{kind}: foreign rule in dedicated laboratory PRE group")
            if not prefixed:
                continue
            require(is_own and row.get("description") == plan["owner"], f"{kind}: name/ownership collision; refusing to adopt")
            require(row["name"] in desired, f"{kind}: obsolete managed objects; shrink/rename needs explicit cleanup")
            require(row["name"] not in names, f"{kind}: duplicate managed name")
            if kind == "SecurityRule":
                require(not any(row.get(k) for k in ("ipsProfileId", "avProfileId", "icapProfileId")) and
                        not any((row.get("schedule") or {}).values()),
                        "SecurityRule: unexpected inspection profile or schedule on managed baseline rule")
            names[row["name"]] = row
        collections[kind] = names
    return collections, rule_groups, bindings, devices


def synchronize(api, c, plan, write=False):
    collections, groups, bindings, _ = preflight(api, c, plan)
    ids, changes = {}, []
    for kind, wanted_rows in plan["objects"].items():
        for symbolic in wanted_rows:
            wanted = resolve(symbolic, ids)
            current = collections[kind].get(wanted["name"])
            if current is None:
                changes.append({"kind": kind, "name": wanted["name"], "action": "create"})
                if write:
                    body = {**wanted, "deviceGroupId": c["device_group_id"]} if kind not in RULES else {
                        **wanted, "positionRelativeToGroup": {"ruleGroupId": groups[kind], "order": "RELATIVE_GROUP_ORDER_GROUP_END"}}
                    created = api.call("Create" + kind, body)
                    require(isinstance(created.get("id"), str) and created["id"], f"Create{kind}: missing ID; rerun check before any retry")
                    ids[wanted["name"]] = created["id"]
                else:
                    ids[wanted["name"]] = "planned:" + wanted["name"]
            else:
                ids[wanted["name"]] = current["id"]
                if differs(kind, current, wanted):
                    changes.append({"kind": kind, "name": wanted["name"], "action": "update"})
                    if write:
                        body = {**wanted, "id": current["id"]}
                        if kind == "Service" and not wanted["srcPorts"]:
                            body["removeSrcPortsAll"] = True
                        api.call("Update" + kind, body)
        if kind in RULES:
            old_order = [r["name"] for r in sorted(collections[kind].values(), key=lambda r: r["position"])]
            # New rules are appended; repair ordering explicitly, including terminal deny.
            appended = old_order + [r["name"] for r in wanted_rows if r["name"] not in collections[kind]]
            wanted_order = [r["name"] for r in wanted_rows]
            if appended != wanted_order:
                changes.append({"kind": kind, "action": "reorder", "count": len(wanted_order)})
                if write:
                    for name in wanted_order:
                        api.call("Move" + kind, {"id": ids[name], "positionRelativeToGroup": {
                            "ruleGroupId": groups[kind], "order": "RELATIVE_GROUP_ORDER_GROUP_END"}})
    for side, current in bindings.items():
        zone_id = ids[f"{c['prefix']}-{side}"]
        if current.get("zone", {}).get("id") != zone_id:
            changes.append({"kind": "VirtualInterface", "name": side, "action": "bind-zone"})
            if write:
                api.call("UpdateVirtualInterface", {"id": current["id"], "zoneId": zone_id})
    if write:
        remaining = synchronize(api, c, plan, write=False)
        require(not remaining, "Read-back differs from desired candidate; policy NOT published, rerun check")
    return changes


def wait_job(api, operation, key, job_id, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        job = api.call(operation, {"id": job_id}).get(key, {})
        state = job.get("state", {}).get("kind")
        if state == "JOB_STATE_DONE":
            return job
        require(state in {"JOB_STATE_PENDING", "JOB_STATE_IN_PROGRESS"}, f"{operation}: job failed, cancelled or unknown state; inspect MNGT")
        time.sleep(2)
    raise PolicyError(f"{operation}: timeout; job may still run, inspect MNGT before retry")


def publish(api, c, plan):
    require(c["dedicated_mngt_confirmed"] is True,
            "CommitSnapshot/PushSnapshot are GLOBAL: explicitly confirm this MNGT is dedicated to the lab")
    _, _, _, devices = preflight(api, c, plan)
    require(len(devices) == 1 and devices[0]["id"] == c["physical_device_id"], "Global publish requires exactly the expected single lab NGFW")
    require(not synchronize(api, c, plan), "Candidate differs: run apply and inspect read-back before publish")
    committed = api.call("CommitSnapshot", {"name": c["prefix"] + "-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime()),
                                           "description": plan["owner"], "push": False})
    require(committed.get("jobId"), "CommitSnapshot: missing job ID")
    job = wait_job(api, "GetSnapshotCommitJob", "commitJob", committed["jobId"], c["job_timeout"])
    snapshot = job.get("state", {}).get("commitDone", {}).get("snapshotId")
    require(snapshot, "Commit finished without snapshot ID")
    pushed = api.call("PushSnapshot", {"snapshotId": snapshot})
    require(pushed.get("jobId"), "PushSnapshot: missing job ID")
    job = wait_job(api, "GetSnapshotPushJob", "pushJob", pushed["jobId"], c["job_timeout"])
    require(not job.get("failedPushes") and not job.get("failedLogCollectorPushes"), "Snapshot push has failed targets")
    require([r.get("deviceName") for r in job.get("successfulPushes", [])] == [devices[0]["name"]],
            "Snapshot push did not confirm the expected NGFW")
    require(not synchronize(api, c, plan), "Candidate changed during publish; packet evidence not established")
    return {"snapshot_id": snapshot, "commit_job_id": committed["jobId"], "push_job_id": pushed["jobId"]}


def execute(config, mode="plan", login=None, password=None, check_mode=False, api=None, inventory=None):
    require(mode in {"plan", "discover", "prepare", "check", "apply", "publish"}, "Unknown policy mode")
    c = configure(config, live=mode not in {"plan", "discover", "prepare"})
    if mode == "prepare":
        c = select_lab_config(c, inventory)
    plan = build_plan(c)
    digest = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    summary = {"counts": plan["counts"], "state": "offline-plan", "packet_hits_verified": False, "plan_sha256": digest}
    if mode == "plan":
        return {"changed": False, "plan": plan, "summary": summary}
    require(login and password, "Set private MNGT credentials via Ansible; never put them in Git")
    api = api or API(c)
    api.call("Login", {"login": login, "password": password})
    try:
        if mode == "prepare":
            changes, prepared, evidence = prepare(api, c, plan, write=not check_mode)
            summary.update(state="preparation-check" if check_mode else "prepared-candidate",
                           change_count=len(changes), **evidence)
            return {"changed": bool(changes), "plan": plan, "summary": summary,
                    "changes": changes, "prepared_config": prepared}
        if mode == "discover":
            inventory = discover(api, c)
            summary.update(state="inventory-read-only", notices=inventory["notices"],
                           inventory_counts={k: len(inventory[k]) for k in (
                               "device_groups", "physical_devices", "virtual_contexts", "virtual_interfaces")})
            return {"changed": False, "plan": plan, "summary": summary, "discovery": inventory}
        if mode == "publish" and not check_mode:
            summary.update(publish(api, c, plan), state="push-job-confirmed")
            return {"changed": True, "plan": plan, "summary": summary}
        write = mode == "apply" and not check_mode
        changes = synchronize(api, c, plan, write=write)
        summary.update(state="candidate-read-back" if write else "remote-check", change_count=len(changes))
        return {"changed": bool(changes), "plan": plan, "summary": summary, "changes": changes}
    finally:
        # A logout failure must not mask a mutation result or prompt blind retry.
        try:
            api.call("Logout", {})
        except PolicyError:
            pass


def main():
    from ansible.module_utils.basic import AnsibleModule
    module = AnsibleModule(argument_spec={
        "config": {"type": "dict", "required": True},
        "mode": {"type": "str", "choices": ["plan", "discover", "prepare", "check", "apply", "publish"], "default": "plan"},
        "inventory": {"type": "dict"},
        "login": {"type": "str", "no_log": True}, "password": {"type": "str", "no_log": True},
    }, supports_check_mode=True)
    lock = None
    try:
        if module.params["mode"] in {"prepare", "apply", "publish"} and not module.check_mode:
            # The managed host is Linux. This also excludes simultaneous local
            # Ansible runs; the operator must separately avoid UI/API writers.
            import fcntl
            import os
            fd = os.open("/run/lock/ngfw-policy.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            lock = os.fdopen(fd, "w")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PolicyError("Another Ansible policy mutation is running") from None
        module.exit_json(**execute(**module.params, check_mode=module.check_mode))
    except PolicyError as exc:
        module.fail_json(msg=str(exc), partial_changes_possible=module.params["mode"] in {"prepare", "apply", "publish"} and not module.check_mode)
    except Exception:
        # Do not serialize raw exceptions/response bodies that may include secrets.
        module.fail_json(msg="Policy adapter failed; inspect configuration/API contract. No automatic retry.",
                         partial_changes_possible=module.params["mode"] in {"prepare", "apply", "publish"} and not module.check_mode)
    finally:
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    main()
