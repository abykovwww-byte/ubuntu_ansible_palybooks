#!/usr/bin/env python3
"""Send one JSON request (stdin or --request-file) to the installed vault adapter."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--request-file', type=Path)
    parser.add_argument('--response-file', type=Path)
    args = parser.parse_args()
    request = args.request_file.read_bytes() if args.request_file else sys.stdin.buffer.read()
    value = json.loads(request)
    if value.get('op') not in {'list', 'read', 'search', 'apply'}:
        raise ValueError('Unsupported operation')
    ssh = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'System32/OpenSSH/ssh.exe' if os.name == 'nt' else Path('/usr/bin/ssh')
    result = subprocess.run([
        str(ssh), '-i', str(Path.home() / '.ssh/id_ed25519'),
        '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
        'abykov@192.168.1.88', 'python3', '/srv/apps/obsidian/vault.py'
    ], input=json.dumps(value, ensure_ascii=True).encode('utf-8'), capture_output=True, timeout=90)
    if result.returncode and not result.stdout:
        sys.stderr.buffer.write(result.stderr)
        return result.returncode
    parsed = json.loads(result.stdout)
    output = json.dumps(parsed, ensure_ascii=True, indent=2).encode('utf-8')
    if args.response_file:
        args.response_file.write_bytes(output)
    else:
        sys.stdout.buffer.write(output + b'\n')
    return result.returncode


if __name__ == '__main__':
    sys.exit(main())
