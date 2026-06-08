#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/ubuntu_ansible_palybooks}"
PLAYBOOK="${1:-playbooks/site.yml}"
INVENTORY="${INVENTORY:-inventories/local/hosts.yml}"
EXTRA_VARS_FILE="${EXTRA_VARS_FILE:-/etc/ansible/local-overrides.yml}"

cd "$REPO_DIR"

if [ -d .git ]; then
  git pull --ff-only
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

# shellcheck source=/dev/null
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install ansible
ansible-galaxy collection install -r collections/requirements.yml

EXTRA_ARGS=()
if [ -f "$EXTRA_VARS_FILE" ]; then
  EXTRA_ARGS+=(--extra-vars "@$EXTRA_VARS_FILE")
fi

ansible-playbook -i "$INVENTORY" "$PLAYBOOK" "${EXTRA_ARGS[@]}"
