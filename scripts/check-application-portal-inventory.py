"""Verify Ansible itself discovers the portal for the actual deployment host."""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    result = subprocess.run(
        ['ansible-inventory', '-i', 'inventories/local/hosts.yml', '--host', 'localhost'],
        cwd=ROOT, capture_output=True, text=True, check=True, timeout=30
    )
    host = json.loads(result.stdout)
    if host.get('application_portal_enabled') is not True:
        raise SystemExit('Portal is not enabled in the effective localhost inventory')
    apps = host.get('application_portal_apps')
    if not isinstance(apps, list) or not apps:
        raise SystemExit('Portal catalogue is missing from the effective localhost inventory')
    if any(app.get('id') == 'hermes' for app in apps):
        raise SystemExit('Retired Hermes must not return in the effective catalogue')
    print(f'PASS: Ansible localhost inventory enables the portal with {len(apps)} applications')


if __name__ == '__main__':
    main()
