"""Portable tests for the read-only, exact package transaction guard."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

spec = importlib.util.spec_from_file_location(
    'package_preflight', Path(__file__).resolve().parents[1] / 'files/package_preflight.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class Cache(dict):
    broken_count = 0


def cache():
    return Cache({name: SimpleNamespace(
        is_installed=False, installed=None,
        candidate=SimpleNamespace(version=version, uris=[
            'file:/opt/pt-ngfw/ngfw-repo/./' + name + '.deb']))
        for name, version in p.PACKAGES.items()})


def simulation(names=None):
    names = list(p.PACKAGES) if names is None else names
    return '\n'.join([
        f'0 upgraded, {len(names)} newly installed, 0 to remove and 0 not upgraded.',
        *[f'{kind} {name} ({p.PACKAGES[name]} localhost [amd64])'
          for kind in ['Inst', 'Conf'] for name in names],
    ])


class PackageGuard(unittest.TestCase):
    def test_exact_bundled_plan_and_idempotent_installed_plan(self):
        c = cache()
        self.assertEqual(p.validate_cache(c), set(p.PACKAGES))
        p.validate_simulation(simulation(), set(p.PACKAGES))
        for pkg in c.values():
            pkg.is_installed = True
            pkg.installed = pkg.candidate
        self.assertEqual(p.validate_cache(c), set())
        p.validate_simulation(simulation([]), set())

    def test_subset_of_missing_packages(self):
        c = cache()
        c['rsyslog'].is_installed = True
        c['rsyslog'].installed = c['rsyslog'].candidate
        missing = p.validate_cache(c)
        p.validate_simulation(simulation(sorted(missing)), missing)

    def test_remote_mixed_missing_or_different_candidates_rejected(self):
        for uris in [[], ['https://deb.debian.org/rsyslog.deb'],
                     ['file:/opt/pt-ngfw/ngfw-repo/a.deb', 'https://example.org/a.deb']]:
            c = cache()
            c['rsyslog'].candidate.uris = uris
            with self.assertRaises(ValueError):
                p.validate_cache(c)
        for candidate in [None, SimpleNamespace(version='new', uris=[])]:
            c = cache()
            c['rsyslog'].candidate = candidate
            with self.assertRaises(ValueError):
                p.validate_cache(c)

    def test_existing_version_change_and_broken_packages_rejected(self):
        c = cache()
        c['rsyslog'].is_installed = True
        c['rsyslog'].installed = SimpleNamespace(version='old')
        with self.assertRaises(ValueError):
            p.validate_cache(c)
        c = cache()
        c.broken_count = 1
        with self.assertRaises(ValueError):
            p.validate_cache(c)

    def test_upgrade_remove_extra_configure_or_unknown_plan_rejected(self):
        good = simulation()
        for output in [
            good + '\nRemv vendor-core [1.0]',
            good + '\nPurg vendor-core [1.0]',
            good + '\nInst extra (1.0 localhost [amd64])',
            good + '\nConf extra (1.0 localhost [amd64])',
            good.replace('Inst rsyslog (', 'Inst rsyslog [old] ('),
            good.replace('Inst rsyslog (8.2302.0-1+deb12u1', 'Inst rsyslog (new'),
            good.replace('0 upgraded', '1 upgraded'),
            '', simulation(['rsyslog']),
        ]:
            with self.subTest(output=output), self.assertRaises(ValueError):
                p.validate_simulation(output, set(p.PACKAGES))


if __name__ == '__main__':
    unittest.main()
