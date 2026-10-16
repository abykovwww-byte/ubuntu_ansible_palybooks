#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/ubuntu_ansible_palybooks}"
PLAYBOOK="${1:-playbooks/site.yml}"
# Extra ansible-playbook arguments after the playbook are forwarded verbatim,
# e.g. `apply-local.sh playbooks/apps.yml --tags security-lab`.
shift || true
INVENTORY="${INVENTORY:-inventories/local/hosts.yml}"
# One or more colon-separated extra-vars files. The default keeps the existing
# single-file behaviour; a second fragment enables a scoped role (security-lab).
EXTRA_VARS_FILE="${EXTRA_VARS_FILE:-/etc/ansible/local-overrides.yml}"
APPLY_RETRY_ATTEMPTS="${APPLY_RETRY_ATTEMPTS:-3}"
APPLY_RETRY_DELAY_SECONDS="${APPLY_RETRY_DELAY_SECONDS:-10}"

retry() {
  local attempt=1
  local exit_code=0
  while true; do
    if "$@"; then
      return 0
    else
      exit_code=$?
    fi
    if (( attempt >= APPLY_RETRY_ATTEMPTS )); then
      printf 'Command failed after %d attempts (exit %d).\n' "$attempt" "$exit_code" >&2
      return "$exit_code"
    fi
    printf 'Command failed (attempt %d/%d); retrying in %ds.\n' \
      "$attempt" "$APPLY_RETRY_ATTEMPTS" "$APPLY_RETRY_DELAY_SECONDS" >&2
    sleep "$APPLY_RETRY_DELAY_SECONDS"
    attempt=$((attempt + 1))
  done
}

cd "$REPO_DIR"

if [ -d .git ]; then
  retry git pull --ff-only
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

# shellcheck source=/dev/null
source .venv/bin/activate
export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_DEFAULT_TIMEOUT=60
retry python -m pip install --requirement requirements-ansible.txt
retry ansible-galaxy collection install --no-cache --timeout 30 -r collections/requirements.yml

EXTRA_ARGS=()
IFS=':' read -r -a EXTRA_VARS_FILES <<< "$EXTRA_VARS_FILE"
for file in "${EXTRA_VARS_FILES[@]}"; do
  if [ -f "$file" ]; then
    EXTRA_ARGS+=(--extra-vars "@$file")
  fi
done

ansible-playbook -i "$INVENTORY" "$PLAYBOOK" "${EXTRA_ARGS[@]}" "$@"
