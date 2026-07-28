---
name: abykovserv-iac-deploy
description: Use when working on abykovserv / 192.168.1.88 deployments through the ubuntu_ansible_palybooks GitHub IaC repository, Ansible local pull model, /etc/ansible/local-overrides.yml, Docker Compose apps, RP Stack, Gateway, or Light GUI. Covers how to deploy, verify, handle secrets, and when SSH needs sandbox escalation.
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
- local workspace: `C:\Users\albykov\Documents\Tavern\ubuntu_ansible_palybooks`
- server checkout: `/opt/ubuntu_ansible_palybooks`

The core model is pull-based:

```text
local repo changes -> commit -> push to GitHub main
server -> git pull --ff-only -> Ansible against localhost -> Docker Compose apps
```

GitHub does not connect to the server. The server pulls GitHub and applies
Ansible locally.

## First Rules

- Do not start local app servers unless the user explicitly asks. The normal
  target is the live server at `192.168.1.88`.
- Any SSH command to `192.168.1.88` or `abykovserv` needs sandbox escape:
  use `sandbox_permissions: "require_escalated"` with a short justification.
  Prefer a scoped prefix rule for OpenSSH, for example
  `["C:\\Windows\\System32\\OpenSSH\\ssh.exe"]`.
- Do not put secrets, tokens, real passwords, API keys, or local-only private
  values in GitHub. Use `/etc/ansible/local-overrides.yml` on the server.
- Treat GitHub + Ansible as the source of truth. Avoid hand-editing files under
  `/srv/apps` as a permanent fix; make the change in IaC and redeploy.
- Preserve user work in the git tree. Never reset or revert unrelated changes
  unless the user explicitly asks.
- Summarize remote command output to the user; they do not see tool output.

## When More Detail Is Needed

Read `references/deployment-map.md` for exact paths, commands, app layout,
verification checks, and rollback notes.

## Normal Deployment Workflow

1. Inspect the local repo status and relevant files.
2. Make the IaC/app change in the local Git working tree.
3. Run focused local checks that do not start a local server.
4. Commit and push to `origin/main` when the change is ready.
5. SSH to `abykov@192.168.1.88` with sandbox escalation.
6. Run `sudo systemctl start ansible-local-apply.service`.
7. Check `sudo journalctl -u ansible-local-apply.service -n 100 --no-pager`.
8. Verify the deployed service on the server with Docker Compose, container
   tests, HTTP smoke checks, and Browser checks for UI work.

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
