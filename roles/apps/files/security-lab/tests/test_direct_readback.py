"""Fake nft JSON contract coverage, never proof of actual kernel enforcement."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from security_lab.contracts import Denied
from security_lab.direct_policy import DirectExecution, compile_direct_execution
from security_lab.direct_readback import validate_readback
from test_direct_policy import bundle, NOW


def match(left, right):
    return {"match": {"op": "==", "left": left, "right": right}}


def document(execution):
    entries = [{"metainfo": {"version": "unit-fixture-only", "json_schema_version": 1}},
               {"table": {"family": "inet", "name": "security_lab_direct", "handle": 1}}]
    version = 6 if ":" in execution.destination else 4
    for v in (4, 6):
        obj = {"family": "inet", "table": "security_lab_direct", "name": f"allowed{v}",
               "type": [f"ipv{v}_addr", "inet_service"], "flags": ["timeout"]}
        if v == version:
            obj["elem"] = [{"elem": {"val": {"concat": [execution.destination, execution.port]},
                                      "timeout": 14, "expires": 13}}]
        entries.append({"set": obj})
    for chain in ("input", "output", "forward"):
        entries.append({"chain": {"family": "inet", "table": "security_lab_direct", "name": chain,
                                  "hook": chain, "type": "filter", "prio": 0, "policy": "drop"}})
    for chain, address_field, port_field, state in (("input", "saddr", "sport", "established"),
                                                   ("output", "daddr", "dport", {"set": ["new", "established"]})):
        for proto, name in (("ip", "allowed4"), ("ip6", "allowed6")):
            expr = [match({"ct": {"key": "state"}}, state),
                    match({"concat": [{"payload": {"protocol": proto, "field": address_field}},
                                      {"payload": {"protocol": "tcp", "field": port_field}}]}, "@" + name),
                    {"accept": None}]
            entries.append({"rule": {"family": "inet", "table": "security_lab_direct", "chain": chain, "expr": expr}})
        if version == 6:
            expr = [match({"meta": {"key": "l4proto"}}, "ipv6-icmp"),
                    match({"payload": {"protocol": "ip6", "field": "hoplimit"}}, 255),
                    match({"payload": {"protocol": "icmpv6", "field": "type"}}, {"set": [135, 136]}),
                    {"accept": None}]
            entries.append({"rule": {"family": "inet", "table": "security_lab_direct", "chain": chain, "expr": expr}})
    return {"nftables": entries}


def items(doc, kind):
    return [obj[kind] for obj in doc["nftables"] if kind in obj]


@pytest.fixture
def ipv4():
    plan, policy, lease = bundle()
    execution = compile_direct_execution(plan, policy, lease, now=NOW)
    return execution, document(execution)


def test_canonical_fake_ipv4_inventory_with_positive_bounded_timeout(ipv4):
    execution, doc = ipv4
    validate_readback(doc, execution, maximum_ms=15000)
    for obj in items(doc, "set"):
        obj["type"] = " . ".join(obj["type"])
    validate_readback(doc, execution, maximum_ms=15000)


def test_canonical_fake_ipv6_ndp_and_named_types():
    plan, policy, lease = bundle(["2001:4860:4860::8888"])
    execution = compile_direct_execution(plan, policy, lease, now=NOW)
    doc = document(execution)
    validate_readback(doc, execution, maximum_ms=15000)
    for rule in items(doc, "rule"):
        if len(rule["expr"]) == 4:
            rule["expr"][2]["match"]["right"] = {"set": ["nd-neighbor-solicit", "nd-neighbor-advert"]}
    validate_readback(doc, execution, maximum_ms=15000)


@pytest.mark.parametrize("field,value", [("timeout", 0), ("timeout", 15), ("timeout", True),
                                          ("expires", 0), ("expires", -1), ("expires", 15),
                                          ("expires", True), ("expires", 14.001)])
def test_static_expired_excessive_or_coerced_element_lifetimes_denied(ipv4, field, value):
    execution, doc = ipv4
    items(doc, "set")[0]["elem"][0]["elem"][field] = value
    with pytest.raises(Denied):
        validate_readback(doc, execution, maximum_ms=15000)


@pytest.mark.parametrize("missing", ["timeout", "expires", "val"])
def test_missing_timeout_expiry_or_endpoint_denied(ipv4, missing):
    execution, doc = ipv4
    del items(doc, "set")[0]["elem"][0]["elem"][missing]
    with pytest.raises(Denied):
        validate_readback(doc, execution, maximum_ms=15000)


@pytest.mark.parametrize("changes", ["extra-element", "wrong-address", "wrong-port", "wrong-family", "flags", "type", "static-element", "extra-set"])
def test_set_must_be_exact_one_timed_selected_endpoint(ipv4, changes):
    execution, doc = ipv4
    obj = items(doc, "set")[0]
    if changes == "extra-element":
        obj["elem"].append(deepcopy(obj["elem"][0]))
    elif changes == "wrong-address":
        obj["elem"][0]["elem"]["val"]["concat"][0] = "8.8.8.8"
    elif changes == "wrong-port":
        obj["elem"][0]["elem"]["val"]["concat"][1] = 443
    elif changes == "wrong-family":
        items(doc, "set")[1]["elem"] = obj.pop("elem")
    elif changes == "flags":
        obj["flags"] = []
    elif changes == "type":
        obj["type"] = "ipv4_addr"
    elif changes == "static-element":
        obj["elem"] = [{"concat": [execution.destination, execution.port]}]
    else:
        doc["nftables"].append({"set": {**obj, "name": "extra"}})
    with pytest.raises(Denied):
        validate_readback(doc, execution, maximum_ms=15000)


@pytest.mark.parametrize("changes", ["extra-rule", "duplicate-rule", "blanket-established", "missing-state", "accept-hook", "wrong-priority", "duplicate-chain", "unknown-entry", "foreign-table", "foreign-family"])
def test_nft_inventory_cannot_hide_broad_accept_or_unexpected_hooks(ipv4, changes):
    execution, doc = ipv4
    rule = items(doc, "rule")[0]
    if changes == "extra-rule":
        doc["nftables"].append({"rule": {**rule, "chain": "forward", "expr": [{"accept": None}]}})
    elif changes == "duplicate-rule":
        doc["nftables"].append({"rule": deepcopy(rule)})
    elif changes == "blanket-established":
        rule["expr"].pop(1)
    elif changes == "missing-state":
        rule["expr"].pop(0)
    elif changes == "accept-hook":
        items(doc, "chain")[0]["policy"] = "accept"
    elif changes == "wrong-priority":
        items(doc, "chain")[0]["prio"] = 10
    elif changes == "duplicate-chain":
        doc["nftables"].append({"chain": deepcopy(items(doc, "chain")[0])})
    elif changes == "unknown-entry":
        doc["nftables"].append({"flowtable": {"family": "inet", "table": "security_lab_direct"}})
    elif changes == "foreign-table":
        rule["table"] = "different"
    else:
        rule["family"] = "ip"
    with pytest.raises(Denied):
        validate_readback(doc, execution, maximum_ms=15000)


@pytest.mark.parametrize("changes", ["router", "echo", "hoplimit", "wrong-l4proto", "extra-ndp", "wrong-family"])
def test_ndp_must_remain_neighbor_only_with_link_hoplimit(changes):
    plan, policy, lease = bundle(["2001:4860:4860::8888"])
    execution = compile_direct_execution(plan, policy, lease, now=NOW)
    doc = document(execution)
    ndp = next(rule for rule in items(doc, "rule") if len(rule["expr"]) == 4)
    if changes in {"router", "echo"}:
        ndp["expr"][2]["match"]["right"] = {"set": [133, 134] if changes == "router" else [128, 129]}
    elif changes == "hoplimit":
        ndp["expr"][1]["match"]["right"] = 64
    elif changes == "wrong-l4proto":
        ndp["expr"][0]["match"]["right"] = "tcp"
    elif changes == "extra-ndp":
        doc["nftables"].append({"rule": deepcopy(ndp)})
    else:
        ndp["expr"].insert(0, match({"meta": {"key": "nfproto"}}, "ipv4"))
    with pytest.raises(Denied):
        validate_readback(doc, execution, maximum_ms=15000)


def numeric_document(execution):
    """nft1.0.6 -n -j shape: exact state bits and optimized ICMPv6 guard."""
    doc = document(execution)
    for rule in items(doc, 'rule'):
        if len(rule['expr']) == 3:
            state = rule['expr'][0]['match']
            state['op'] = 'in'
            state['right'] = 2 if rule['chain'] == 'input' else {'set': [2, 8]}
        else:
            rule['expr'].pop(0)
    return doc


@pytest.mark.parametrize('addresses', [None, ['2001:4860:4860::8888']])
def test_actual_numeric_ipv4_ipv6_json_format_with_seconds_and_implied_ndp_protocol(addresses):
    plan, policy, lease = bundle(addresses)
    execution = compile_direct_execution(plan, policy, lease, now=NOW)
    validate_readback(numeric_document(execution), execution, maximum_ms=15000)


def test_captured_owned_linux_nft_1_0_6_ipv4_inventory():
    # Actual table readback from owned attempt-v3, not a successful HTTP or
    # full kernel qualification receipt. Contains only public lab inventory.
    doc = json.loads((Path(__file__).parent / 'fixtures' / 'nft-1.0.6-ipv4.json').read_text('utf-8'))
    execution = DirectExecution((), '172.31.64.3', 8443, 'fixture.example.test', '0' * 64)
    validate_readback(doc, execution, maximum_ms=20000)
    with pytest.raises(Denied):
        validate_readback(doc, execution, maximum_ms=14999)


@pytest.mark.parametrize('states', [True, False, 10, 1, 4, 16, {'set': [2, 10]},
                                    {'set': [2, 2]}, {'set': [2, 'established']},
                                    {'set': [2, True]}, {'set': [2, 8, 8]}, 2.0])
def test_unknown_masks_bool_mixed_duplicate_or_float_conntrack_states_denied(ipv4, states):
    execution, _ = ipv4
    doc = numeric_document(execution)
    rule = items(doc, 'rule')[2]  # output must be exactly new + established
    rule['expr'][0]['match']['right'] = states
    with pytest.raises(Denied):
        validate_readback(doc, execution, maximum_ms=15000)


def test_seconds_readback_uses_conservative_truncation_upper_bound(ipv4):
    execution, doc = ipv4
    # A reported14s can encode14.999s. Comparing14*1000 would accept
    # hidden excess over14.999s; require a full15s upper bound instead.
    with pytest.raises(Denied):
        validate_readback(doc, execution, maximum_ms=14999)
    validate_readback(doc, execution, maximum_ms=15000)
    elem = items(doc, 'set')[0]['elem'][0]['elem']
    elem.update(timeout=15, expires=14)
    with pytest.raises(Denied):
        validate_readback(doc, execution, maximum_ms=15999)
    validate_readback(doc, execution, maximum_ms=16000)


@pytest.mark.parametrize('guard', [58, 'ipv6-icmp', 'icmpv6'])
def test_explicit_correct_ndp_protocol_guard_can_coexist_with_payload(guard):
    plan, policy, lease = bundle(['2001:4860:4860::8888'])
    execution = compile_direct_execution(plan, policy, lease, now=NOW)
    doc = numeric_document(execution)
    for rule in items(doc, 'rule'):
        if rule['expr'][0]['match']['left'] == {'payload': {'protocol': 'ip6', 'field': 'hoplimit'}}:
            rule['expr'].insert(0, match({'meta': {'key': 'l4proto'}}, guard))
    validate_readback(doc, execution, maximum_ms=15000)


@pytest.mark.parametrize('guard', [6, 'tcp', 17, 'udp', 59, True])
def test_implied_ndp_protocol_cannot_hide_wrong_or_unknown_explicit_guard(guard):
    plan, policy, lease = bundle(['2001:4860:4860::8888'])
    execution = compile_direct_execution(plan, policy, lease, now=NOW)
    doc = numeric_document(execution)
    ndp = next(rule for rule in items(doc, 'rule')
               if rule['expr'][0]['match']['left'] == {'payload': {'protocol': 'ip6', 'field': 'hoplimit'}})
    ndp['expr'].insert(0, match({'meta': {'key': 'l4proto'}}, guard))
    with pytest.raises(Denied):
        validate_readback(doc, execution, maximum_ms=15000)
