# Obsidian deployment

The canonical infrastructure and this skill live in `abykovwww-byte/ubuntu_ansible_palybooks`.
Use its current AGENTS.md, deployment skill and repository work standard. Do not deploy during ordinary note edits.

The dedicated `obsidian` role and `playbooks/obsidian.yml` manage only this service. The site playbook also includes it. Delivery is branch → PR → green CI → merge → interactive operator Ansible apply → live checks. Do not use Docker permissions to bypass the interactive apply rule.

Application: `/srv/apps/obsidian`. Data: `/srv/app-data/obsidian`. Vault: `vaults/Work`. Operation log: `operations/events.ndjson` (paths/hashes, no previous contents). The user explicitly disabled backup on 2026-09-28; no backup jobs or off-host copies are created.

Container HTTP is bound only to `127.0.0.1:3200`. Tailscale Serve terminates HTTPS at the node's tailnet hostname. Normal username/password authentication is provided by the container (`CUSTOM_USER` / `FILE__PASSWORD`). No Tailscale user identity is used as application authentication. The default login is `abykov`; the generated password is stored in `/etc/ansible/obsidian-password`. The operator can retrieve it locally with `sudo cat /etc/ansible/obsidian-password`; the agent must not read, print or store it. Existing credentials are reused on subsequent applies.

If HTTPS is not enabled in the tailnet, the Serve service will fail with a Tailscale consent URL. Have the user enable HTTPS through that exact official URL; do not enable public Funnel. Re-run the focused apply after configuration is available.

Deploy from a current source checkout. In the examined Windows environment the working SSH identity is `~/.ssh/id_ed25519`, not the historical `id_ed25519_codex_abykovserv` name. Confirm hostname/identity before mutations. Use the required sandbox permission.

IaC seeds application settings only when missing and never copies a corpus over existing notes. Seed note files through the adapter after the empty vault is verified. Pilot: IDM, NGFW, Data Security plus ten task IDs; then all current TT records, keeping terminal tasks.

Validate: no-auth request gets 401; permitted user logs in; Cyrillic notes and links display; an adapter change appears in the editor; container restart preserves the page; same-content retry is a no-op; stale hashes and traversal are rejected. Require a real browser check, not just health status. Password entry may require the user; finish preparation before asking them to log in.

Rollback service by reverting the image/revision through IaC. User content rollback is not available without an independent copy: the user chose no backup. Never replace the entire vault as a service rollback.
