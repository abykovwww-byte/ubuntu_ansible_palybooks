import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import unittest
from unittest.mock import Mock, patch

source = Path(__file__).resolve().parents[1] / 'files' / 'guest_clock_ssh.py'
spec = importlib.util.spec_from_file_location('clock_transport', source)
clock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(clock)


class ClockTransportTests(unittest.TestCase):
    def test_private_session_fixed_guest_no_authentication_fallback(self):
        uid = 1000
        states = [Mock(st_mode=stat.S_IFDIR | 0o700, st_uid=uid),
                  Mock(st_mode=stat.S_IFSOCK | 0o600, st_uid=uid)]
        value = {'guest_boot_id': '11111111-1111-1111-1111-111111111111',
                 'guest_epoch_ns': 123456, 'guest_monotonic_ns': 654321}
        with patch.object(clock.os, 'getuid', return_value=uid, create=True), \
                patch.object(clock.Path, 'lstat', side_effect=states), \
                patch.object(clock.Path, 'read_text', return_value='print("fixed clock")'), \
                patch.object(clock.subprocess, 'run', return_value=Mock(stdout=json.dumps(value).encode())) as run:
            self.assertEqual(clock.read_clock('/private/control'), value)
        argv = run.call_args.args[0]
        self.assertIn('ProxyCommand=false', argv)
        self.assertIn('BatchMode=yes', argv)
        self.assertEqual(argv[-2], 'ngfw@10.77.0.20')
        self.assertNotIn('sudo', argv[-1])
        self.assertEqual(run.call_args.kwargs['timeout'], 5)

    def test_other_user_or_exposed_socket_rejected_before_ssh(self):
        for mode, uid in ((stat.S_IFSOCK | 0o666, 1000), (stat.S_IFSOCK | 0o600, 1001)):
            states = [Mock(st_mode=stat.S_IFDIR | 0o700, st_uid=1000), Mock(st_mode=mode, st_uid=uid)]
            with self.subTest(mode=mode, uid=uid), \
                    patch.object(clock.os, 'getuid', return_value=1000, create=True), \
                    patch.object(clock.Path, 'lstat', side_effect=states), \
                    patch.object(clock.subprocess, 'run') as run:
                with self.assertRaisesRegex(ValueError, 'private operator-owned'):
                    clock.read_clock('/private/control')
                run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
