import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'files'))
import audit_profile_ctl as ctl


def baseline():
    rules = ['-w /etc/passwd -p wa -k admin']
    for key in sorted(ctl.NETWORK):
        rules += ['-a always,exit -F arch=b64 -S connect -F key=' + key]
    rules += ['-w /etc/shadow -p wa -k privileges',
              '-w /proc -p r -k pt_siem_proc', '-w /etc/ngfw -p wa -k config']
    text = ctl.text_rules(rules)
    return dict(rules_text=text, rules_sha256=ctl.digest(text), boot_id='boot', file_hashes={'conf': 'hash'},
                status=dict(enabled=1, failure=0, pid=42, rate_limit=0, backlog_limit=8192,
                            backlog_wait_time=0, lost=0), edr_active=True, auditd_active=True)


class Kernel:
    def __init__(self):
        self.base = baseline()
        self.rules = ctl.parse_rules(self.base['rules_text'])
        self.deleted = []
    def inventory(self):
        text = ctl.text_rules(self.rules)
        return copy.deepcopy(self.base) | {'rules_text': text, 'rules_sha256': ctl.digest(text)}
    def delete(self, rule):
        self.deleted.append(rule)
        self.rules.remove(rule)
    def add(self, rule):
        self.rules.append(rule)


class Profiles(unittest.TestCase):
    def test_exact_allowed_keys_and_ordered_restore(self):
        for profile, keys in ctl.PROFILES.items():
            kernel = Kernel()
            before = kernel.inventory()
            change = ctl.plan(before, profile, before['rules_sha256'])
            def callback():
                self.assertEqual(kernel.rules, change['remaining'])
                self.assertEqual(set().union(*(ctl.keys(r) for r in kernel.deleted)), keys)
                return 17
            self.assertEqual(ctl.apply(kernel, before, change, callback), 17)
            self.assertEqual(kernel.inventory(), before)

    def test_unexpected_hash_blocks_before_mutation(self):
        with self.assertRaisesRegex(ValueError, 'hash'):
            ctl.plan(baseline(), 'C-EDR-NET', 'bad')

    def test_error_and_interrupt_restore(self):
        for exception in (RuntimeError, KeyboardInterrupt):
            kernel = Kernel()
            before = kernel.inventory()
            change = ctl.plan(before, 'C-EDR-PROC', before['rules_sha256'])
            def fail():
                raise exception('test')
            with self.assertRaises(exception):
                ctl.apply(kernel, before, change, fail)
            self.assertEqual(kernel.inventory(), before)

    def test_partial_delete_failure_restores(self):
        kernel = Kernel()
        before = kernel.inventory()
        change = ctl.plan(before, 'C-EDR-NET', before['rules_sha256'])
        original = kernel.delete
        calls = []
        def fail_once(rule):
            calls.append(rule)
            if len(calls) == 2:
                raise RuntimeError('delete failed')
            original(rule)
        kernel.delete = fail_once
        with self.assertRaises(RuntimeError):
            ctl.apply(kernel, before, change, lambda: None)
        self.assertEqual(kernel.inventory(), before)

    def test_foreign_drift_and_unrestorable_format_fail_closed(self):
        kernel = Kernel()
        kernel.rules.append('-w /foreign -p wa -k other')
        with self.assertRaisesRegex(ValueError, 'drift'):
            ctl.restore(kernel, baseline())
        for rule in ('-e 2', '-a always,task', '-a always,exit -S all', '-A always,exit -S connect'):
            with self.assertRaises(ValueError):
                ctl.parse_rules(rule)

    def test_control_or_boot_change_never_mutates(self):
        for field in ('boot_id', 'file_hashes'):
            kernel = Kernel()
            before = kernel.inventory()
            kernel.base[field] = 'changed'
            with self.assertRaises(ValueError):
                ctl.restore(kernel, before)
            self.assertEqual(kernel.deleted, [])

    def test_A_and_D_cannot_be_invented(self):
        for profile in ('A-NO-EDR', 'D-EDR-CANDIDATE'):
            with self.assertRaises(ValueError):
                ctl.plan(baseline(), profile, baseline()['rules_sha256'])


if __name__ == '__main__':
    unittest.main()
