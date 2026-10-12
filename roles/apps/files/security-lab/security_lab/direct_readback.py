"""Fail-closed validation of the dedicated direct-worker nft JSON inventory.

Validating a fake document is unit coverage only. Actual nft JSON and kernel
enforcement still require the owned receiving-side laboratory qualification.
"""
from __future__ import annotations

import ipaddress

from .contracts import Denied
from .direct_policy import DirectExecution

TABLE = "security_lab_direct"


def _require(condition, message):
    if not condition:
        raise Denied("Direct nft read-back: " + message)


def _members(value):
    if isinstance(value, dict) and set(value) == {"set"}:
        value = value["set"]
    return value if isinstance(value, list) else [value]


def _rule_signature(rule):
    expr = rule.get("expr")
    _require(isinstance(expr, list) and bool(expr) and expr[-1] == {"accept": None}, "rule must end in exactly one accept")
    state, endpoint, ndp = None, None, set()
    protocol_guards = {}
    for item in expr[:-1]:
        _require(isinstance(item, dict) and set(item) == {"match"}, "unexpected rule expression")
        match = item["match"]
        _require(isinstance(match, dict) and set(match) == {"op", "left", "right"}
                 and match["op"] in {"==", "in"}, "unexpected match operator")
        left, right = match["left"], match["right"]
        if left == {"ct": {"key": "state"}}:
            _require(state is None, "duplicate conntrack match")
            values = _members(right)
            if all(type(value) is int for value in values):
                # nft1.0.6 numeric output exposes individual state bits, not a
                # permissive bitmask. Never expand unknown or combined masks.
                _require(all(value in {2, 8} for value in values), "unknown conntrack state bits")
                values = [{2: "established", 8: "new"}[value] for value in values]
            else:
                _require(all(type(value) is str for value in values), "mixed or invalid conntrack state encoding")
            _require(len(values) == len(set(values)), "duplicate conntrack states")
            state = frozenset(values)
        elif isinstance(left, dict) and set(left) == {"concat"}:
            _require(endpoint is None and isinstance(left["concat"], list) and len(left["concat"]) == 2,
                     "invalid endpoint concatenation")
            candidates = [
                ([{"payload": {"protocol": family, "field": ip_field}},
                  {"payload": {"protocol": "tcp", "field": port_field}}], (direction, name))
                for family, name in (("ip", "allowed4"), ("ip6", "allowed6"))
                for direction, ip_field, port_field in (("input", "saddr", "sport"), ("output", "daddr", "dport"))
            ]
            # Compare objects directly, avoiding dictionary insertion-order
            # dependence in JSON input and accepting no additional payload key.
            endpoint = next((signature for payloads, signature in candidates
                             if left["concat"] == payloads), None)
            _require(endpoint is not None and right == "@" + endpoint[1], "endpoint must reference its exact family set")
        elif left == {"meta": {"key": "l4proto"}}:
            _require("l4proto" not in protocol_guards, "duplicate protocol guard")
            if type(right) is int:
                right = {6: "tcp", 58: "ipv6-icmp"}.get(right)
            _require(type(right) is str and right in {"tcp", "ipv6-icmp", "icmpv6"}, "unknown protocol guard")
            protocol_guards["l4proto"] = right
            if right != "tcp":
                ndp.add("icmpv6")
        elif left == {"meta": {"key": "nfproto"}}:
            _require("nfproto" not in protocol_guards, "duplicate family guard")
            if type(right) is int:
                right = {2: "ipv4", 10: "ipv6"}.get(right)
            _require(type(right) is str and right in {"ipv4", "ipv6"}, "unknown family guard")
            protocol_guards["nfproto"] = right
        elif left == {"payload": {"protocol": "ip6", "field": "hoplimit"}} and type(right) is int and right == 255:
            _require("hoplimit" not in ndp, "duplicate hop-limit match")
            ndp.add("hoplimit")
        elif left == {"payload": {"protocol": "icmpv6", "field": "type"}}:
            values = _members(right)
            _require("types" not in ndp and len(values) == 2 and
                     ((all(type(value) is int for value in values) and set(values) == {135, 136})
                      or (all(type(value) is str for value in values) and set(values) == {"nd-neighbor-solicit", "nd-neighbor-advert"})),
                     "only neighbor solicitation and advertisement are allowed")
            ndp.add("types")
        else:
            raise Denied("Direct nft read-back: unexpected match fields")
    if endpoint is not None:
        _require(not ndp, "mixed TCP and control-plane rule")
        _require(protocol_guards.get("l4proto", "tcp") == "tcp"
                 and protocol_guards.get("nfproto", "ipv4" if endpoint[1] == "allowed4" else "ipv6")
                 == ("ipv4" if endpoint[1] == "allowed4" else "ipv6"), "incorrect TCP protocol guard")
        required = frozenset({"established"}) if endpoint[0] == "input" else frozenset({"new", "established"})
        _require(state == required and rule.get("chain") == endpoint[0], "established access must retain exact timed membership")
        return endpoint
    # nft1.0.6 removes a redundant explicit l4proto match when the icmpv6
    # payload expression already implies the protocol. Both forms are exact.
    _require(state is None and ndp in ({"icmpv6", "hoplimit", "types"}, {"hoplimit", "types"})
             and rule.get("chain") in {"input", "output"},
             "arbitrary accept rule")
    _require(protocol_guards.get("l4proto", "ipv6-icmp") in {"ipv6-icmp", "icmpv6"}, "incorrect NDP protocol guard")
    _require(protocol_guards.get("nfproto", "ipv6") == "ipv6", "incorrect NDP family guard")
    return rule["chain"], "ndp"


def validate_readback(document, execution: DirectExecution, *, maximum_ms: int) -> None:
    """Require exact drop-default hooks, timed endpoint and gated allow rules."""
    try:
        _require(type(maximum_ms) is int and 0 < maximum_ms <= 30_000, "invalid timeout bound")
        address = ipaddress.ip_address(execution.destination)
        _require(isinstance(document, dict) and set(document) == {"nftables"}
                 and isinstance(document["nftables"], list), "invalid JSON envelope")
        inventory = {kind: [] for kind in ("table", "set", "chain", "rule")}
        for item in document["nftables"]:
            _require(isinstance(item, dict) and len(item) == 1, "invalid inventory entry")
            kind, obj = next(iter(item.items()))
            if kind == "metainfo":
                _require(isinstance(obj, dict), "invalid metadata")
                continue
            _require(kind in inventory and isinstance(obj, dict), "unknown inventory object")
            _require(obj.get("family") == "inet" and
                     (obj.get("name") == TABLE if kind == "table" else obj.get("table") == TABLE),
                     "foreign table or family")
            inventory[kind].append(obj)
        _require(len(inventory["table"]) == 1, "exactly one dedicated table required")
        sets = inventory["set"]
        _require(len(sets) == 2 and {s.get("name") for s in sets} == {"allowed4", "allowed6"}, "exact two family sets required")
        chosen = "allowed4" if address.version == 4 else "allowed6"
        for obj in sets:
            version = "ipv4_addr" if obj["name"] == "allowed4" else "ipv6_addr"
            _require(obj.get("type") in (f"{version} . inet_service", [version, "inet_service"])
                     and obj.get("flags") == ["timeout"], "set must be an exact timed address/port concatenation")
            elements = obj.get("elem", [])
            _require(isinstance(elements, list) and len(elements) == (1 if obj["name"] == chosen else 0), "extra or missing endpoint")
            if elements:
                wrapped = elements[0]
                _require(isinstance(wrapped, dict) and set(wrapped) == {"elem"}, "timed element wrapper required")
                elem = wrapped["elem"]
                _require(isinstance(elem, dict) and set(elem) == {"val", "timeout", "expires"}, "element must expose timeout and remaining expiry")
                _require(elem["val"] == {"concat": [execution.destination, execution.port]}, "wrong endpoint")
                _require(type(elem["timeout"]) is int and type(elem["expires"]) is int
                         and 0 < elem["expires"] <= elem["timeout"]
                         and (elem["timeout"] + 1) * 1000 <= maximum_ms,
                         "invalid or excessive element lifetime")
        chains = inventory["chain"]
        _require(len(chains) == 3 and {c.get("name") for c in chains} == {"input", "output", "forward"}, "exact three chains required")
        for chain in chains:
            _require(chain.get("hook") == chain["name"] and chain.get("type") == "filter"
                     and chain.get("prio") == 0 and chain.get("policy") == "drop", "invalid drop-default hook")
        signatures = [_rule_signature(rule) for rule in inventory["rule"]]
        expected = [(direction, name) for direction in ("input", "output") for name in ("allowed4", "allowed6")]
        if address.version == 6:
            expected += [("input", "ndp"), ("output", "ndp")]
        _require(sorted(signatures) == sorted(expected), "extra, duplicate or missing allow rule")
    except Denied:
        raise
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise Denied("Direct nft read-back: malformed inventory") from exc
