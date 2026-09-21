import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'files'))
import campaign as c
import measurement


def runs():
    result = []
    for i, (profile, cpu) in enumerate([('A', 10), ('B', 40), ('C-NET', 15), ('A', 10)]):
        summary = dict(status='completed', audit_profile=profile, signature='matrix', measurement_config={'threshold': 1},
                       tool_sha256={'tool': 'hash'}, run_id='run-' + str(i), started_at=str(i*2), finished_at=str(i*2+1),
                       context=dict(profile_id=profile, campaign_id='campaign', comparison_id='block', workload_id='tcp'))
        inventory = dict(inventory=dict(rules_sha256=profile, auditd_conf_sha256='conf', forwarder_conf_sha256='forward'))
        windows = {'tcp/' + str(n): dict(cpu_pct=cpu, achieved_work=100, issues=[]) for n in range(1, 4)}
        result.append((summary, inventory, windows))
    return result


class Campaign(unittest.TestCase):
    def check(self, values):
        with patch.object(c, 'windows', side_effect=values):
            return c.evaluate(['A1', 'B', 'C', 'A2'])

    def test_numeric_success_not_automatic_causal_verdict(self):
        value = self.check(runs())
        self.assertTrue(value['all_numeric_conditions_met'])
        self.assertAlmostEqual(value['windows'][0]['removed_added_cpu_pct'], 83.3333333)
        self.assertEqual(value['verdict'], 'REQUIRES_SEMANTIC_REVIEW')

    def test_less_work_never_passes(self):
        value = runs()
        value[2][2]['tcp/1']['achieved_work'] = 75
        self.assertFalse(self.check(value)['all_numeric_conditions_met'])

    def test_noise_and_small_denominator_do_not_pass(self):
        value = runs()
        value[1][2]['tcp/2']['cpu_pct'] = 14
        self.assertFalse(self.check(value)['all_numeric_conditions_met'])

    def test_missing_or_incompatible_pair_rejected(self):
        value = runs()
        del value[2][2]['tcp/3']
        with self.assertRaises(ValueError):
            self.check(value)
        value = runs()
        value[1][0]['measurement_config'] = {'threshold': 2}
        with self.assertRaises(ValueError):
            self.check(value)

    def test_real_artifacts_hash_and_window_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summary = runs()[0][0]
            summary.update(config={'limits': {'udp_loss_pct': .5}}, results=[dict(phase='measure', scenario='tcp', repetition=1,
                window_started_monotonic=0, window_finished_monotonic=20, metrics={'received_bps': 100})])
            inventory = runs()[0][1] | {'boot_id': 'boot'}
            for name, value in [('summary', summary), ('inventory-before', inventory), ('inventory-after', inventory)]:
                (root / (name + '.json')).write_text(json.dumps(value), encoding='utf-8')
            rows = [dict(monotonic=i, phase='measure', scenario='tcp', repetition=1, measurement={'guest':
                    {'boot_id': 'boot', 'audit': {'lost': 0}, 'derived': {'cpu': {'cpu': {'busy_pct': 20}}, 'gaps': []}}}) for i in range(0, 21, 5)]
            (root / 'metrics.ndjson').write_text('\n'.join(map(json.dumps, rows)), encoding='utf-8')
            (root / 'manifest.json').write_text(json.dumps(measurement.manifest(root)), encoding='utf-8')
            self.assertEqual(c.windows(root)[2]['tcp/1']['cpu_pct'], 20)
            self.assertFalse(c.windows(root)[2]['tcp/1']['issues'])
            (root / 'summary.json').write_text('{}', encoding='utf-8')
            with self.assertRaises(ValueError):
                c.windows(root)


if __name__ == '__main__':
    unittest.main()
