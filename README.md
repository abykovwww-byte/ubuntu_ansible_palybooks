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
- Fill `nginx_apps` with real domains and upstream ports.
- Set `docker_users` and optional Docker daemon settings.
- Decide whether `hardening_manage_ufw` should be enabled.
- Decide whether `coolify_enabled` should be enabled and fill Coolify variables.
- Create encrypted `vault.yml` from `vault.example.yml` for secrets.
