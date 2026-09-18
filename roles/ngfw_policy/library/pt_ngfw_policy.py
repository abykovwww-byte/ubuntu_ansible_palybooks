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
    description: Plan offline, check remotely, reconcile candidate, or publish explicitly.
    type: str
    choices: [plan, check, apply, publish]
    default: plan
  login:
    description: MNGT account supplied privately by Ansible.
    type: str
  password:
    description: MNGT password supplied privately by Ansible.
    type: str
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
'''

DEFAULTS = {
    "prefix": "auditd-lab", "acl_count": 1000, "nat_count": 200,
    "client": "10.77.10.10", "server": "10.77.20.10", "dnat_vip": "10.77.20.100",
    "left_cidr": "10.77.10.1/24", "right_cidr": "10.77.20.1/24",
    "management_url": "https://10.77.0.10", "ca_file": None,
    "device_group_id": "", "device_group_name": "ngfw-auditd-lab",
    "logical_device_id": "", "physical_device_id": "",
    "left_interface_id": "", "right_interface_id": "",
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


def configure(raw, live=False):
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
    url = urllib.parse.urlsplit(c["management_url"])
    require(url.scheme == "https" and not url.username and not url.password and
            not url.query and not url.fragment and url.path in {"", "/"} and url.port in {None, 443},
            "MNGT must use a plain HTTPS origin on port 443")
    host = ipaddress.ip_address(url.hostname)
    require(host in ipaddress.ip_network("10.77.0.0/24"), "MNGT must be on isolated 10.77.0.0/24")
    require(not left.network.overlaps(ipaddress.ip_network("10.77.0.0/24")) and
            not right.network.overlaps(ipaddress.ip_network("10.77.0.0/24")), "Data networks overlap management")
    if live:
        for key in ("device_group_id", "logical_device_id", "physical_device_id", "left_interface_id", "right_interface_id"):
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


def preflight(api, c, plan):
    groups = list(flatten(api.call("GetDeviceGroupsTree", {}).get("groups", [])))
    group = [g for g in groups if g.get("id") == c["device_group_id"]]
    require(len(group) == 1 and group[0].get("name") == c["device_group_name"] and group[0].get("parentId"),
            "Select the named, non-root laboratory device group explicitly")
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
        require(v.get("virtualContext", {}).get("deviceGroup", {}).get("id") == c["device_group_id"],
                f"{side} interface does not belong to the selected lab device group")
        require(not v.get("virtualContext", {}).get("defaultIpsProfileId"),
                f"{side} lab context has a default IPS profile; baseline must be explicitly agreed first")
        bindings[side] = v
    router = bindings["left"].get("virtualRouter", {}).get("id")
    require(router and router == bindings["right"].get("virtualRouter", {}).get("id"), "Lab interfaces must share an existing virtual router")
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


def execute(config, mode="plan", login=None, password=None, check_mode=False, api=None):
    c = configure(config, live=mode != "plan")
    plan = build_plan(c)
    digest = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    summary = {"counts": plan["counts"], "state": "offline-plan", "packet_hits_verified": False, "plan_sha256": digest}
    if mode == "plan":
        return {"changed": False, "plan": plan, "summary": summary}
    require(login and password, "Set private MNGT credentials via Ansible; never put them in Git")
    api = api or API(c)
    api.call("Login", {"login": login, "password": password})
    try:
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
        "mode": {"type": "str", "choices": ["plan", "check", "apply", "publish"], "default": "plan"},
        "login": {"type": "str", "no_log": True}, "password": {"type": "str", "no_log": True},
    }, supports_check_mode=True)
    lock = None
    try:
        if module.params["mode"] in {"apply", "publish"} and not module.check_mode:
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
        module.fail_json(msg=str(exc), partial_changes_possible=module.params["mode"] in {"apply", "publish"} and not module.check_mode)
    except Exception:
        # Do not serialize raw exceptions/response bodies that may include secrets.
        module.fail_json(msg="Policy adapter failed; inspect configuration/API contract. No automatic retry.",
                         partial_changes_possible=module.params["mode"] in {"apply", "publish"} and not module.check_mode)
    finally:
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    main()
