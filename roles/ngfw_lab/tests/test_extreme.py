import asyncio
import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
import socket
import tempfile
import contextlib
from unittest.mock import AsyncMock, patch
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1] / 'files/extreme'


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


p = load('plan')
l = load('load')


def config():
    offered = dict(kind='short-tcp', rate=5000, concurrency=512)
    return dict(campaign_id='test', target='10.77.20.10',
                invariants={key: 'a' * 64 for key in p.HASH_FIELDS},
                receipts={key: 'evidence.json' for key in ('isolation', 'snapshot_A', 'snapshot_B', 'rollback',
                    'profile_A', 'profile_B', 'native_collection', 'observer_impact', 'resource_reservations')},
                profiles={'A': {'edr_installed': False, 'audit_profile': 'stock'},
                          'B': {'edr_installed': True, 'audit_profile': 'full', 'audit_rule_count': 116}},
                workloads=[dict(id='short', offered=offered, control_capacity=dict(receipt='direct.json',
                    load_hash=p.fingerprint(offered), topology_hash='a' * 64, status='VALID',
                    offered_achieved=True, resources_unlimited=True))])


class PlanTests(unittest.TestCase):
    def test_pending_plan_preserves_unknown_capacity_and_refuses_readiness(self):
        import json
        result = p.materialize(json.loads((ROOT / 'pending-readiness.json').read_text()))
        self.assertTrue(result['blockers'])
        self.assertIn('http-capacity', [w['id'] for w in result['unselected_workloads']])
        self.assertEqual(result['endurance_pending'], ['A', 'B'])
    def test_three_independent_return_blocks_identical_load(self):
        result = p.materialize(config())
        self.assertEqual(result['blockers'], [])
        self.assertEqual(''.join(w['profile'] for w in result['windows']), 'ABABABABA')
        self.assertEqual(len({w['block_id'] for w in result['windows']}), 3)
        self.assertEqual(len({w['offered_hash'] for w in result['windows']}), 1)
        self.assertTrue(all(w['measure_seconds'] == 300 and w['recovery_seconds'] >= 120 for w in result['windows']))
        self.assertEqual(result['endurance_pending'], ['A', 'B'])

    def test_stopped_agent_is_not_profile_A(self):
        conf = config()
        conf['profiles']['A']['edr_installed'] = True
        self.assertTrue(any('A must' in e for e in p.readiness(conf)))

    def test_capacity_does_not_transfer_to_higher_load(self):
        conf = config()
        conf['workloads'][0]['offered']['rate'] = 10000
        self.assertTrue(any('capacity' in e for e in p.readiness(conf)))

    def test_duration_bounds_and_endurance_receipt(self):
        conf = config()
        conf['stabilization_seconds'] = 61
        with self.assertRaises(ValueError):
            p.materialize(conf)
        conf = config()
        conf['endurance_selection'] = {'A': {'offered': conf['workloads'][0]['offered'], 'stable_receipt': 'stable.json'}}
        self.assertEqual(p.materialize(conf)['endurance'][0]['measure_seconds'], 3600)

    def test_iperf_per_stream_and_udp_payload(self):
        argv = p.commands(dict(kind='udp', bps=900000000, parallel=4, payload_bytes=64),
                          '10.77.20.10', 300, 'test')[0]
        self.assertEqual(argv[argv.index('-b') + 1], '225000000')
        self.assertEqual(argv[argv.index('-l') + 1], '64')

    def test_held_no_inherited_15300_ceiling(self):
        argv = p.commands(dict(kind='held', sessions=60000, open_rate=5000), '10.77.20.10',
                          300, 'test', ['10.77.10.10', '10.77.10.11'])[0]
        self.assertIn('60000', argv)
        with self.assertRaises(ValueError):
            p.commands(dict(kind='held', sessions=60000, open_rate=5000), '10.77.20.10', 300, 'test')

    def test_mixed_avoids_iperf_server_busy(self):
        with self.assertRaises(ValueError):
            p.validate_load(dict(kind='mixed', components=[dict(kind='tcp', bps=900000000),
                dict(kind='udp', bps=50000000, payload_bytes=64), dict(kind='dns', rate=5000, concurrency=512)]))

    def test_attribution_needs_evidence_and_cpu_is_not_stop(self):
        evidence = dict(complete=True, identical_invariants=True, offered_achieved=True, cpu=100)
        self.assertEqual(p.classify(evidence), 'STABLE_LIMIT_NOT_ESTABLISHED')
        self.assertEqual(p.classify(dict(evidence, degraded=True)), 'INVALID_MEASUREMENT')
        self.assertEqual(p.classify(dict(evidence, host_limited=True)), 'HOST_LIMITED')
        self.assertEqual(p.classify(dict(evidence, safety_stop=True)), 'SAFETY_STOP')
        self.assertEqual(p.classify(dict(evidence, degraded=True, direct_control_valid=True,
                                       ngfw_limit_evidence='drop counters')), 'NGFW_LIMIT')
        self.assertEqual(p.classify(dict(evidence, offered_achieved=False)), 'INVALID_MEASUREMENT')
        self.assertEqual(p.classify(dict(evidence, offered_achieved=False, degraded=True,
                         direct_control_valid=True, ngfw_limit_evidence='drops')), 'NGFW_LIMIT')


class ClientTests(unittest.IsolatedAsyncioTestCase):
    def args(self, **kwargs):
        return SimpleNamespace(**(dict(run_id='test', kind='short-tcp', rate=20, concurrency=2,
                    duration=.2, source_ip=[], target='127.0.0.1', sessions=2, heartbeat=1) | kwargs))

    async def test_actual_success_counts_and_histogram_population(self):
        args = self.args()
        stats = l.Stats(args)
        with patch.object(l, 'request', new=AsyncMock()):
            await l.request_load(args, stats)
        self.assertGreater(stats.attempted, 0)
        self.assertEqual(sum(stats.hist), stats.success)
        self.assertEqual(stats.attempted, stats.success)

    async def test_errors_are_not_success_or_zero_latency(self):
        args = self.args()
        stats = l.Stats(args)
        with patch.object(l, 'request', new=AsyncMock(side_effect=OSError(24, 'fd exhausted'))):
            await l.request_load(args, stats)
        self.assertEqual(stats.success, 0)
        self.assertEqual(sum(stats.errors.values()), stats.attempted)
        self.assertEqual(sum(stats.hist), 0)

    async def test_held_failed_open_never_becomes_socket_count_success(self):
        args = self.args(kind='held', source_ip=['127.0.0.1'])
        stats = l.Stats(args)
        with patch.object(asyncio, 'open_connection', new=AsyncMock(side_effect=OSError(99, 'ports'))), patch.object(l, 'emit'):
            await l.held_load(args, stats)
        self.assertIsNone(stats.measure_begin)
        self.assertEqual(len(stats.confirmed), 0)
        self.assertIn('not established', stats.stop_reason)

    async def test_boundary_censored_is_not_network_failure(self):
        async def slow(*args, **kwargs):
            await asyncio.sleep(2)
        args = self.args()
        stats = l.Stats(args)
        with patch.object(l, 'request', new=slow):
            await l.request_load(args, stats)
        self.assertGreater(stats.censored, 0)
        self.assertEqual(sum(stats.errors.values()), 0)

    async def test_held_requires_matching_echoes_during_measurement(self):
        class Echo:
            def write(self, data):
                self.data = data
            async def drain(self):
                pass
            async def readexactly(self, length):
                return self.data
            def close(self):
                pass
            async def wait_closed(self):
                pass
        async def connect(*args, **kwargs):
            stream = Echo()
            return stream, stream
        args = self.args(kind='held', source_ip=['127.0.0.1'], heartbeat=.01, rate=200, duration=.08)
        stats = l.Stats(args)
        with patch.object(asyncio, 'open_connection', new=connect), patch.object(l, 'emit'):
            await l.held_load(args, stats)
        self.assertEqual(stats.final_active, 2)
        self.assertEqual(stats.minimum_active, 2)
        self.assertEqual(len(stats.confirmed), 2)
        self.assertEqual(sum(stats.hist), stats.success)
        self.assertGreater(stats.success, 2)

    def test_supervised_iperf_forced_cleanup(self):
        import subprocess
        child = Mock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired('iperf3', 3), 0]
        l.stop_child(child)
        child.terminate.assert_called_once()
        child.kill.assert_called_once()

    async def test_real_loopback_protocols_and_held_receiver_readback(self):
        # Kernel allocates unused ports; this test does not contact the lab.
        def port(kind=socket.SOCK_STREAM):
            with socket.socket(socket.AF_INET, kind) as sock:
                sock.bind(('127.0.0.1', 0))
                return sock.getsockname()[1]
        tcp, http, dns = port(), port(), port(socket.SOCK_DGRAM)
        policy_port = None
        for candidate in range(10000, 10100):
            with socket.socket() as probe:
                try:
                    probe.bind(('127.0.0.1', candidate))
                    policy_port = candidate
                    break
                except OSError:
                    pass
        self.assertIsNotNone(policy_port, 'one free bounded policy test port required')
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'receiver.json'
            args = self.args(bind='127.0.0.1', max_sessions=10, state_path=str(state),
                             tcp_port=tcp, http_port=http, dns_port=dns, with_iperf=False)
            with patch.object(l, 'emit'), patch.dict(l.os.environ, {'NGFW_POLICY_LISTEN_RANGES': f'[[{policy_port},{policy_port}]]'}):
                task = asyncio.create_task(l.serve(args))
                try:
                    for _ in range(100):
                        if task.done():
                            await task
                        try:
                            await l.request('http', '127.0.0.1', None, 1, ports=(tcp, http, dns))
                            break
                        except ConnectionRefusedError:
                            await asyncio.sleep(.02)
                    else:
                        self.fail('receiver startup timeout')
                    await l.request('short-tcp', '127.0.0.1', None, 2, ports=(tcp, http, dns))
                    await l.request('dns', '127.0.0.1', None, 3, ports=(tcp, http, dns))
                    # Preserve the original NAT/policy probe protocol on8080.
                    import json
                    for probe_port in (http, policy_port):
                        reader, writer = await asyncio.open_connection('127.0.0.1', probe_port)
                        nonce = 'a' * 32
                        writer.write(f'GET /policy-probe/{nonce} HTTP/1.1\r\nHost: lab.invalid\r\n\r\n'.encode())
                        await writer.drain()
                        head = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 2)
                        self.assertIn(b'200 OK', head)
                        response = json.loads(await asyncio.wait_for(reader.read(), 2))
                        self.assertEqual(response, dict(nonce=nonce, peer='127.0.0.1', local='127.0.0.1', port=probe_port))
                        writer.close()
                        await writer.wait_closed()
                    args = self.args(kind='held', tcp_port=tcp, source_ip=['127.0.0.1'],
                                     heartbeat=.02, rate=200, duration=.15)
                    stats = l.Stats(args)
                    await l.held_load(args, stats)
                    self.assertEqual(stats.final_active, 2)
                    self.assertEqual(stats.minimum_active, 2)
                    self.assertEqual(len(stats.confirmed), 2)
                    self.assertGreater(stats.success, 2)
                finally:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                import json
                readback = json.loads(state.read_text())
                ident = str(int.from_bytes(l.hashlib.sha256(b'test').digest()[:8], 'big'))
                self.assertEqual(readback['runs'][ident]['connections'], 2)
                self.assertGreaterEqual(readback['runs'][ident]['messages'], stats.success + 2)

    def test_histogram_overflow_retained(self):
        stats = l.Stats(self.args())
        stats.observe(9)
        self.assertEqual(stats.hist[-1], 1)


if __name__ == '__main__':
    unittest.main()
