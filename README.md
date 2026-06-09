# Local Ubuntu Deployment Platform Ansible

Ansible foundation for a local Ubuntu server that will host application deployments. The repository is intentionally small and predictable: playbooks compose focused roles, shared settings live in `group_vars`, host-specific connection data lives in `host_vars`, and secrets are expected to be stored with Ansible Vault.

## Repository Layout

```text
.
├── ansible.cfg
├── collections/
│   └── requirements.yml
├── inventories/
│   ├── local/
│   │   ├── hosts.yml
│   │   └── group_vars/
│   │       ├── all.yml
│   │       └── server.yml
│   └── lab/
│       ├── hosts.yml
│       ├── group_vars/
│       │   ├── all.yml
│       │   ├── server.yml
│       │   └── vault.example.yml
│       └── host_vars/
│           └── server01.yml
├── playbooks/
│   ├── bootstrap.yml
│   ├── coolify.yml
│   ├── docker.yml
│   ├── hardening.yml
│   ├── nginx.yml
│   └── site.yml
├── roles/
│   ├── common/
│   ├── coolify/
│   ├── docker/
│   ├── hardening/
│   └── nginx/
├── scripts/
│   └── apply-local.sh
└── .gitignore
```

## Roles

- `common`: base packages, timezone, deploy group/user, SSH public keys, service directories.
- `hardening`: cautious SSH configuration and optional UFW firewall.
- `docker`: Docker Engine, Compose plugin, Docker group membership, optional daemon config.
- `apps`: Docker Compose app deployments under `/srv/apps`.
- `nginx`: Nginx reverse proxy sites from `nginx_apps`.
- `coolify`: optional Coolify preparation when `coolify_enabled: true`.

## Required Collections

This scaffold uses mostly `ansible.builtin` modules plus:

```bash
ansible-galaxy collection install -r collections/requirements.yml
```

`community.general.timezone` is used for timezone management, `community.general.ufw` for firewall rules, and `ansible.posix.authorized_key` for deploy user SSH keys.

## Configure Inventory

Edit `inventories/lab/hosts.yml` or `inventories/lab/host_vars/server01.yml` before running anything:

```yaml
ansible_host: "TODO_SERVER_IP_OR_DNS"
ansible_user: "TODO_SSH_USER"
ansible_port: 22
ansible_ssh_private_key_file: "~/.ssh/TODO_PRIVATE_KEY"
```

Keep shared lab defaults in `inventories/lab/group_vars/all.yml` and `inventories/lab/group_vars/server.yml`. Use `host_vars` only for values unique to one server.

## Secrets And Vault

Copy the example file and encrypt it:

```bash
cp inventories/lab/group_vars/vault.example.yml inventories/lab/group_vars/vault.yml
ansible-vault encrypt inventories/lab/group_vars/vault.yml
```

Put secrets such as sudo passwords, registry credentials, tokens, and generated admin passwords in `vault.yml`. Do not commit decrypted vault files or real secrets.

Run with vault:

```bash
ansible-playbook playbooks/site.yml --ask-vault-pass
```

## Run Playbooks

Check connectivity:

```bash
ansible server -m ping
```

Run everything:

```bash
ansible-playbook playbooks/site.yml
```

Run a focused playbook:

```bash
ansible-playbook playbooks/bootstrap.yml
ansible-playbook playbooks/docker.yml
ansible-playbook playbooks/apps.yml
ansible-playbook playbooks/nginx.yml
ansible-playbook playbooks/hardening.yml
ansible-playbook playbooks/coolify.yml
```

Dry run:

```bash
ansible-playbook playbooks/site.yml --check --diff
```

## Pull Model On The Target Server

For a self-hosted local server that pulls this public repository from GitHub and applies Ansible to itself:

```bash
sudo apt update
sudo apt install -y git python3-venv python3-pip
sudo git clone https://github.com/abykovwww-byte/ubuntu_ansible_palybooks.git /opt/ubuntu_ansible_palybooks
sudo chown -R "$USER:$USER" /opt/ubuntu_ansible_palybooks
cd /opt/ubuntu_ansible_palybooks
chmod +x scripts/apply-local.sh
./scripts/apply-local.sh playbooks/bootstrap.yml
```

Local-only overrides can be placed in `/etc/ansible/local-overrides.yml`. Do not commit that file. Use it for real SSH public keys, local domains, firewall flags, and other host-specific values that should not live in the public repository.

## Tags

- `preflight`: assert required variables before changes.
- `common`, `packages`, `timezone`, `users`, `ssh`, `directories`.
- `hardening`, `firewall`.
- `docker`, `repository`, `config`, `service`.
- `apps`, `source`, `compose`, `env`.
- `nginx`, `sites`, `validate`.
- `coolify`.

Examples:

```bash
ansible-playbook playbooks/site.yml --tags docker
ansible-playbook playbooks/site.yml --tags preflight
ansible-playbook playbooks/site.yml --skip-tags hardening
```

## Nginx Apps

Define reverse proxy entries in `inventories/lab/group_vars/server.yml`:

```yaml
nginx_apps:
  - name: "example-app"
    enabled: true
    server_names:
      - "app.example.local"
    upstream_host: "127.0.0.1"
    upstream_port: 3000
    websocket: true
```

The role renders `/etc/nginx/sites-available/<name>.conf` and links it into `sites-enabled`.

## Task Reminder App

The first bundled app is `task-reminder`, published through Nginx at:

```text
task.abykov.site
```

The app is deployed by the `apps` role as a Docker Compose project:

```text
/srv/apps/task-reminder
/srv/app-data/task-reminder
/var/log/apps/task-reminder
```

It provides:

- public task list at `/`;
- admin UI at `/admin`;
- editable tasks and time triggers;
- automatic import of new tasks from `abykovwww-byte/task.abykov.site`;
- persistent in-page reminders that stay visible until clicked;
- optional browser notifications when notification permission is granted.

The admin password and session secret are generated on the server by Ansible and are not stored in Git:

```bash
sudo cat /etc/ansible/task-reminder-admin-password
```

Apply the app through the normal pull model:

```bash
sudo systemctl start ansible-local-apply.service
```

GitHub task import reads:

```text
https://raw.githubusercontent.com/abykovwww-byte/task.abykov.site/main/tasks.json
```

The app imports only new GitHub task ids and ignores later edits to already imported ids. See `docs/task-github-format.md` for the exact JSON format.

If `abykovwww-byte/task.abykov.site` is private, set a read-only token in `/etc/ansible/local-overrides.yml`:

```yaml
task_reminder_github_token: "github_pat_or_fine_grained_token_here"
```

## Hermes Agent

Hermes Agent is deployed as a Docker Compose app when `hermes_enabled: true`.

```text
Project: /srv/apps/hermes
Data: /srv/app-data/hermes
Gateway API: 127.0.0.1:8642
Dashboard: 127.0.0.1:9119
```

Hermes is also published through Nginx:

```text
Dashboard: http://hermes.abykov.site
Gateway API: http://api_hermes.abykov.site
Gateway API alias: http://api-hermes.abykov.site
```

`api_hermes.abykov.site` is included because it was requested, but underscores are not valid in strict DNS hostnames. Prefer `api-hermes.abykov.site` if the DNS provider or browser rejects the underscore name.

The dashboard currently runs with Hermes `--insecure` because no dashboard auth provider is configured yet. Nginx Basic Auth is enabled on both Hermes vhosts as an outer access gate.

Generated local secret:

```bash
sudo cat /etc/ansible/hermes-api-server-key
sudo cat /etc/ansible/hermes-dashboard-password
```

Hermes stores its config, provider keys, sessions, skills, and memory in `/srv/app-data/hermes`, mounted into the container as `/opt/data`.

## Coolify

Coolify is disabled by default:

```yaml
coolify_enabled: false
```

When you decide to use Coolify, set:

```yaml
coolify_enabled: true
coolify_domain: "TODO_COOLIFY_DOMAIN"
coolify_email: "TODO_ADMIN_EMAIL"
coolify_install_dir: /opt/coolify
```

The role currently prepares the install directory and environment file. It intentionally does not execute a remote install script, because the approved Coolify installation method and version should be pinned before real deployment.

## Notes On Shell And Command Usage

The scaffold avoids `shell`. The Nginx role uses `ansible.builtin.command: nginx -t` and the hardening role uses `ansible.builtin.command: /usr/sbin/sshd -t` because both services provide native configuration validators and Ansible has no built-in module that fully replaces those checks. Both tasks are read-only and marked `changed_when: false`.

## Manual TODOs Before First Real Run

- Replace `ansible_host` with the local server IP or DNS name.
- Replace `ansible_user` and `ansible_ssh_private_key_file`.
- Add real `ssh_public_keys`.
- Review `deploy_user_name`, `deploy_group_name`, and `deploy_user_groups`.
- Review service directories under `/srv` and `/var/log/apps`.
- Review the bundled `task-reminder` entry in `nginx_apps` and add more apps as needed.
- Set `docker_users` and optional Docker daemon settings.
- Decide whether `hardening_manage_ufw` should be enabled.
- Decide whether `coolify_enabled` should be enabled and fill Coolify variables.
- Create encrypted `vault.yml` from `vault.example.yml` for secrets.
