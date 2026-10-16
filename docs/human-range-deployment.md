# Human Range

The application source is `abykovwww-byte/human-range`. IaC clones an exact reviewed
40-character `human_range_repo_version` using a server-private read-only
`human_range_repo_token`. Neither value is inferred from another application.
`human_range_enabled` stays false until the revision and server configuration are ready.

The application exposes only nginx at `192.168.1.88:8012`. API and PostgreSQL have
no published host ports. PostgreSQL becomes healthy, then the one-shot Alembic job
succeeds before API/web startup. A migration failure stops the apply. Existing
application checkouts and databases are not reused.

After green application and IaC PRs are merged, the owner enables the application
with its reviewed revision and repository token in server-local Ansible overrides,
then runs `sudo systemctl start ansible-local-apply.service`. Never paste credentials
into chat or logs. The role generates stable database/session/admin secrets under
`/etc/ansible/human-range-*` and renders a root-only `.env` without diff/log output.

The default replay configuration makes no provider calls. Connected profiles need
the owner's models, budget and decision threshold, the server-private OpenRouter key,
`human_range_llm_config_path: /config/llm.yml`, and explicit network enablement.
Assessment stays disabled. Public scenarios require genuine writer provenance,
policy approval and human review of each variant in the application publication flow.

Verify `/healthz`, `/readyz`, learner and expert pages after apply, then read the
authoritative journal/result for actual browser runs. CI checks do not replace that.
Backup is daily at 03:20 server time to `/srv/backups/human-range` with 14-day retention.
Run `sudo /usr/local/sbin/human-range-backup`, then
`sudo /usr/local/sbin/human-range-restore-check /srv/backups/human-range/FILE.dump`.
The restore check creates a separate empty database, restores all tables and prints
only migration/count summaries, then removes that temporary database. It never
restores over the application database. Capture this actual result before accepting П9.

Not implemented in MVP: public Internet exposure, TLS/domain provisioning, multi-admin
identity, high availability, automatic restore over production, or silent model selection.
