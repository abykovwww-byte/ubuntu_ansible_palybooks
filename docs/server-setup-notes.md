# Server Setup Notes

Date: 2026-06-08

Target server:

```text
Host: 192.168.1.88
SSH user: abykov
SSH port: 22
Hostname observed: abykovserv
```

## Chosen Operating Model

The server uses a pull-based self-hosted Ansible model:

```text
Ubuntu server -> private GitHub repository over SSH with a read-only deploy key
Ubuntu server -> applies Ansible playbooks to localhost
```

GitHub does not connect to the server. The server pulls the private repository with a server-only read-only deploy key and runs Ansible locally.

## Repository On Server

The repository is cloned here:

```text
/opt/ubuntu_ansible_palybooks
```

Remote repository:

```text
git@github.com:abykovwww-byte/ubuntu_ansible_palybooks.git
```

The checkout was verified on branch:

```text
main
```

## Installed Components

The following baseline was installed or verified:

```text
git
python3-venv
python3-pip
Ansible in /opt/ubuntu_ansible_palybooks/.venv
Docker Engine
Docker Compose plugin
Nginx
```

Observed versions during setup:

```text
Ansible core: 2.21.0
Docker: 29.5.3
Docker Compose: v5.1.4
```

Services verified:

```text
docker: active
nginx: active
```

Nginx config test was successful:

```bash
sudo nginx -t
```

## Applied Playbooks

These playbooks were run successfully on the server through the local inventory:

```bash
./scripts/apply-local.sh playbooks/bootstrap.yml
./scripts/apply-local.sh playbooks/docker.yml
./scripts/apply-local.sh playbooks/nginx.yml
```

The full site playbook was also tested through systemd and completed with no failures:

```text
failed=0
```

## Local Inventory

The pull model uses:

```text
inventories/local/hosts.yml
inventories/local/group_vars/all.yml
inventories/local/group_vars/server.yml
```

The inventory configures Ansible to run against:

```text
localhost
ansible_connection: local
```

## Local-Only Overrides

Host-specific values that must not be committed to the repository are stored here:

```text
/etc/ansible/local-overrides.yml
```

This file is owned by the server and is not part of Git.

Current safe defaults created during setup:

```yaml
server_timezone: "Europe/Moscow"
ssh_public_keys: []
hardening_manage_ssh: false
hardening_manage_ufw: false
coolify_enabled: false
```

Use this file later for real local-only values such as:

```text
real SSH public keys
local domains
Nginx app upstreams
firewall flags
Coolify enablement
private registry settings
```

Do not put secrets or real private values into the repository. The GitHub deploy key is also server-only and must remain outside Git.

## Systemd Manual Apply Service

A manual oneshot service was created:

```text
/etc/systemd/system/ansible-local-apply.service
```

Run it manually when you want the server to pull the latest GitHub changes and apply `playbooks/site.yml`:

```bash
sudo systemctl start ansible-local-apply.service
```

Check the result:

```bash
sudo systemctl status ansible-local-apply.service --no-pager -l
sudo journalctl -u ansible-local-apply.service -n 100 --no-pager
```

The service currently runs:

```bash
/opt/ubuntu_ansible_palybooks/scripts/apply-local.sh playbooks/site.yml
```

The Git safe directory setting was added at system level so root-run systemd can pull the repository:

```bash
sudo git config --system --add safe.directory /opt/ubuntu_ansible_palybooks
```

## Day-To-Day Workflow

1. Change Ansible code in GitHub or locally.
2. Push changes to `main`.
3. SSH to the server.
4. Run:

```bash
sudo systemctl start ansible-local-apply.service
```

5. Check logs:

```bash
sudo journalctl -u ansible-local-apply.service -n 100 --no-pager
```

## Safer Manual Playbook Runs

For focused runs:

```bash
cd /opt/ubuntu_ansible_palybooks
./scripts/apply-local.sh playbooks/bootstrap.yml
./scripts/apply-local.sh playbooks/docker.yml
./scripts/apply-local.sh playbooks/nginx.yml
```

For a full run:

```bash
cd /opt/ubuntu_ansible_palybooks
./scripts/apply-local.sh playbooks/site.yml
```

## Hardening Status

SSH hardening and UFW firewall were intentionally left disabled:

```yaml
hardening_manage_ssh: false
hardening_manage_ufw: false
```

Reason: this avoids accidentally locking out SSH access during the initial setup.

Recommended next step before enabling hardening:

1. Confirm SSH key login works.
2. Confirm another active SSH session is open.
3. Put the real allowed users and SSH port in local overrides.
4. Run only:

```bash
cd /opt/ubuntu_ansible_palybooks
./scripts/apply-local.sh playbooks/hardening.yml
```

## Docker Notes

The following users were configured for Docker group membership through the local inventory:

```text
abykov
deploy
```

For the current SSH session to pick up Docker group membership, log out and log back in.

Check Docker:

```bash
docker --version
docker compose version
systemctl is-active docker
```

## Nginx Notes

Nginx was installed, started, and the default site was disabled by the role.

The first reverse proxy app is configured in `inventories/local/group_vars/server.yml`:

```yaml
nginx_apps:
  - name: "task-reminder"
    server_names:
      - "task.abykov.site"
    upstream_host: "127.0.0.1"
    upstream_port: 3100
```

Add app definitions later either in the repository defaults or in:

```text
/etc/ansible/local-overrides.yml
```

Example:

```yaml
nginx_apps:
  - name: "my-app"
    enabled: true
    server_names:
      - "my-app.local"
    upstream_host: "127.0.0.1"
    upstream_port: 3000
    websocket: true
```

Then apply:

```bash
sudo systemctl start ansible-local-apply.service
```

## Task Reminder App

The first platform app is a Docker Compose task reminder site:

```text
Domain: task.abykov.site
Project: /srv/apps/task-reminder
Data: /srv/app-data/task-reminder
Internal port: 127.0.0.1:3100 -> container 3000
```

Public page:

```text
https://task.abykov.site/
```

Admin page:

```text
https://task.abykov.site/admin
```

The admin password is generated locally on the server:

```bash
sudo cat /etc/ansible/task-reminder-admin-password
```

The site shows active tasks on entry. Time triggers are checked in the browser: when a task becomes due, an in-page reminder appears and remains visible until clicked. If browser notification permission is granted, the app also sends a native browser notification.

GitHub import is configured from:

```text
https://raw.githubusercontent.com/abykovwww-byte/task.abykov.site/main/tasks.json
```

The app imports only new GitHub task ids. Existing imported ids are skipped so local app state is not overwritten. The authoring format is documented in:

```text
docs/task-github-format.md
```

If the task source repository is private, add a read-only GitHub token to `/etc/ansible/local-overrides.yml`:

```yaml
task_reminder_github_token: "github_pat_or_fine_grained_token_here"
```

## Hermes Agent

Hermes Agent is configured as a Docker Compose app:

```text
Container: hermes
Project: /srv/apps/hermes
Data: /srv/app-data/hermes
Gateway API: 127.0.0.1:8642
Dashboard: 127.0.0.1:9119
```

The service is published through Nginx:

```text
Dashboard: hermes.abykov.site -> 127.0.0.1:9119
Gateway API: api_hermes.abykov.site -> 127.0.0.1:8642
Gateway API alias: api-hermes.abykov.site -> 127.0.0.1:8642
```

Use `api-hermes.abykov.site` if DNS tooling rejects the underscore in `api_hermes.abykov.site`.

The dashboard currently runs with Hermes `--insecure` because no dashboard auth provider is configured yet. Nginx Basic Auth is enabled on both Hermes vhosts as an outer access gate.

Useful checks after apply:

```bash
docker ps --filter name=hermes
curl -fsS http://127.0.0.1:8642/health
sudo cat /etc/ansible/hermes-api-server-key
sudo cat /etc/ansible/hermes-dashboard-password
```

## OpenSearch AD Analysis

OpenSearch is configured as a Docker Compose app for AD user/group analysis:

```text
Container: ad-opensearch
Dashboards container: ad-opensearch-dashboards
Project: /srv/apps/opensearch-ad
OpenSearch API: https://127.0.0.1:9200 on the server
OpenSearch Dashboards: http://127.0.0.1:5601 on the server
Performance Analyzer: 127.0.0.1:9600 on the server
```

The raw OpenSearch API and Performance Analyzer ports stay bound to localhost. OpenSearch Dashboards is published through Nginx:

```text
Dashboard: osearch.abykov.site -> 127.0.0.1:5601
```

Create a DNS A record for `osearch.abykov.site` pointing to the server. Nginx Basic Auth is enabled as an outer access gate.

Generated local secrets:

```bash
sudo cat /etc/ansible/opensearch-ad-dashboard-password
sudo cat /etc/ansible/opensearch-ad-admin-password
```

Use SSH forwarding only for raw API access from the workstation:

```bash
ssh -L 9200:127.0.0.1:9200 abykov@192.168.1.88
```

The compose deployment uses OpenSearch and OpenSearch Dashboards image tag `3.7.0`, sets `discovery.type=single-node`, and keeps data in the Docker volume `opensearch-ad-data`.

If OpenSearch logs report a `vm.max_map_count` bootstrap error, set the host value once:

```bash
echo 'vm.max_map_count=262144' | sudo tee /etc/sysctl.d/99-opensearch-ad.conf
sudo sysctl --system
```

Useful checks after apply:

```bash
docker ps --filter name=ad-opensearch
curl -k -u admin:$(sudo cat /etc/ansible/opensearch-ad-admin-password) https://127.0.0.1:9200
curl -fsS http://127.0.0.1:5601/api/status
```

AD CSV snapshots can be loaded from the Windows export folder through the SSH tunnel:

```powershell
cd C:\Users\albykov\Documents\Пользователи\opensearch-ad
$env:OPENSEARCH_URL = "https://127.0.0.1:9200"
$env:OPENSEARCH_USER = "admin"
$env:OPENSEARCH_PASSWORD = "PASTE_SERVER_PASSWORD_HERE"
python .\load_ad_to_opensearch.py --recreate
```

## tovar.ai

tovar.ai is configured as a Docker Compose app managed by the `apps` role.

Public endpoint:

```text
http://tovar.abykov.site
```

Runtime layout:

```text
Project: /srv/apps/tovar-ai
Data: /srv/app-data/tovar-ai
Logs: /var/log/apps/tovar-ai
Internal port: 127.0.0.1:3101 -> container 3000
```

The app source is pulled from:

```text
https://github.com/abykovwww-byte/tovar.ai.git
```

If the repository is private, the app reuses `task_reminder_github_token` by
default through `tovar_ai_github_token`. The token is passed to Git through a
temporary Basic Auth HTTP header and is not written to the cloned repository
remote URL or stored inside `docker_apps`.

The OpenRouter key must stay only on the server, usually in:

```text
/etc/ansible/local-overrides.yml
```

Expected local override variables:

```yaml
tovar_ai_llm_provider: "openrouter"
tovar_ai_llm_base_url: "https://openrouter.ai/api/v1"
tovar_ai_llm_model: "deepseek/deepseek-chat"
tovar_ai_llm_api_key: "PASTE_OPENROUTER_API_KEY_HERE"
tovar_ai_llm_timeout_seconds: 60
tovar_ai_llm_max_output_tokens: 1200
tovar_ai_openrouter_site_url: "https://tovar.abykov.site"
tovar_ai_openrouter_app_name: "tovar.ai"
tovar_ai_browser_extraction_enabled: true
tovar_ai_browser_timeout_ms: 20000
tovar_ai_browser_max_excerpt_chars: 12000
tovar_ai_browser_concurrency: 2
tovar_ai_extraction_provider_order: "local,browser_use,external"
tovar_ai_extraction_fallback_min_text_chars: 800
tovar_ai_browser_use_api_key: ""
tovar_ai_browser_use_base_url: "https://api.browser-use.com/api/v3"
tovar_ai_browser_use_model: "claude-sonnet-4.6"
tovar_ai_browser_use_timeout_seconds: 180
tovar_ai_browser_use_poll_interval_seconds: 2
tovar_ai_external_browser_api_url: ""
tovar_ai_external_browser_api_key: ""
tovar_ai_external_browser_timeout_seconds: 90
```

Useful checks after apply:

```bash
docker ps --filter name=tovar-ai
curl -fsS http://127.0.0.1:3101/health
```

## RP Stack

RP Stack is configured as a Docker Compose app managed by the `apps` role.
Light GUI is the LAN client and proxies party-scoped API requests to Gateway.

LAN endpoint:

```text
http://192.168.1.88:8010
```

Runtime layout:

```text
Project: /srv/apps/rp-stack
Persistent data: /srv/app-data/rp-stack
Backups: /srv/backups/rp-stack
Port bind: 192.168.1.88:8010 -> Light GUI container 80
```

Provider API keys are not managed by this repository. Keep them only in
`/etc/ansible/local-overrides.yml` on the server.

State workflow:

```bash
cd /srv/apps/rp-stack
python3 scripts/validate-state.py
python3 scripts/validate-state.py --patch state/proposed/turn-001.json
python3 scripts/apply-state-patch.py --patch state/proposed/turn-001.json
python3 scripts/apply-state-patch.py --patch state/proposed/turn-001.json --confirm
python3 scripts/render-state-block.py
```

## Coolify Status

Coolify is currently disabled:

```yaml
coolify_enabled: false
```

Enable it only after deciding the desired install method, domain, and admin email. Put real host-specific values in:

```text
/etc/ansible/local-overrides.yml
```

## Apt Source Cleanup

During setup, `apt update` showed a warning for this third-party repository:

```text
https://apt.lizardbyte.dev noble InRelease
Could not resolve apt.lizardbyte.dev
```

The source was found here:

```text
/etc/apt/sources.list.d/lizardbyte.list
```

It was disabled by renaming it to:

```text
/etc/apt/sources.list.d/lizardbyte.list.disabled
```

After that, `sudo apt-get update` completed without the LizardByte DNS warning.

## Current Recommended Next Steps

1. Log out and log back in to refresh Docker group membership for `abykov`.
2. Decide whether app configs should live in Git or in `/etc/ansible/local-overrides.yml`.
3. Add the first Nginx app reverse proxy entry.
4. Keep hardening disabled until SSH access is fully confirmed.
5. Decide later whether to add a systemd timer for automatic periodic pull/apply. For now, manual apply is safer.
