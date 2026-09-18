#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  printf '%s\n' 'Run interactively with sudo; no password is accepted as an argument.' >&2
  exit 1
fi
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
if [[ $# -gt 1 || ( $# -eq 1 && $1 != --check ) ]]; then
  printf '%s\n' 'Usage: sudo bash scripts/ngfw-policy-candidate.sh [--check]' >&2
  exit 2
fi
deployment_state=$(systemctl show ansible-local-apply.service --property=ActiveState --value)
case "$deployment_state" in
  active|activating|reloading|deactivating)
    printf '%s\n' 'The general Ansible deployment is active; wait for it to finish.' >&2
    exit 1 ;;
esac
exec .venv/bin/ansible-playbook -i inventories/local/hosts.yml \
  playbooks/ngfw-policy-candidate.yml -e @/etc/ansible/local-overrides.yml "$@"
