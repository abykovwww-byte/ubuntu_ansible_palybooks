"""Preparation from a previously discovered lab; never a live-appliance claim."""
import copy
import unittest

from test_policy import FakeAPI, ident, p


class PrepareAPI(FakeAPI):
    def __init__(self):
        super().__init__()
        self.groups = [{"id": ident(9), "name": "Global", "subgroups": []}]
        self.devices[0]["address"] = "10.77.0.20"
        self.contexts[0]["deviceGroup"] = {"id": ident(9), "name": "Global"}
        for v in self.interfaces:
            v["virtualContext"] = copy.deepcopy(self.contexts[0])
        self.baseline = {"SecurityRule": [{"id": ident(900), "name": "existing-baseline",
            "ruleGroupId": ident(90), "enabled": True, "position": 0,
            "action": "SECURITY_RULE_ACTION_ALLOW", "logMode": "SECURITY_RULE_LOG_MODE_NO_LOG"}], "NatRule": []}
        self.hide_inheritance = False
        self.parent_pre = False

    def call(self, op, body):
        if op == "CreateDeviceGroup":
            self.calls.append((op, copy.deepcopy(body)))
            self.groups[0]["subgroups"].append({"id": ident(1), **body})
            return {"id": ident(1)}
        if op == "UpdateVirtualContext":
            self.calls.append((op, copy.deepcopy(body)))
            self.contexts[0]["deviceGroup"] = {"id": body["deviceGroupId"], "name": "ngfw-auditd-lab"}
            for v in self.interfaces:
                v["virtualContext"] = copy.deepcopy(self.contexts[0])
            return {}
        if op.endswith("RuleGroups"):
            self.calls.append((op, copy.deepcopy(body)))
            rows = [{"id": ident(90 if "Security" in op else 91), "deviceGroupId": ident(9),
                     "precedence": "RULE_PRECEDENCE_PRE" if self.parent_pre else "RULE_PRECEDENCE_POST"}]
            if body["deviceGroupId"] == ident(1):
                rows.append({"id": ident(10 if "Security" in op else 11), "deviceGroupId": ident(1),
                             "precedence": "RULE_PRECEDENCE_PRE"})
            return {"ruleGroups": rows}
        for kind in p.RULES:
            if op == p.KINDS[kind][0]:
                self.calls.append((op, copy.deepcopy(body)))
                rows = copy.deepcopy(self.baseline[kind])
                if body["deviceGroupId"] == ident(1):
                    rows = ([] if self.hide_inheritance else rows) + copy.deepcopy(self.rows[kind])
                offset = int(body.get("cursor", 0))
                part = rows[offset:offset + body["limit"]]
                result = {"items": part}
                if offset + len(part) < len(rows):
                    result["nextCursor"] = str(offset + len(part))
                return result
        return super().call(op, body)


def inventory_for(api):
    return p.discover(api, p.configure({}))


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.api = PrepareAPI()
        self.inventory = inventory_for(self.api)
        self.api.calls.clear()
        self.config = {"acl_count": 18, "nat_count": 4}

    def run_prepare(self, **kwargs):
        return p.execute(self.config, "prepare", "private-login", "private-password",
                         api=self.api, inventory=self.inventory, **kwargs)

    def test_prepare_preserves_interfaces_baseline_and_never_publishes(self):
        before = p.interface_fingerprint(self.api.interfaces)
        baseline = copy.deepcopy(self.api.baseline)
        result = self.run_prepare()
        self.assertEqual([op for op, _ in self.api.mutations], ["CreateDeviceGroup", "UpdateVirtualContext"])
        self.assertEqual(self.api.mutations[-1][1], {"id": ident(6), "deviceGroupId": ident(1)})
        self.assertEqual(p.interface_fingerprint(self.api.interfaces), before)
        self.assertEqual(self.api.baseline, baseline)
        self.assertEqual(result["prepared_config"]["device_group_id"], ident(1))
        self.assertEqual(result["summary"]["state"], "prepared-candidate")
        self.assertTrue(result["summary"]["baseline_inheritance_verified"])
        self.assertTrue(result["summary"]["interfaces_unchanged_verified"])
        self.assertFalse(result["summary"]["packet_hits_verified"])

    def test_second_prepare_with_original_inventory_is_noop(self):
        self.run_prepare()
        self.api.calls.clear()
        self.assertFalse(self.run_prepare()["changed"])
        self.assertEqual(self.api.mutations, [])

    def test_entire_candidate_sequence_is_repeatable_without_publish(self):
        for _ in range(2):
            prepared = self.run_prepare()["prepared_config"]
            result = p.execute(prepared, "apply", "test", "test", api=self.api)
            check = p.execute(prepared, "check", "test", "test", api=self.api)
            self.assertEqual(check["summary"]["change_count"], 0)
        self.assertFalse(result["changed"])
        self.assertFalse(any(op.startswith(("Commit", "Push", "Delete")) for op, _ in self.api.calls))

    def test_check_mode_no_group_creation_or_context_move(self):
        result = self.run_prepare(check_mode=True)
        self.assertEqual(result["summary"]["state"], "preparation-check")
        self.assertEqual(len(result["changes"]), 2)
        self.assertEqual(result["prepared_config"]["device_group_id"], "")
        self.assertEqual(self.api.mutations, [])
        self.assertFalse(result["summary"]["baseline_inheritance_verified"])

    def test_preparation_never_persists_global_publish_authorization(self):
        self.config["dedicated_mngt_confirmed"] = True
        self.assertFalse(self.run_prepare()["prepared_config"]["dedicated_mngt_confirmed"])

    def test_owned_group_from_partial_run_is_reused(self):
        self.api.groups[0]["subgroups"] = [{"id": ident(1), "name": "ngfw-auditd-lab", "parentId": ident(9),
            "description": p.build_plan(p.configure({}))["owner"]}]
        self.run_prepare()
        self.assertEqual([op for op, _ in self.api.mutations], ["UpdateVirtualContext"])

    def test_foreign_same_name_group_is_not_adopted(self):
        self.api.groups[0]["subgroups"] = [{"id": ident(1), "name": "ngfw-auditd-lab", "parentId": ident(9)}]
        with self.assertRaisesRegex(p.PolicyError, "not an owned"):
            self.run_prepare()
        self.assertEqual(self.api.mutations, [])

    def test_duplicate_target_group_fails_before_write(self):
        self.api.groups[0]["subgroups"] = [{"id": ident(i), "name": "ngfw-auditd-lab"} for i in (1, 8)]
        with self.assertRaisesRegex(p.PolicyError, "Duplicate laboratory"):
            self.run_prepare()
        self.assertEqual(self.api.mutations, [])

    def test_stale_discovery_device_identity_fails_before_write(self):
        self.api.devices[0]["id"] = ident(888)
        with self.assertRaisesRegex(p.PolicyError, "identity mismatch"):
            self.run_prepare()
        self.assertEqual(self.api.mutations, [])

    def test_added_device_or_context_fails_before_write(self):
        for field, value in [("devices", {"id": ident(888)}), ("contexts", {"id": ident(777)})]:
            with self.subTest(field=field):
                rows = getattr(self.api, field)
                rows.append(value)
                with self.assertRaises(p.PolicyError):
                    self.run_prepare()
                rows.pop()
                self.assertEqual(self.api.mutations, [])

    def test_wrong_live_address_or_name_fails_before_write(self):
        for key, value in [("name", "another-device"), ("address", "10.77.0.99")]:
            old = self.api.devices[0][key]
            self.api.devices[0][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(p.PolicyError, "lab identity changed"):
                self.run_prepare()
            self.api.devices[0][key] = old
            self.assertEqual(self.api.mutations, [])

    def test_unapproved_inventory_never_contacts_mngt(self):
        for change in ("device-name", "extra-device", "duplicate-ip", "missing-parent"):
            original = copy.deepcopy(self.inventory)
            if change == "device-name":
                self.inventory["physical_devices"][0]["name"] = "unapproved"
            elif change == "extra-device":
                self.inventory["physical_devices"].append({"id": ident(777)})
            elif change == "duplicate-ip":
                self.inventory["virtual_interfaces"].append(copy.deepcopy(self.inventory["virtual_interfaces"][0]))
            else:
                self.inventory["device_groups"] = []
            with self.subTest(change=change), self.assertRaises(p.PolicyError):
                self.run_prepare()
            self.inventory = original
            self.assertEqual(self.api.calls, [])

    def test_explicit_conflicting_id_never_overwritten(self):
        self.config["physical_device_id"] = ident(999)
        with self.assertRaisesRegex(p.PolicyError, "conflicts with discovery"):
            self.run_prepare()
        self.assertEqual(self.api.calls, [])

    def test_interface_drift_or_ips_fails_before_write(self):
        self.api.interfaces[0]["inet"] = ["10.77.0.20/24"]
        with self.assertRaisesRegex(p.PolicyError, "intended lab IPv4"):
            self.run_prepare()
        self.assertEqual(self.api.mutations, [])

    def test_parent_pre_policy_refused_before_write(self):
        self.api.parent_pre = True
        with self.assertRaisesRegex(p.PolicyError, "active parent PRE"):
            self.run_prepare()
        self.assertEqual(self.api.mutations, [])

    def test_missing_inherited_baseline_stops_without_publish(self):
        self.api.hide_inheritance = True
        with self.assertRaisesRegex(p.PolicyError, "not inherited unchanged"):
            self.run_prepare()
        self.assertFalse(any(op.startswith(("Commit", "Push")) for op, _ in self.api.calls))

    def test_unexpected_interface_change_during_move_is_detected(self):
        original = self.api.call
        def drift(op, body):
            result = original(op, body)
            if op == "UpdateVirtualContext":
                self.api.interfaces[0]["zone"] = {"id": "unexpected-zone"}
            return result
        self.api.call = drift
        with self.assertRaisesRegex(p.PolicyError, "Interface configuration changed"):
            self.run_prepare()

    def test_context_move_failure_is_not_retried_and_logs_out(self):
        original = self.api.call
        def fail(op, body):
            if op == "UpdateVirtualContext":
                self.api.calls.append((op, body))
                raise p.PolicyError("UpdateVirtualContext: HTTP 503; no automatic write retry")
            return original(op, body)
        self.api.call = fail
        with self.assertRaisesRegex(p.PolicyError, "HTTP 503"):
            self.run_prepare()
        self.assertEqual(sum(op == "UpdateVirtualContext" for op, _ in self.api.calls), 1)
        self.assertEqual(self.api.calls[-1], ("Logout", {}))


if __name__ == "__main__":
    unittest.main()
