import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'files'))
import audit_evidence as a


class Evidence(unittest.TestCase):
    def test_key_attribution_and_no_payload(self):
        row = a.parse(b'type=SYSCALL msg=audit(100.100:3): key="net-test" secret=DO_NOT_EXPORT\n', 'audit', {'net-test': 'network_syscall'})
        self.assertEqual(row['group'], 'network_syscall')
        self.assertNotIn('DO_NOT_EXPORT', json.dumps(row))
        self.assertEqual(a.parse(b'type=NETFILTER_PKT msg=audit(100.100:4): x=1', 'audit', {})['group'], 'packet')

    def test_siblings_records_events_duplicates_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lines = ['type=SYSCALL msg=audit(100.100:3): key="files" SECRET',
                     'type=PATH msg=audit(100.100:3): name="SECRET"',
                     'type=USER msg=audit(100.200:4): SECRET',
                     'type=USER msg=audit(90.200:1): outside']
            source = root / 'source.log'
            source.write_text('\n'.join(lines) + '\n', encoding='utf-8')
            receiver = root / 'receiver.jsonl'
            receiver.write_text('\n'.join(json.dumps(dict(stream='auditd', source_ip='10.77.0.20',
                received_at='1970-01-01T00:01:41+00:00', raw='<header> ' + row)) for row in [lines[0], lines[1], lines[1]]) + '\n', encoding='utf-8')
            summary = a.summarize([source], root / 'a', 'audit', 'one-boot', 100, 101, {'files': 'file'})
            a.summarize([receiver], root / 'b', 'collector', 'one-boot', 100, 101, {'files': 'file'})
            self.assertEqual(summary['events'], 2)
            self.assertEqual(summary['groups']['file']['records'], 2)
            self.assertEqual(summary['groups']['file']['events'], 1)
            compare = a.compare(root / 'a', root / 'b')
            self.assertEqual(compare['matched_distinct_records'], 2)
            self.assertEqual(compare['missing_in_receiver_export'], 1)
            self.assertEqual(compare['receiver_duplicate_records'], 1)
            self.assertNotIn(b'SECRET', (root / 'a' / 'records.sqlite').read_bytes())
            self.assertNotIn('SECRET', json.dumps(summary))

    def test_malformed_and_bound_cannot_pass_silently(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root / 'input'
            log.write_bytes(b'broken\n')
            summary = a.summarize([log], root / 'out', 'audit', 'boot', 1, 2, {})
            self.assertTrue(summary['gaps'])
            with self.assertRaises(ValueError):
                a.summarize([log], root / 'limited', 'audit', 'boot', 1, 2, {}, max_bytes=2)

    def test_other_source_not_attributed_and_timestamp_required(self):
        self.assertIsNone(a.parse(json.dumps({'stream': 'ngfw'}).encode(), 'collector', {}))
        with self.assertRaises(ValueError):
            a.parse(b'no audit prefix', 'audit', {})
        with self.assertRaises(ValueError):
            a.parse(json.dumps(dict(stream='auditd', source_ip='10.77.0.20', received_at='2026-01-01',
                                    raw='type=USER msg=audit(100.1:2): x')).encode(), 'collector', {})


if __name__ == '__main__':
    unittest.main()
