# Private Obsidian Work vault

Obsidian runs as a pinned LinuxServer container, with a browser desktop and a persistent Markdown vault. It is a separate application from RP Stack, Showroom and the task-reminder service.

The `obsidian` role is enabled in the local inventory. It is included in `site.yml`; `playbooks/obsidian.yml` permits a focused apply without converging unrelated applications. Follow the repository's PR/CI/pull-based deployment procedure and interactive sudo rule.

## Access and data

- LAN browser: `https://192.168.1.88:3201/`.
- Tailscale browser: `https://100.117.52.16:3201/` from a connected Tailscale device.
- Both addresses open the same application and Work vault using the container's native HTTPS listener. No domain, reverse proxy or Tailscale Serve activation is required.
- Both use the container's self-signed certificate. The browser asks the user to accept it on first use; agents must not bypass that browser warning.
- Obsidian login: `abykov`, with one separate generated password shared by both addresses; no inherited Tailscale application identity.
- Password file, operator-only: `/etc/ansible/obsidian-password`. Retrieve locally with `sudo cat /etc/ansible/obsidian-password`; do not copy it to Git or agent logs.
- Docker binds container HTTPS port 3001 only to the two configured IPv4 addresses at port 3201. HTTP is not published. Configure `obsidian_lan_bind_host`, `obsidian_tailscale_bind_host` and `obsidian_browser_port`; the role checks that both IPs belong to the host. Neither listener binds all interfaces. No Funnel is configured.
- App: `/srv/apps/obsidian`; vault: `/srv/app-data/obsidian/vaults/Work`; settings: `/srv/app-data/obsidian/config`.
- No GPU, Docker socket, privileged mode or broad host mounts. Container terminal/sudo/remote command UI is disabled. The authenticated user can edit vault content and install extensions, so only the intended user should have the password.
- User decision on 2026-09-28: **no backups yet**. The file adapter keeps only audit hashes, not prior contents; content restoration is not provided.

The role stops, disables and removes the obsolete `obsidian-serve` systemd unit on upgrade. The initial application registry opens `/vault/Work`; existing settings and notes are never replaced by Ansible.

## LLM file workflow

Canonical skill: `codex-skills/obsidian-workspace`. Its `remote.py` sends structured JSON over the existing SSH identity to a root-owned adapter/config. Four operations: list, search, read, apply. Each write requires the previous content hash (or null for a new file), validates path/YAML/unique ID, stages in the same directory, atomically replaces and reads back. Symlink/junction/hardlink escapes and hidden config files are rejected. The writer lock coordinates adapters, not the human editor: simultaneous editing still requires coordination.

The installed adapter uses host `python3-yaml`; the client wrapper uses only Python's standard library. Local tests require PyYAML; no LLM provider/API key is involved. A user with general SSH access still has that account's ordinary OS permissions: the adapter is an application boundary, not an OS sandbox for arbitrary SSH commands.

`import_tasks.py` reflects selected or all TT records from an explicitly fetched snapshot. It preserves handwritten notes/custom properties, terminal history and separate Jira/local status. It does not call GitHub/Jira write APIs. Business content and snapshots are excluded from the IaC repository; they are transferred only into the private vault.

## Acceptance and operations

Check `docker compose ps` in `/srv/apps/obsidian`, no-auth HTTPS → 401 on both IP addresses, normal login, and that both addresses display the same Russian note/link/attachment. Both use the documented self-signed certificate and require user acceptance in the browser. Confirm that HTTP has no published host port and the legacy `obsidian-serve` unit is removed. Check adapter read-back and persistence after a container restart. Run the adapter/import unit suite; Linux CI covers symlink rejection. Verify repeating an import creates no extra IDs and does not erase manual notes.

To stop the application and both entry points: `docker compose stop` in its project directory. Service rollback changes the image pin in IaC and reapplies; it must not replace or delete user data. No automatic full-vault rollback exists while backups are disabled.

Sources: [LinuxServer Obsidian](https://docs.linuxserver.io/images/docker-obsidian/), [Bases](https://help.obsidian.md/bases).
