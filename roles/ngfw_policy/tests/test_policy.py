"""Offline contract-shaped simulator, not evidence of an actual PT appliance."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("policy", ROOT / "library/pt_ngfw_policy.py")
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


def ident(i):
    return str(uuid.UUID(int=i))


def config(**extra):
    return {"device_group_id": ident(1), "logical_device_id": ident(2), "physical_device_id": ident(3),
            "left_interface_id": ident(4), "right_interface_id": ident(5), "acl_count": 18, "nat_count": 4, **extra}


def api_row(kind, body, i):
    row = copy.deepcopy(body)
    row["id"] = ident(i)
    row.pop("positionRelativeToGroup", None)
    if kind == "NetworkObject":
        row["inet"] = row.pop("value")["inet"]["inet"]
    if kind in p.RULES:
        row["ruleGroupId"] = ident(10 if kind == "SecurityRule" else 11)
        for key, val in list(row.items()):
            if isinstance(val, dict) and "objects" in val:
                val["objects"] = [{"id": x} for x in val["objects"].get("array", [])]
            if key in {"srcTranslatedAddress", "dstTranslatedAddress"}:
                row[key] = [{"id": x} for x in val]
    return row


class FakeAPI:
    def __init__(self):
        self.rows = {k: [] for k in p.KINDS}
        self.calls = []
        self.counter = 100
        self.devices = [{"id": ident(3), "name": "pt-ngfw-auditd", "logicalDevice": {"id": ident(2)}, "productVersion": "1.11.1"}]
        self.interfaces = [
            {"id": ident(4 + i), "enabled": True, "mode": "VIRTUAL_INTERFACE_MODE_ROUTING",
             "inet": [cidr], "inet6": [], "virtualContext": {"deviceGroup": {"id": ident(1)}},
             "virtualRouter": {"id": ident(20)}, "zone": {"id": "old-zone"}}
            for i, cidr in enumerate(["10.77.10.1/24", "10.77.20.1/24"])]
        self.corrupt_readback = False

    @property
    def mutations(self):
        return [(op, body) for op, body in self.calls if op.startswith(("Create", "Update", "Move", "Commit", "Push", "Delete"))]

    def call(self, op, body):
        self.calls.append((op, copy.deepcopy(body)))
        if op in {"Login", "Logout"}:
            return {}
        if op == "GetDeviceGroupsTree":
            return {"groups": [{"id": ident(9), "name": "Root", "subgroups": [
                {"id": ident(1), "name": "ngfw-auditd-lab", "parentId": ident(9)}]}]}
        if op == "ListPhysicalDevices":
            return {"physicalDevices": copy.deepcopy(self.devices)}
        if op == "ListVirtualInterfaces":
            return {"virtualInterfaces": copy.deepcopy(self.interfaces)}
        if op == "UpdateVirtualInterface":
            next(v for v in self.interfaces if v["id"] == body["id"])["zone"] = {"id": body["zoneId"]}
            return {}
        if op.endswith("RuleGroups"):
            kind = "SecurityRule" if "Security" in op else "NatRule"
            return {"ruleGroups": [{"id": ident(10 if kind == "SecurityRule" else 11), "deviceGroupId": ident(1), "precedence": "RULE_PRECEDENCE_PRE"}]}
        for kind, (list_op, key) in p.KINDS.items():
            if op == list_op:
                offset = int(body.get("cursor", body.get("offset", 0)))
                rows = sorted(self.rows[kind], key=lambda x: x.get("position", 0))
                part = copy.deepcopy(rows[offset:offset + body["limit"]])
                if self.corrupt_readback and part and kind == "SecurityRule":
                    part[0]["enabled"] = False
                result = {key: part}
                if kind in p.RULES and offset + len(part) < len(rows):
                    result["nextCursor"] = str(offset + len(part))
                return result
            if op == "Create" + kind:
                self.counter += 1
                row = api_row(kind, body, self.counter)
                row["position"] = len(self.rows[kind])
                self.rows[kind].append(row)
                return {"id": row["id"]}
            if op == "Update" + kind:
                row = next(r for r in self.rows[kind] if r["id"] == body["id"])
                row.update(api_row(kind, body, uuid.UUID(row["id"]).int))
                return {}
            if op == "Move" + kind:
                row = next(r for r in self.rows[kind] if r["id"] == body["id"])
                self.rows[kind].remove(row)
                self.rows[kind].append(row)
                for i, r in enumerate(self.rows[kind]):
                    r["position"] = i
                return {}
        if op in {"CommitSnapshot", "PushSnapshot"}:
            return {"jobId": ident(10000)}
        if op == "GetSnapshotCommitJob":
            return {"commitJob": {"state": {"kind": "JOB_STATE_DONE", "commitDone": {"snapshotId": ident(10001)}}}}
        if op == "GetSnapshotPushJob":
            return {"pushJob": {"state": {"kind": "JOB_STATE_DONE"}, "successfulPushes": [{"deviceName": "pt-ngfw-auditd"}], "failedPushes": [], "failedLogCollectorPushes": []}}
        raise AssertionError(op)


class GenerationTests(unittest.TestCase):
    def test_default_exact_counts_unique_names_and_ports(self):
        plan = p.build_plan(p.configure({}))
        self.assertEqual(plan["counts"], {"Zone": 2, "NetworkObject": 3, "Service": 1199, "SecurityRule": 1000, "NatRule": 200})
        all_names = [r["name"] for rows in plan["objects"].values() for r in rows]
        self.assertEqual(len(set(all_names)), len(all_names))
        acl = plan["objects"]["SecurityRule"]
        self.assertTrue(acl[-1]["name"].endswith("deny-rest"))
        self.assertEqual(acl[-1]["action"], "SECURITY_RULE_ACTION_DROP")
        self.assertTrue(all(r["sourceAddr"]["kind"] == "RULE_KIND_LIST" and r["destinationAddr"]["kind"] == "RULE_KIND_LIST" for r in acl))
        self.assertTrue(all(r["logMode"].endswith("NO_LOG") for r in acl))

    def test_maximum_scale(self):
        plan = p.build_plan(p.configure({"acl_count": 10000, "nat_count": 2000}))
        self.assertEqual(plan["counts"]["SecurityRule"], 10000)
        self.assertEqual(plan["counts"]["NatRule"], 2000)
        self.assertEqual(plan["receiver_ranges"], [[10000, 19991], [20000, 20999]])

    def test_invalid_inputs(self):
        for bad in [{"acl_count": True}, {"acl_count": 10001}, {"nat_count": 3},
                    {"management_url": "http://10.77.0.10"}, {"management_url": "https://example.org"},
                    {"management_url": "https://x:y@10.77.0.10"}, {"client": "192.168.1.88"},
                    {"right_cidr": "10.77.10.1/24"}, {"server": "10.77.20.100"},
                    {"left_cidr": "10.77.0.1/24"}, {"log_mode": "AT_RULE_HIT"}, {"arbitrary": 1}]:
            with self.subTest(bad=bad), self.assertRaises((p.PolicyError, ValueError)):
                p.configure(bad)

    def test_plan_never_connects_or_requires_credentials(self):
        api = FakeAPI()
        result = p.execute({}, api=api)
        self.assertFalse(result["changed"])
        self.assertEqual(api.calls, [])

    def test_representative_samples(self):
        plan = p.build_plan(p.configure({}))
        self.assertEqual({r["kind"] for r in plan["probes"]}, {"acl", "snat", "dnat"})
        self.assertIn("drop", {r["expect"] for r in plan["probes"]})
        self.assertEqual({r["port"] for r in plan["probes"] if r["kind"] == "snat"}, {20000, 20050, 20099})


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.c = config()

    def run_policy(self, mode="apply", **kw):
        return p.execute(self.c, mode, "private-login", "private-password", api=self.api, **kw)

    def test_apply_then_second_run_is_noop(self):
        first = self.run_policy()
        self.assertTrue(first["changed"])
        count = len(self.api.mutations)
        second = self.run_policy()
        self.assertFalse(second["changed"])
        self.assertEqual(len(self.api.mutations), count)
        self.assertEqual(second["summary"]["state"], "candidate-read-back")
        self.assertFalse(any(op.startswith(("Commit", "Push", "Delete")) for op, _ in self.api.calls))

    def test_full_default_profile_reconciles_across_pages(self):
        self.c.update(acl_count=1000, nat_count=200)
        self.assertTrue(self.run_policy()["changed"])
        self.assertFalse(self.run_policy()["changed"])
        self.assertTrue(any(op == "ListSecurityRules2" and "cursor" in body for op, body in self.api.calls))
        self.assertTrue(any(op == "ListServices" and body.get("offset", 0) > 0 for op, body in self.api.calls))

    def test_reference_set_order_does_not_cause_updates(self):
        self.run_policy()
        for row in self.api.rows["SecurityRule"]:
            row["destinationAddr"]["objects"].reverse()
        for row in self.api.rows["Service"]:
            row["dstPorts"].reverse()
        self.assertFalse(self.run_policy()["changed"])

    def test_credential_values_do_not_appear_in_output(self):
        output = json.dumps(self.run_policy())
        self.assertNotIn("private-login", output)
        self.assertNotIn("private-password", output)

    def test_check_and_ansible_check_are_readonly(self):
        for mode, check in [("check", False), ("apply", True), ("publish", True)]:
            result = self.run_policy(mode, check_mode=check)
            self.assertTrue(result["changed"])
            self.assertEqual(self.api.mutations, [])

    def test_duplicate_name_refused_before_any_write(self):
        c = p.configure(self.c)
        plan = p.build_plan(c)
        row = api_row("Service", {**plan["objects"]["Service"][0], "deviceGroupId": ident(1)}, 500)
        self.api.rows["Service"] = [row, {**row, "id": ident(501)}]
        with self.assertRaisesRegex(p.PolicyError, "duplicate managed name"):
            self.run_policy()
        self.assertEqual(self.api.mutations, [])

    def test_foreign_objects_not_adopted(self):
        self.api.rows["Zone"] = [{"id": ident(500), "name": "auditd-lab-left", "description": "human-owned", "deviceGroupId": ident(1)}]
        with self.assertRaisesRegex(p.PolicyError, "ownership"):
            self.run_policy()
        self.assertEqual(self.api.mutations, [])

    def test_foreign_rules_refused(self):
        self.api.rows["NatRule"] = [{"id": ident(500), "name": "production-rule", "ruleGroupId": ident(11)}]
        with self.assertRaisesRegex(p.PolicyError, "foreign rule"):
            self.run_policy()
        self.assertEqual(self.api.mutations, [])

    def test_management_interface_not_modified(self):
        self.api.interfaces[0]["inet"] = ["10.77.0.20/24"]
        with self.assertRaisesRegex(p.PolicyError, "intended lab IPv4"):
            self.run_policy()
        self.assertEqual(self.api.mutations, [])

    def test_wrong_group_not_modified(self):
        self.api.interfaces[1]["virtualContext"]["deviceGroup"]["id"] = ident(900)
        with self.assertRaisesRegex(p.PolicyError, "selected lab device group"):
            self.run_policy()
        self.assertEqual(self.api.mutations, [])

    def test_wrong_version_not_modified(self):
        self.api.devices[0]["productVersion"] = "1.12.0"
        with self.assertRaisesRegex(p.PolicyError, "1.11.1"):
            self.run_policy()
        self.assertEqual(self.api.mutations, [])

    def test_grow_repairs_terminal_deny_order(self):
        self.run_policy()
        self.c["acl_count"] = 20
        self.run_policy()
        rules = sorted(self.api.rows["SecurityRule"], key=lambda r: r["position"])
        self.assertTrue(rules[-1]["name"].endswith("deny-rest"))
        self.assertFalse(self.run_policy()["changed"])

    def test_shrink_fails_without_deletion(self):
        self.run_policy()
        self.api.calls.clear()
        self.c["acl_count"] = 16
        with self.assertRaisesRegex(p.PolicyError, "obsolete"):
            self.run_policy()
        self.assertEqual(self.api.mutations, [])

    def test_field_drift_repaired_once(self):
        self.run_policy()
        self.api.rows["SecurityRule"][0]["enabled"] = False
        result = self.run_policy()
        self.assertEqual(result["summary"]["change_count"], 1)
        self.assertFalse(self.run_policy()["changed"])

    def test_readback_failure_never_publishes(self):
        self.api.corrupt_readback = True
        with self.assertRaisesRegex(p.PolicyError, "Read-back"):
            self.run_policy()
        self.assertFalse(any(op.startswith(("Commit", "Push")) for op, _ in self.api.calls))

    def test_publish_requires_explicit_global_scope(self):
        self.run_policy()
        self.api.calls.clear()
        with self.assertRaisesRegex(p.PolicyError, "GLOBAL"):
            self.run_policy("publish")
        self.assertEqual(self.api.mutations, [])

    def test_publish_refuses_additional_ngfw(self):
        self.run_policy()
        self.c["dedicated_mngt_confirmed"] = True
        self.api.devices.append({"id": ident(900)})
        self.api.calls.clear()
        with self.assertRaisesRegex(p.PolicyError, "single lab"):
            self.run_policy("publish")
        self.assertEqual(self.api.mutations, [])

    def test_publish_checks_both_background_jobs(self):
        self.run_policy()
        self.c["dedicated_mngt_confirmed"] = True
        result = self.run_policy("publish")
        self.assertEqual(result["summary"]["state"], "push-job-confirmed")
        calls = dict(self.api.calls)
        self.assertFalse(calls["CommitSnapshot"]["push"])
        self.assertIn("GetSnapshotCommitJob", calls)
        self.assertIn("GetSnapshotPushJob", calls)
        self.assertFalse(result["summary"]["packet_hits_verified"])

    def test_failed_push_is_not_success(self):
        self.run_policy()
        self.c["dedicated_mngt_confirmed"] = True
        original = self.api.call
        def failed(op, body):
            if op == "GetSnapshotPushJob":
                return {"pushJob": {"state": {"kind": "JOB_STATE_DONE"}, "failedPushes": [{"deviceName": "pt-ngfw-auditd"}]}}
            return original(op, body)
        self.api.call = failed
        with self.assertRaisesRegex(p.PolicyError, "failed targets"):
            self.run_policy("publish")


class PaginationTests(unittest.TestCase):
    def test_offset_exhaustion(self):
        class Pages:
            def call(self, op, body):
                return {"items": [{"id": str(i)} for i in range(body["offset"], min(450, body["offset"] + body["limit"]))]}
        self.assertEqual(len(p.pages(Pages(), "List", "items")), 450)

    def test_cursor_loop_refused(self):
        class Pages:
            def call(self, op, body):
                return {"items": [], "nextCursor": "repeated"}
        with self.assertRaisesRegex(p.PolicyError, "cursor loop"):
            p.pages(Pages(), "List", "items", cursor=True)

    def test_duplicate_id_refused(self):
        class Pages:
            def call(self, op, body):
                return {"items": [{"id": "x"}, {"id": "x"}]}
        with self.assertRaisesRegex(p.PolicyError, "duplicate"):
            p.pages(Pages(), "List", "items")

    def test_redirect_never_forwards_credentials(self):
        with self.assertRaisesRegex(p.PolicyError, "redirect refused"):
            p.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://elsewhere")


@unittest.skipUnless(os.environ.get("NGFW_OPENAPI_SCHEMA"), "Vendor schema is local evidence, not distributed in Git")
class VendorSchemaTests(unittest.TestCase):
    def test_every_emitted_request_matches_supplied_vendor_schema(self):
        """Validate field names, required fields, enums, nested oneof selections.

        Vendor protobuf oneOf branches omit required and additionalProperties;
        generic JSON Schema oneOf wrongly matches all branches. Require exactly
        one named branch instead. This test does not claim server acceptance.
        """
        doc = json.loads(Path(os.environ["NGFW_OPENAPI_SCHEMA"]).read_text(encoding="utf-8"))
        schemas = doc["components"]["schemas"]
        def validate(value, schema):
            if "$ref" in schema:
                return validate(value, schemas[schema["$ref"].split("/")[-1]])
            for item in schema.get("allOf", []):
                validate(value, item)
            if "oneOf" in schema:
                keys = set().union(*(x.get("properties", {}) for x in schema["oneOf"]))
                self.assertEqual(len(keys.intersection(value)), 1)
            if "enum" in schema:
                self.assertIn(value, schema["enum"])
            typ = schema.get("type")
            if typ == "object":
                self.assertIsInstance(value, dict)
                self.assertTrue(set(schema.get("required", [])).issubset(value))
                properties = schema.get("properties", {})
                self.assertFalse(set(value) - set(properties), f"Unknown fields: {set(value) - set(properties)}")
                for key, item in value.items():
                    validate(item, properties[key])
            elif typ == "array":
                self.assertIsInstance(value, list)
                for item in value:
                    validate(item, schema["items"])
            elif typ in {"integer", "string", "boolean"}:
                self.assertIs(type(value), {"integer": int, "string": str, "boolean": bool}[typ])
        api = FakeAPI()
        c = config(dedicated_mngt_confirmed=True)
        p.execute(c, "apply", "test", "test", api=api)
        api.rows["SecurityRule"][0]["enabled"] = False
        api.rows["Service"][0]["protocol"] = 17
        api.rows["NetworkObject"][0]["inet"] = "10.77.10.11/32"
        api.rows["Zone"][0]["description"] = p.build_plan(p.configure(c))["owner"]
        c["acl_count"] = 20
        p.execute(c, "apply", "test", "test", api=api)
        p.execute(c, "publish", "test", "test", api=api)
        for operation, body in api.calls:
            with self.subTest(operation=operation):
                route = doc["paths"]["/api/v2/" + operation]["post"]
                if "requestBody" in route:
                    validate(body, route["requestBody"]["content"]["application/json"]["schema"])


if __name__ == "__main__":
    unittest.main()
