import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('native_route', Path(__file__).resolve().parents[1] / 'files/route.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

SOURCE = (b'# PT supplied\r\n[agent]\r\n  interval = "10s"\r\n'
          b'# [[outputs.influxdb]]\r\n[[outputs.influxdb]] # preserve\r\n'
          b'  urls = [\r\n    "http://127.0.0.1:8086", # local\r\n  ]\r\n'
          b'  database = "telegraf"\r\n  username = "synthetic-user"\r\n'
          b'  password = "synthetic-test-secret"\r\n  skip_database_creation = true\r\n'
          b'[[inputs.cpu]]\r\n  percpu = true\r\n')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def portable_writer(path, data, owner):
    # Transaction logic is portable; POSIX atomic metadata implementation is
    # tested separately on Linux CI, never simulated as guest live evidence.
    path.write_bytes(data)
    os.chmod(path, owner['mode'])


class TransformTests(unittest.TestCase):
    def test_one_url_only_byte_preserved_and_no_secret_result(self):
        candidate, result = r.transform(SOURCE, digest(SOURCE))
        self.assertEqual(candidate, SOURCE.replace(r.SOURCE.encode(), r.DESTINATION.encode(), 1))
        self.assertNotIn('synthetic', json.dumps(result))
        self.assertEqual(r.parse(candidate)['agent']['interval'], '10s')
        self.assertEqual(r.parse(candidate)['outputs']['influxdb'][0]['password'], 'synthetic-test-secret')

    def test_hash_mismatch_and_missing_hash_reject(self):
        for expected in ('', 'a' * 64):
            with self.assertRaises(r.RouteError):
                r.transform(SOURCE, expected)

    def test_multiple_output_and_url_reject(self):
        duplicate = SOURCE + b'[[outputs.influxdb]]\nurls=["http://127.0.0.1:8086"]\n'
        with self.assertRaisesRegex(r.RouteError, 'exactly_one_outputs'):
            r.transform(duplicate, digest(duplicate))
        multiple = SOURCE.replace(b'"http://127.0.0.1:8086",', b'"http://127.0.0.1:8086", "http://other",')
        with self.assertRaisesRegex(r.RouteError, 'exactly_one_expected_url'):
            r.transform(multiple, digest(multiple))

    def test_wrong_destination_and_malformed_source_reject(self):
        with self.assertRaisesRegex(r.RouteError, 'allowlisted'):
            r.transform(SOURCE, digest(SOURCE), 'http://10.77.0.2:8086')
        with self.assertRaisesRegex(r.RouteError, 'invalid_toml'):
            r.transform(b'[invalid', digest(b'[invalid'))

    def test_idempotent_only_exact_inverse_hash(self):
        candidate, _ = r.transform(SOURCE, digest(SOURCE))
        same, report = r.transform(candidate, digest(SOURCE))
        self.assertEqual(same, candidate)
        self.assertFalse(report['changed'])
        modified = candidate.replace(b'interval = "10s"', b'interval = "1s"')
        with self.assertRaisesRegex(r.RouteError, 'sha256_mismatch'):
            r.transform(modified, digest(SOURCE))

    def test_additional_output_in_includes_reject(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / 'extra.conf').write_bytes(b'[[outputs.influxdb]]\nurls=["http://other"]\n')
            with self.assertRaisesRegex(r.RouteError, 'additional_influxdb'):
                r.validate_include_outputs(directory)


class TransactionTests(unittest.TestCase):
    def call(self, directory, action='apply', **overrides):
        config, backups = directory / 'telegraf.conf', directory / 'backups'
        if not config.exists():
            config.write_bytes(SOURCE)
        args = dict(validator=Mock(), ping=Mock(), active=lambda: True,
                    restart_service=Mock(), writer=portable_writer,
                    secure_dir=lambda path: path.mkdir(exist_ok=True))
        args.update(overrides)
        result = r.transaction(config, backups, digest(SOURCE), action, **args)
        return result, args

    def test_plan_validates_before_apply_without_restart_or_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            result, args = self.call(directory, 'plan')
            self.assertEqual(result['status'], 'PLAN_VALIDATED')
            self.assertEqual(args['validator'].call_count, 2)
            args['ping'].assert_called_once()
            args['restart_service'].assert_not_called()
            self.assertEqual((directory / 'telegraf.conf').read_bytes(), SOURCE)
            self.assertFalse((directory / 'backups').exists())
            self.assertEqual(list(directory.glob('.ngfw-route-candidate-*')), [])

    def test_apply_idempotent_no_second_restart_restore_exact_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / 'telegraf.conf').write_bytes(SOURCE)
            owner = r.metadata(directory / 'telegraf.conf')
            result, args = self.call(directory)
            self.assertEqual(result['status'], 'APPLIED')
            args['restart_service'].assert_called_once()
            again, second = self.call(directory)
            self.assertEqual(again['status'], 'UNCHANGED')
            second['restart_service'].assert_not_called()
            restored, final = self.call(directory, 'restore')
            self.assertTrue(restored['changed'])
            self.assertEqual((directory / 'telegraf.conf').read_bytes(), SOURCE)
            self.assertEqual(r.metadata(directory / 'telegraf.conf'), owner)
            final['restart_service'].assert_called_once()
            state = json.loads((directory / 'backups' / (digest(SOURCE) + '.json')).read_text())
            self.assertEqual(state['owner'], owner)

    def test_restart_failure_rolls_back_original_and_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            restart = Mock(side_effect=[r.RouteError('restart_failure'), None])
            with self.assertRaisesRegex(r.RouteError, 'original_restored'):
                self.call(directory, restart_service=restart)
            self.assertEqual(restart.call_count, 2)
            self.assertEqual((directory / 'telegraf.conf').read_bytes(), SOURCE)
            state = json.loads((directory / 'backups' / (digest(SOURCE) + '.json')).read_text())
            self.assertEqual(state['status'], 'ROLLED_BACK')

    def test_interrupted_restore_original_bytes_still_restarts_resident_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.call(directory)
            # Reproduce a crash after writing original bytes but before restart;
            # active service is deliberately still reported by the fake backend.
            (directory / 'telegraf.conf').write_bytes(SOURCE)
            restart = Mock()
            result, _ = self.call(directory, 'restore', restart_service=restart)
            self.assertEqual(result['status'], 'RESTORED')
            self.assertFalse(result['config_changed'])
            self.assertTrue(result['service_restarted'])
            restart.assert_called_once()

    def test_concurrent_edit_after_backup_read_not_overwritten_by_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.call(directory)
            original_read = r.read_backup
            def read_and_edit(*args):
                result = original_read(*args)
                (directory / 'telegraf.conf').write_bytes(SOURCE + b'# independent edit\n')
                return result
            restart = Mock()
            with patch.object(r, 'read_backup', side_effect=read_and_edit):
                with self.assertRaisesRegex(r.RouteError, 'source_changed_before_restore'):
                    self.call(directory, 'restore', restart_service=restart)
            restart.assert_not_called()
            self.assertTrue((directory / 'telegraf.conf').read_bytes().endswith(b'# independent edit\n'))

    def test_destination_failure_before_mutation_and_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            restart = Mock()
            with self.assertRaisesRegex(r.RouteError, 'ping_failure'):
                self.call(directory, ping=Mock(side_effect=r.RouteError('ping_failure')), restart_service=restart)
            self.assertEqual((directory / 'telegraf.conf').read_bytes(), SOURCE)
            restart.assert_not_called()
            with self.assertRaisesRegex(r.RouteError, 'original_restored'):
                self.call(directory, ping=Mock(side_effect=[None, r.RouteError('ping_failure')]))
            self.assertEqual((directory / 'telegraf.conf').read_bytes(), SOURCE)

    def test_failure_to_rollback_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            with self.assertRaisesRegex(r.RouteError, 'rollback_unconfirmed'):
                self.call(directory, restart_service=Mock(side_effect=r.RouteError('service_failed')))

    def test_atomic_apply_failure_before_change_does_not_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            restart = Mock()
            def fail_candidate(path, data, owner):
                if path.name == 'telegraf.conf' and r.DESTINATION.encode() in data:
                    raise OSError('atomic replace failed before mutation')
                portable_writer(path, data, owner)
            with self.assertRaisesRegex(r.RouteError, 'original_restored'):
                self.call(directory, writer=fail_candidate, restart_service=restart)
            restart.assert_not_called()
            self.assertEqual((directory / 'telegraf.conf').read_bytes(), SOURCE)

    def test_source_changed_during_validation_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            config = directory / 'telegraf.conf'
            config.write_bytes(SOURCE)
            def ping():
                config.write_bytes(SOURCE + b'# concurrent edit\n')
            with self.assertRaisesRegex(r.RouteError, 'changed_during_preflight'):
                self.call(directory, ping=ping)
            self.assertTrue(config.read_bytes().endswith(b'# concurrent edit\n'))

    def test_prepared_transaction_not_treated_as_idempotent_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.call(directory)
            statepath = directory / 'backups' / (digest(SOURCE) + '.json')
            state = json.loads(statepath.read_text())
            state['status'] = 'PREPARED'
            statepath.write_text(json.dumps(state))
            with self.assertRaisesRegex(r.RouteError, 'incomplete_transaction'):
                self.call(directory)

    @unittest.skipUnless(os.name == 'posix', 'POSIX uid/gid/atomic replace contract verified on Linux')
    def test_posix_atomic_metadata_preservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'target'
            owner = {'uid': os.getuid(), 'gid': os.getgid(), 'mode': 0o640}
            r.atomic_write(path, SOURCE, owner)
            self.assertEqual(path.read_bytes(), SOURCE)
            self.assertEqual(r.metadata(path), owner)
            r.atomic_write(path, SOURCE + b'\n', owner)
            self.assertEqual(r.metadata(path), owner)


if __name__ == '__main__':
    unittest.main()
