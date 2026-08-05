# Abykovserv Deployment Map

## Host And Repository

```text
Server IP:        192.168.1.88
SSH user:         abykov
Observed host:    abykovserv
GitHub IaC repo:  https://github.com/abykovwww-byte/ubuntu_ansible_palybooks
Local checkout:   $env:USERPROFILE\Documents\Tavern\ubuntu_ansible_palybooks
Server checkout:  /opt/ubuntu_ansible_palybooks
Main branch:      main
```

The server is self-hosted and pull-based:

```text
Codex edits local checkout
-> git commit
-> git push origin main
-> SSH to server
-> sudo systemctl start ansible-local-apply.service
-> server uses a read-only deploy key, pulls GitHub, and applies Ansible to localhost
```

The private deploy key is stored only in the server account's SSH directory.
The repository-local `core.sshCommand` selects it for pull operations; the key
is not committed and has no push permission.

`scripts/apply-local.sh` performs:

```text
cd /opt/ubuntu_ansible_palybooks
git pull --ff-only
create/use .venv
install ansible
install collections/requirements.yml
load /etc/ansible/local-overrides.yml if present
ansible-playbook -i inventories/local/hosts.yml playbooks/site.yml
```

Inventory target:

```yaml
localhost:
  ansible_connection: local
```

## Sandbox Rule For SSH

Network access from the Codex sandbox is restricted. For SSH commands to the
server, use escalation:

```json
{
  "sandbox_permissions": "require_escalated",
  "justification": "Allow SSH to 192.168.1.88 to deploy or verify abykovserv?",
  "prefix_rule": ["C:\\Windows\\System32\\OpenSSH\\ssh.exe"]
}
```

Use `C:\Windows\System32\OpenSSH\ssh.exe abykov@192.168.1.88 "..."` from
PowerShell. Do not try to work around the sandbox with unrelated tools.

## Ansible Structure

Important local repo paths:

```text
playbooks/site.yml
playbooks/bootstrap.yml
scripts/apply-local.sh
inventories/local/hosts.yml
inventories/local/group_vars/all.yml
inventories/local/group_vars/server.yml
roles/apps/tasks/main.yml
roles/apps/templates/
roles/apps/files/
```

`playbooks/site.yml` applies these roles:

```text
common -> hardening -> docker -> apps -> nginx -> coolify
```

`roles/apps` is the Docker Compose deployment engine. It can:

- create managed directories;
- clone external app repositories when `repo_url` is used;
- copy bundled app source when `source_dir` is used;
- render compose files and `.env`;
- run `docker compose up -d --build`.

Bundled app source lives under:

```text
roles/apps/files/<app-name>/
```

Compose and env templates live under:

```text
roles/apps/templates/<app-name>.compose.yml.j2
roles/apps/templates/<app-name>.env.j2
roles/apps/templates/<app-name>.env.example.j2
```

## Local Overrides And Secrets

Server-only values live here:

```text
/etc/ansible/local-overrides.yml
```

Never commit this file. Use it for:

- real SSH public keys;
- local-only domains or flags;
- private GitHub tokens;
- API keys;
- host-specific firewall or hardening settings.

Examples:

```yaml
server_timezone: "Europe/Moscow"
ssh_public_keys: []
hardening_manage_ssh: false
hardening_manage_ufw: false
coolify_enabled: false
rp_stack_nvidia_api_key: "..."
```

Generated server secrets are also under `/etc/ansible/`, for example app
password files. Do not print them in final answers.

## RP Stack

Ansible variables are in `inventories/local/group_vars/server.yml`.

Key defaults:

```yaml
rp_stack_enabled: true
rp_stack_bind_host: "192.168.1.88"
rp_stack_light_gui_host_port: 8010
rp_stack_gateway_port: 8088
rp_stack_nvidia_api_base: "https://integrate.api.nvidia.com/v1"
rp_stack_nvidia_model: "z-ai/glm-5.2"
rp_stack_nvidia_model_catalog_live: true
rp_stack_nvidia_model_catalog_url: "https://build.nvidia.com/models?q=llm"
```

Runtime paths:

```text
Project:         /srv/apps/rp-stack
Persistent data: /srv/app-data/rp-stack
Gateway data:    /srv/app-data/rp-stack/gateway
Backups:         /srv/backups/rp-stack
Worldpacks:      /srv/apps/rp-stack/worldpacks
Party state:     /srv/app-data/rp-stack/state/parties
```

Services:

```text
rp-stack-light-gui    -> LAN http://192.168.1.88:8010
rp-stack-gateway      -> internal http://rp-gateway:8088
```

Light GUI proxies `/api/*` to `rp-gateway:8088`.

## Deploy Commands

From the local workstation:

```powershell
git status --short --branch
git diff
git add <files>
git commit -m "<message>"
git push origin main
```

Remote apply:

```powershell
C:\Windows\System32\OpenSSH\ssh.exe abykov@192.168.1.88 "sudo systemctl start ansible-local-apply.service"
```

Remote apply status:

```powershell
C:\Windows\System32\OpenSSH\ssh.exe abykov@192.168.1.88 "sudo systemctl status ansible-local-apply.service --no-pager -l"
C:\Windows\System32\OpenSSH\ssh.exe abykov@192.168.1.88 "sudo journalctl -u ansible-local-apply.service -n 100 --no-pager"
```

If sudo requires a password, use the already established secure local method if
available in the thread context. Never reveal the password.

## Verification

RP Stack:

```powershell
C:\Windows\System32\OpenSSH\ssh.exe abykov@192.168.1.88 "cd /srv/apps/rp-stack && docker compose ps"
C:\Windows\System32\OpenSSH\ssh.exe abykov@192.168.1.88 "cd /srv/apps/rp-stack && docker compose run --rm rp-gateway pytest"
C:\Windows\System32\OpenSSH\ssh.exe abykov@192.168.1.88 "curl -fsS -o /tmp/rp-light-gui.html -w '%{http_code} %{size_download}\n' http://192.168.1.88:8010/"
```

Useful health checks:

```bash
cd /srv/apps/rp-stack
docker inspect --format='{{.State.Health.Status}}' rp-stack-gateway
docker inspect --format='{{.State.Health.Status}}' rp-stack-light-gui
docker compose logs --tail=100 rp-gateway
docker compose logs --tail=100 rp-light-gui
```

For UI work, use Browser against `http://192.168.1.88:8010/` after deploy.
Check visible Russian text, forms, console errors, and network-backed dropdowns.

## Rollback

Preferred rollback:

1. Revert or fix the bad commit in the IaC repository.
2. Push to `origin/main`.
3. Run `sudo systemctl start ansible-local-apply.service` on the server.
4. Verify containers and HTTP endpoints.

For data loss or runtime state problems, restore from `/srv/backups/<app-name>`
or the app-specific backup path. Stop containers before restoring data, then
start them again.

Avoid manual edits under `/srv/apps/<app>` except as short-lived emergency
diagnostics; capture any real fix back into Git.
