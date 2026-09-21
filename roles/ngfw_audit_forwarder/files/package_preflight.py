"""Read-only guard for the separately approved PT NGFW 1.11.1 package bootstrap."""
import json
import os
import re
import subprocess


PACKAGES = {
    'libestr0': '0.1.11-1',
    'libfastjson4': '1.2304.0-1',
    'liblognorm5': '2.0.6-4',
    'rsyslog': '8.2302.0-1+deb12u1',
}


def validate_cache(cache):
    if cache.broken_count:
        raise ValueError('Existing broken packages require separate review')
    missing = set()
    for name, version in PACKAGES.items():
        package = cache[name]
        if package.is_installed:
            if package.installed.version != version:
                raise ValueError('Refusing to upgrade or downgrade an installed package: ' + name)
            continue
        candidate = package.candidate
        if candidate is None or candidate.version != version:
            raise ValueError('Bundled package version differs from the approved plan: ' + name)
        if not candidate.uris or any(not uri.startswith('file:/opt/pt-ngfw/ngfw-repo/')
                                     for uri in candidate.uris):
            raise ValueError('Refusing a package outside the bundled PT repository: ' + name)
        missing.add(name)
    return missing


def validate_simulation(output, missing):
    installed = set()
    configured = set()
    for line in output.splitlines():
        if line.startswith(('Remv ', 'Purg ')):
            raise ValueError('Package removal is outside approval')
        if line.startswith(('Inst ', 'Conf ')):
            match = re.fullmatch(r'(Inst|Conf) (\S+) \((\S+) .+\)', line)
            if not match:
                raise ValueError('Unrecognized package change or existing package upgrade')
            kind, name, version = match.groups()
            if name not in missing or PACKAGES.get(name) != version:
                raise ValueError('Additional package change is outside approval: ' + name)
            (installed if kind == 'Inst' else configured).add(name)
    if installed != missing or configured != missing:
        raise ValueError('Simulation does not match the exact missing package set')
    if not re.search(r'^0 upgraded, \d+ newly installed, 0 to remove and \d+ not upgraded\.$',
                     output, re.MULTILINE):
        raise ValueError('Unexpected package transaction summary')


def main():
    # Do not install python3-apt automatically or refresh the appliance repository.
    import apt
    cache = apt.Cache()
    missing = validate_cache(cache)
    packages = [name + '=' + version for name, version in PACKAGES.items()]
    result = subprocess.run(
        ['/usr/bin/apt-get', '--simulate', '--no-install-recommends', '--no-remove',
         'install', *packages], check=True, capture_output=True, text=True,
        env=dict(os.environ, LC_ALL='C'), timeout=30)
    validate_simulation(result.stdout, missing)
    print(json.dumps({'needed': bool(missing), 'packages': packages}))


if __name__ == '__main__':
    main()
