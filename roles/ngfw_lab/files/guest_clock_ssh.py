#!/usr/bin/env python3
"""Read clocks through an existing private authenticated SSH session, without sudo."""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess


def read_clock(control_socket):
    endpoint = Path(control_socket)
    directory, socket = endpoint.parent.lstat(), endpoint.lstat()
    if (not stat.S_ISDIR(directory.st_mode) or stat.S_IMODE(directory.st_mode) != 0o700
            or not stat.S_ISSOCK(socket.st_mode) or socket.st_mode & 0o077
            or directory.st_uid != os.getuid() or socket.st_uid != os.getuid()):
        raise ValueError('private operator-owned SSH socket required')
    source = Path(__file__).with_name('guest_clock_probe.py').read_text(encoding='utf-8')
    command = 'python3 -c ' + shlex.quote(source)
    result = subprocess.run(
        ['ssh', '-S', str(endpoint), '-o', 'ControlMaster=no', '-o', 'BatchMode=yes',
         '-o', 'ProxyCommand=false',
         '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=3',
         '-o', 'NumberOfPasswordPrompts=0', 'ngfw@10.77.0.20', command],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        check=True, timeout=5)
    if len(result.stdout) > 2048:
        raise ValueError('clock response exceeds limit')
    value = json.loads(result.stdout)
    if (set(value) != {'guest_boot_id', 'guest_epoch_ns', 'guest_monotonic_ns'}
            or not isinstance(value['guest_boot_id'], str)
            or not re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', value['guest_boot_id'])
            or any(type(value[key]) is not int or value[key] <= 0
                   for key in ('guest_epoch_ns', 'guest_monotonic_ns'))):
        raise ValueError('invalid guest clock response')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--control-socket', type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(read_clock(args.control_socket), sort_keys=True))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError):
        print(json.dumps({'error': 'guest_clock_unavailable'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
