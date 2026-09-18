import copy
import importlib.util
import json
from pathlib import Path
import socket
import socketserver
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


r = load("runner", ROOT / "files/runner.py")
sys.path.insert(0, str(ROOT / "files/traffic"))
e = load("endpoint", ROOT / "files/traffic/endpoint.py")
LIMITS = dict(backlog_pct=75, backlog_seconds=30, disk_used_pct=80, cpu_pct=90,
              cpu_seconds=60, memory_used_pct=90, memory_seconds=60,
              log_projection_hours=24, udp_loss_pct=.5, tcp_drop_pct=20)


def config():
    return dict(duration=1, warmup=1, idle=0, repetitions=3, sample_interval=2,
                project_dir="/test", reports_dir="/test", target="10.77.20.10",
                management_address="10.77.0.20", mngt_address="10.77.0.10", management_port=22, mngt_port=443,
                ngfw_vm="ngfw", mngt_vm="mngt", limits=copy.deepcopy(LIMITS),
                scenarios=[dict(name="tcp", kind="tcp", parallel=4, mbps=100)])


def sample(now=0):
    return dict(monotonic=now, time="test", unavailable=[], dataplane_ok=True, management_ok=True,
                mngt_ok=True, vms_ok=True, host=dict(memory_used_pct=30, disk_used_pct=20),
                audit=dict(boot_id="boot-1", audit_lost=0, audit_backlog=0, audit_backlog_limit=8192,
                           cpu_total=100 + now * 100, cpu_idle=90 + now * 90, root_used_pct=20,
                           audit_disk_used_pct=20, memory_used_pct=40, log_bytes=10,
                           log_inode=1, event_serial=1, audit_free_bytes=1e9))


class Guards(unittest.TestCase):
    def test_no_audit_cannot_pass_as_observed(self):
        s = sample()
        s.pop("audit")
        self.assertTrue(r.Guard(LIMITS, True).check(s))
        self.assertEqual(r.Guard(LIMITS, False).check(s), [])

    def test_lost_and_reboot(self):
        guard = r.Guard(LIMITS, True)
        self.assertEqual(guard.check(sample()), [])
        s = sample(5)
        s["audit"].update(audit_lost=1, boot_id="boot-2")
        reasons = guard.check(s)
        self.assertIn("AuditD lost is nonzero", reasons)
        self.assertIn("appliance rebooted", reasons)

    def test_sustained_backlog_resets(self):
        guard = r.Guard(LIMITS, True)
        for now in (0, 10, 29):
            s = sample(now)
            s["audit"]["audit_backlog"] = 7000
            self.assertEqual(guard.check(s), [])
        self.assertEqual(guard.check(sample(30)), [])
        for now in (31, 60, 61):
            s = sample(now)
            s["audit"]["audit_backlog"] = 7000
            self.assertEqual(bool(guard.check(s)), now == 61)

    def test_cpu_pressure_and_rotation_gap(self):
        guard = r.Guard(LIMITS, True)
        guard.check(sample())
        for now in (5, 65):
            s = sample(now)
            s["audit"].update(cpu_idle=90, log_inode=2, log_bytes=0)
            reasons = guard.check(s)
            if now == 5:
                self.assertIn("log_rate_gap", s["audit"])
            else:
                self.assertIn("sustained appliance CPU", reasons)

    def test_disk_and_management_abort_immediately(self):
        s = sample()
        s["management_ok"] = False
        s["audit"]["audit_disk_used_pct"] = 80
        reasons = r.Guard(LIMITS, True).check(s)
        self.assertIn("management_ok failed", reasons)
        self.assertIn("appliance filesystem full threshold", reasons)

    def test_probe_rejects_nonfinite_and_missing_numbers(self):
        self.assertEqual(r.parse_probe("audit_lost=nan\nother=oops\nboot_id=a\naudit_backlog=12"),
                         {"boot_id": "a", "audit_backlog": 12})


class Planning(unittest.TestCase):
    def test_ngfw_guest_numa_matches_appliance_dpdk_profile(self):
        defaults = (ROOT / "defaults/main.yml").read_text()
        domain = (ROOT / "templates/ngfw-domain.xml.j2").read_text()
        expected_memory = [7168, 3072, 3072, 3072]
        for cell_id, memory_mib in enumerate(expected_memory):
            self.assertIn(
                f'{{id: {cell_id}, cpus: "{cell_id}", memory_mib: {memory_mib}}}',
                defaults,
            )
        self.assertIn("<numa>", domain)
        self.assertIn("{% for cell in ngfw_lab_ngfw_numa_cells %}", domain)
        self.assertIn(
            '<cell id="{{ cell.id }}" cpus="{{ cell.cpus }}" '
            'memory="{{ cell.memory_mib }}" unit="MiB"/>',
            domain,
        )

    def test_domains_have_stable_uuids_for_redefinition(self):
        defaults = (ROOT / "defaults/main.yml").read_text()
        ngfw_domain = (ROOT / "templates/ngfw-domain.xml.j2").read_text()
        mngt_domain = (ROOT / "templates/mngt-domain.xml.j2").read_text()
        self.assertIn('ngfw_lab_ngfw_uuid: "c365a5c0-2dd4-4a1f-8ae7-4fd9b9b506d5"', defaults)
        self.assertIn('ngfw_lab_mngt_uuid: "eee9f2dc-4fdf-4d1b-a91c-f3c6b571e2fa"', defaults)
        self.assertIn("<uuid>{{ ngfw_lab_ngfw_uuid }}</uuid>", ngfw_domain)
        self.assertIn("<uuid>{{ ngfw_lab_mngt_uuid }}</uuid>", mngt_domain)

    def test_ngfw_interface_order_matches_appliance_roles(self):
        domain = (ROOT / "templates/ngfw-domain.xml.j2").read_text()
        management = domain.index('mac address="52:54:00:77:00:20"')
        sync = domain.index('mac address="52:54:00:77:30:07"')
        left = domain.index('mac address="52:54:00:77:10:01"')
        right = domain.index('mac address="52:54:00:77:20:01"')
        unused = domain.index("{% for nic_number in range(3, 7) %}")
        self.assertLess(management, sync)
        self.assertLess(sync, left)
        self.assertLess(left, right)
        self.assertLess(right, unused)

    def test_lab_operator_access_is_managed_without_sudo(self):
        tasks = (ROOT / "tasks/main.yml").read_text()
        defaults = (ROOT / "defaults/main.yml").read_text()
        guide = (ROOT.parents[1] / "docs/ngfw-auditd-pilot.md").read_text()
        task_start = tasks.index("- name: Grant PT NGFW lab operators access to system libvirt")
        task_end = tasks.index("\n- name:", task_start + 1)
        operator_task = tasks[task_start:task_end]
        self.assertIn("groups:\n      - libvirt", operator_task)
        self.assertIn("append: true", operator_task)
        self.assertIn("ngfw_lab_operator_users:", defaults)
        self.assertNotIn("sudo virsh", guide)
        self.assertIn("virsh -c qemu:///system start pt-ngfw-mngt", guide)

    def test_libvirt_definitions_only_run_for_changed_xml(self):
        tasks = (ROOT / "tasks/main.yml").read_text()
        network_start = tasks.index("- name: Define isolated libvirt networks")
        network_end = tasks.index("\n- name:", network_start + 1)
        domain_start = tasks.index("- name: Define PT NGFW virtual machines")
        domain_end = tasks.index("\n- name:", domain_start + 1)
        self.assertIn("\n  when: item.changed\n", tasks[network_start:network_end])
        self.assertIn("\n    - item.changed\n", tasks[domain_start:domain_end])

    def test_aggregate_tcp_bitrate_not_multiplied_by_streams(self):
        cfg = r.validate(config())
        cmd = r.command(cfg, cfg["scenarios"][0], 600)
        self.assertEqual(cmd[cmd.index("-b") + 1], "25000000")

    def test_udp_small_payload_explicit(self):
        cmd = r.command(config(), dict(kind="udp", mbps=5, length=64), 10)
        self.assertEqual(cmd[cmd.index("-l") + 1], "64")

    def test_bad_limits_and_duration_fail_before_workload(self):
        for key, value in (("duration", 0), ("repetitions", -1), ("warmup", 99999)):
            cfg = config()
            cfg[key] = value
            with self.assertRaises(ValueError):
                r.validate(cfg)

    def test_isolation_topology_and_age(self):
        evidence = dict(checked_at=1, topology={"network": "a"})
        r.validate_isolation(evidence, {"network": "a"}, 5)
        for topology, now in (({"network": "b"}, 5), ({"network": "a"}, 90000)):
            with self.assertRaises(r.Abort):
                r.validate_isolation(evidence, topology, now)

    def test_baseline_uses_measurement_median_only(self):
        baseline = dict(status="completed", signature="a", results=[
            dict(phase="warmup", scenario="tcp", metrics=dict(received_bps=1)),
            dict(phase="measure", scenario="tcp", metrics=dict(received_bps=100)),
            dict(phase="measure", scenario="tcp", metrics=dict(received_bps=200))])
        self.assertEqual(r.baseline_values(baseline, "a"), {"tcp": 150})
        with self.assertRaises(r.Abort):
            r.baseline_values(baseline, "b")

    def test_iperf_receiver_not_sender_and_missing_summary(self):
        raw = dict(end=dict(sum_received=dict(bits_per_second=12, seconds=2),
                            sum_sent=dict(bits_per_second=20, retransmits=3)))
        self.assertEqual(r.result_metrics(dict(kind="tcp"), raw)["received_bps"], 12)
        with self.assertRaises(r.Abort):
            r.result_metrics(dict(kind="tcp"), {"end": {}})

    def test_matrix_order_repeats_and_idle(self):
        cfg = config()
        runner = r.Runner(cfg, None, None, {}, None, {})
        events = []
        runner.workload = lambda s, n, phase, seconds: events.append((n, phase, seconds))
        runner.pause = lambda seconds: events.append(("idle", seconds))
        runner.monitor = lambda: None
        runner.run()
        self.assertEqual(events, [event for n in (1, 2, 3) for event in
                                  ((n, "warmup", 1), (n, "measure", 1), ("idle", 0))])

    def test_partial_reports_are_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = dict(status="aborted", mode="traffic-only", audit_profile="P0", started_at="test",
                          results=[], gaps=["AuditD unavailable"], reason="management unavailable")
            r.write_report(Path(tmp), report)
            self.assertEqual(json.loads((Path(tmp) / "summary.json").read_text())["status"], "aborted")
            self.assertIn("management unavailable", (Path(tmp) / "report.md").read_text())

    def test_aborted_monitor_stops_remote_job_not_only_docker_cli(self):
        class Process:
            returncode = None

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = 1

            def wait(self, timeout):
                return self.returncode

        class Backend:
            compose = ["docker", "compose"]

            def endpoint(self, service, argv):
                self.stopped = (service, argv)

        with tempfile.TemporaryDirectory() as tmp:
            backend = Backend()
            runner = r.Runner(config(), backend, Path(tmp), {"gaps": []}, None, {})
            calls = [0]

            def monitor():
                calls[0] += 1
                if calls[0] == 2:
                    raise r.Abort("AuditD lost is nonzero")

            runner.monitor = monitor
            with patch.object(r.subprocess, "Popen", return_value=Process()):
                with self.assertRaisesRegex(r.Abort, "lost"):
                    runner.workload(config()["scenarios"][0], 1, "measure", 1)
            self.assertEqual(backend.stopped[0], "traffic-client")
            self.assertEqual(backend.stopped[1][2], "stop")

    def test_failed_cli_still_cancels_remote_job(self):
        class Process:
            returncode = 1

            def poll(self):
                return 1

        class Backend:
            compose = ["docker", "compose"]

            def endpoint(self, service, argv):
                self.stopped = argv

        with tempfile.TemporaryDirectory() as tmp:
            backend = Backend()
            runner = r.Runner(config(), backend, Path(tmp), {"gaps": []}, None, {})
            runner.monitor = lambda: None
            with patch.object(r.subprocess, "Popen", return_value=Process()):
                with self.assertRaises(r.Abort):
                    runner.workload(config()["scenarios"][0], 1, "measure", 1)
            self.assertEqual(backend.stopped[2], "stop")


class Endpoints(unittest.TestCase):
    def test_dns_is_static_and_checks_transaction(self):
        query = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + e.QUESTION
        self.assertEqual(e.dns_answer(query)[:2], b"\x12\x34")
        self.assertIsNone(e.dns_answer(query[:-1]))
        self.assertIsNone(e.dns_answer(query.replace(b"invalid", b"example")))

    def test_bounded_workload_records_errors(self):
        with patch.object(e, "request", side_effect=OSError("unavailable")):
            result = e.workload("http", "127.0.0.1", .05, 100, 1)
        self.assertGreater(result["errors"], 0)
        with self.assertRaises(r.Abort):
            r.result_metrics(dict(kind="http"), result)


if __name__ == "__main__":
    unittest.main()
