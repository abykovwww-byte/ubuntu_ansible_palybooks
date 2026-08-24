# Abykovserv Deployment Map

## Host And Repository

```text
Server IP:        192.168.1.88
SSH user:         abykov
Observed host:    abykovserv
GitHub IaC repo:  https://github.com/abykovwww-byte/ubuntu_ansible_palybooks
Local checkout:   C:\Users\Адександр\Documents\Tavern\ubuntu_ansible_palybooks
Server checkout:  /opt/ubuntu_ansible_palybooks
Main branch:      main
```

The server is self-hosted and pull-based:

```text
Codex edits local checkout
-> git commit
-> push the working codex/ branch
-> open a non-draft PR
-> wait for green CI and merge the PR into main
-> verify SSH with the explicit workstation identity
-> user runs sudo systemctl start ansible-local-apply.service interactively
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

Use
`C:\Windows\System32\OpenSSH\ssh.exe -i ~/.ssh/id_ed25519_codex_abykovserv abykov@192.168.1.88 "..."`
from PowerShell. The local SSH config also names this identity, but the agent is
stopped and disabled, so repository commands keep `-i` explicit. Do not try to
work around the sandbox with unrelated tools.

`sudo -n` fails on this host. Codex must stop at `merged` and ask the user to
run the apply interactively; never request or capture the sudo password.

## Workstation tools

`gh` 2.97.0 is authorized as `abykovwww-byte` with `gist`, `read:org`, `repo`,
and `workflow` scopes when normal network access is available. Bundled Python
3.12.13 and Node.js 24.14.0 live under
`%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\`.
Do not call the unusable PATH `python`; use `scripts/ci.ps1`, which resolves the
bundled runtime.

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
rp_stack_openrouter_api_key: "..."
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
rp_stack_openrouter_api_base: "https://openrouter.ai/api/v1"
rp_stack_openrouter_models:
  - "openrouter/auto"
  - "openrouter/free"
rp_stack_service_model_choice: "local-gemma"
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
git push -u origin <codex-branch>
gh pr create --fill
gh pr checks --watch
gh pr merge --merge --delete-branch
```

The pull request must be non-draft and merged only after CI is green. Direct
pushes to `main` are prohibited.

Remote apply:

```powershell
# Run this interactively by the user; Codex does not supply the sudo password.
C:\Windows\System32\OpenSSH\ssh.exe -i ~/.ssh/id_ed25519_codex_abykovserv abykov@192.168.1.88 "sudo systemctl start ansible-local-apply.service"
```

Remote apply status:

```powershell
C:\Windows\System32\OpenSSH\ssh.exe -i ~/.ssh/id_ed25519_codex_abykovserv abykov@192.168.1.88 "systemctl status ansible-local-apply.service --no-pager -l"
C:\Windows\System32\OpenSSH\ssh.exe -i ~/.ssh/id_ed25519_codex_abykovserv abykov@192.168.1.88 "journalctl -u ansible-local-apply.service -n 100 --no-pager"
```

The apply command is intentionally user-interactive. Never ask the user to paste
the password into Codex or make it available to automation.

## Verification

RP Stack:

```powershell
C:\Windows\System32\OpenSSH\ssh.exe -i ~/.ssh/id_ed25519_codex_abykovserv abykov@192.168.1.88 "cd /srv/apps/rp-stack && docker compose ps"
C:\Windows\System32\OpenSSH\ssh.exe -i ~/.ssh/id_ed25519_codex_abykovserv abykov@192.168.1.88 "cd /srv/apps/rp-stack && docker compose run --rm rp-gateway pytest"
C:\Windows\System32\OpenSSH\ssh.exe -i ~/.ssh/id_ed25519_codex_abykovserv abykov@192.168.1.88 "curl -fsS -o /tmp/rp-light-gui.html -w '%{http_code} %{size_download}\n' http://192.168.1.88:8010/"
```

For a production Python probe, send a local script on stdin instead of nesting
PowerShell, SSH, and Python quoting. Open SQLite only with
`file:/data/rp_gateway.db?mode=ro` and `uri=True`; do not print secret-bearing
rows.

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

1. Revert or fix the bad commit on a `codex/` branch or in an isolated worktree.
2. Push the working branch, open a non-draft PR, and merge it after CI is green.
3. Run `sudo systemctl start ansible-local-apply.service` on the server.
4. Verify containers and HTTP endpoints.

For data loss or runtime state problems, restore from `/srv/backups/<app-name>`
or the app-specific backup path. Stop containers before restoring data, then
start them again.

Avoid manual edits under `/srv/apps/<app>` except as short-lived emergency
diagnostics; capture any real fix back into Git.
