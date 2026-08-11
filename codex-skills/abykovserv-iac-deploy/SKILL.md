---
name: abykovserv-iac-deploy
description: Use when working on abykovserv / 192.168.1.88 deployments through the ubuntu_ansible_palybooks GitHub IaC repository, Ansible local pull model, /etc/ansible/local-overrides.yml, Docker Compose apps, RP Stack, Gateway, or Light GUI. Covers how to deploy, verify, handle secrets, preserve RP Stack Wiki documentation, and when SSH needs sandbox escalation.
metadata:
  short-description: Deploy to abykovserv through GitHub and Ansible
---

# Abykovserv IaC Deploy

Use this skill for deployment, operations, and configuration changes for the
user's home server:

- host: `192.168.1.88`
- observed hostname: `abykovserv`
- SSH user: `abykov`
- IaC repository: `https://github.com/abykovwww-byte/ubuntu_ansible_palybooks`
- local workspace: `C:\Users\Адександр\Documents\Tavern\ubuntu_ansible_palybooks`
- server checkout: `/opt/ubuntu_ansible_palybooks`

The core model is pull-based:

```text
local repo changes on a codex/ branch or in an isolated worktree -> commit
-> push the working branch -> non-draft PR -> green CI -> merge into GitHub main
server -> read-only deploy key -> git pull --ff-only -> Ansible against localhost -> Docker Compose apps
```

GitHub does not connect to the server. The server reads the private repository
with a server-only read-only deploy key and applies Ansible locally.

## First Rules

- Read `docs/repository-work-standard.md` for the checked workstation contract.
- Allow project-scoped developer tools and dependencies on Windows for editing,
  builds, tests, and validation when explicitly approved. This is not a local
  RP Stack deployment; do not start local app servers unless the user asks.
- Any SSH command to `192.168.1.88` or `abykovserv` needs sandbox escape:
  use `sandbox_permissions: "require_escalated"` with a short justification.
  Prefer a scoped prefix rule for OpenSSH, for example
  `["C:\\Windows\\System32\\OpenSSH\\ssh.exe"]`.
- Always pass the workstation identity explicitly:
  `ssh -i ~/.ssh/id_ed25519_codex_abykovserv abykov@192.168.1.88 ...`.
  The same identity is present in the local SSH config, but `ssh-agent` is
  stopped and disabled; repository instructions must not depend on agent state.
- Work on a `codex/` branch or in an isolated worktree. Push only the working
  branch, open a non-draft pull request, and merge it into `main` after CI is
  green. Direct pushes to `main` are prohibited.
- Remote `sudo` requires interactive user entry and `sudo -n` fails. Stop after
  the PR is merged and ask the user to run the Ansible apply interactively.
  Never request, log, or store the sudo password.
- Do not put secrets, tokens, real passwords, API keys, or local-only private
  values in GitHub. Use `/etc/ansible/local-overrides.yml` on the server.
- Treat GitHub + Ansible as the source of truth. Avoid hand-editing files under
  `/srv/apps` as a permanent fix; make the change in IaC and redeploy.
- Preserve user work in the git tree. Never reset or revert unrelated changes
  unless the user explicitly asks.
- For every RP Stack change, read `../rp-stack-wiki/SKILL.md` and perform its
  documentation impact gate. Significant changes must update the affected Wiki
  pages and Mermaid diagrams; documentation-neutral changes must be identified
  as such in the completion report.
- Summarize remote command output to the user; they do not see tool output.

## Decision 022 readiness gate

For RP Stack requirements, use only the readiness levels `каркас` (code exists
and module tests are green), `подключено` (execution in the real turn path), `наблюдается`
(effect in the authoritative mechanic store and in a later real-party prompt),
and `держится` (later scenes repeatedly account for the effect without drift).
Do not use bare `implemented`, `working`, `ready`, `реализовано`, `работает`, or
`готово` claims. Green CI is necessary for delivery, but is insufficient for
`наблюдается` or `держится`.

Treat `roles/apps/files/rp-stack/evals/acceptance/manifest.yml` and
`roles/apps/files/rp-stack/evals/acceptance/corpus/**` as an independent,
user-owned, read-only oracle. Never change its labels or thresholds during
implementation. Read thresholds from the manifest and report
`event_precision`, `event_recall`, `character_id_accuracy`,
`empty_scene_false_positive_rate`, `positive_trust_recall`, and
`correction_retention` separately, including per-event-class metrics when
requested there.

Do not collapse the evidence layers:

- offline uses schemas and saved responses and never invokes providers;
- provider-canary uses a real prompt and model through admin-autotest and does
  not mutate the source party;
- production-endurance uses a long live party and `causal_probe` through later
  scene consequences; only it can establish `держится`.

If the revision introduces `service_call_log`, stop after merge and before any
apply that would record live data. Obtain the user's explicit decision on log
retention and redaction depth even when CI is green or a default is configured.

## When More Detail Is Needed

Read `references/deployment-map.md` for exact paths, commands, app layout,
verification checks, and rollback notes.

For RP Stack architecture and documentation rules, use the companion
[`rp-stack-wiki` skill](../rp-stack-wiki/SKILL.md). Its human-readable Wiki is
published from `docs/wiki/README.md`.

## Normal Deployment Workflow

1. Inspect the local repo status and relevant files.
2. Make the IaC/app change in the local Git working tree.
3. Run the `rp-stack-wiki` documentation impact gate for RP Stack changes.
4. Update affected Wiki pages and Mermaid diagrams in the same change when the
   change is significant.
5. Run focused local checks that do not start a local server.
6. Commit and push the working branch when the change is ready.
7. Open a non-draft PR, wait for green CI, and merge it into `main`; do not
   leave merge-ready work on the branch.
8. Verify access with
   `ssh -i ~/.ssh/id_ed25519_codex_abykovserv abykov@192.168.1.88 hostname`.
9. If the revision introduces `service_call_log`, obtain the user's explicit
   retention and redaction decision before proceeding to apply.
10. Stop at `merged` and ask the user to run
   `sudo systemctl start ansible-local-apply.service` interactively.
11. After the user confirms apply completion, inspect status/journal and verify
   Docker Compose, container tests, HTTP, and Browser checks as appropriate.

## Key Commands

Deploy latest GitHub state on the server:

```bash
sudo systemctl start ansible-local-apply.service
```

Check deploy logs:

```bash
sudo journalctl -u ansible-local-apply.service -n 100 --no-pager
```

Focused manual apply from the server checkout:

```bash
cd /opt/ubuntu_ansible_palybooks
./scripts/apply-local.sh playbooks/site.yml
```

RP Stack verification:

```bash
cd /srv/apps/rp-stack
docker compose ps
docker compose run --rm rp-gateway pytest
curl -fsS -o /tmp/rp-light-gui.html -w '%{http_code} %{size_download}\n' http://192.168.1.88:8010/
```

## Local Overrides

Use `/etc/ansible/local-overrides.yml` for host-specific and secret values.
Common examples:

```yaml
server_timezone: "Europe/Moscow"
hardening_manage_ssh: false
hardening_manage_ufw: false
rp_stack_nvidia_api_key: "..."
```

If the user asks "what line should I add to local-overrides", answer with the
smallest YAML snippet needed and remind them not to commit it.

## RP Stack Notes

RP Stack is an app managed by the Ansible `apps` role.

- project: `/srv/apps/rp-stack`
- persistent data: `/srv/app-data/rp-stack`
- backups: `/srv/backups/rp-stack`
- Light GUI: `http://192.168.1.88:8010`
- gateway: internal container service `rp-gateway:8088`

For Light GUI frontend changes, use the Browser plugin against the live
`http://192.168.1.88:8010/` page after the remote deploy. Do not validate by
starting a local frontend server unless explicitly requested.
