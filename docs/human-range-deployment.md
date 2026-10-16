# Human Range

The application source is `abykovwww-byte/human-range`. IaC clones an exact reviewed
40-character `human_range_repo_version` using a server-private read-only
`human_range_repo_token`. Neither value is inferred from another application.
`human_range_enabled` stays false until the revision and server configuration are ready.
The managed checkout directory is `abykov:abykov`, mode `0750`; runtime `.env`
remains root-only. This flag enables configuration; setting it back to false skips
the application tasks and does not stop an existing stack or remove its backup cron.
Deactivation is a separate, explicit owner operation. No automatic shutdown or data
removal is added by this change.

The application exposes only nginx at `192.168.1.88:8012`. API and PostgreSQL have
no published host ports. PostgreSQL becomes healthy, then the one-shot Alembic job
succeeds before API/web startup. A migration failure stops the apply. Existing
application checkouts and databases are not reused.

Application and IaC PRs require explicit owner permission before merge; green CI
and bot review are not permission. Human Range PR #2 stays draft with auto-merge
disabled until that permission. IaC #248 was merged by the Codex lead at
2026-10-16 16:16:54 UTC (`5cdd067`): merged into main, not deployed,
`human_range_enabled: false`; activation still requires the flag and a manual command.
It is not reverted. This owner instruction supersedes the generic green-CI merge
workflow for Human Range: [curator instruction](https://github.com/abykovwww-byte/human-range/issues/1#issuecomment-6219985808).

After approved application and IaC PRs are merged, the owner enables the application
with its reviewed revision and repository token in server-local Ansible overrides,
then runs `sudo systemctl start ansible-local-apply.service`. Never paste credentials
into chat or logs. The role generates stable database/session/admin secrets under
`/etc/ansible/human-range-*` and renders a root-only `.env` without diff/log output.
An image build is keyed by the pinned application SHA and environment checksum in
`/etc/ansible/human-range-build-stamp`, written only after a successful build. A
same-SHA Git reset does not rebuild images; a failed build is retried on the next
apply. If images are removed manually, set `docker_apps_force_compose_up: true`
for the repair apply. Database readiness and the explicit migration still run.

The default replay configuration makes no provider calls. Connected profiles need
the owner's models, budget and decision threshold, the server-private OpenRouter key,
`human_range_llm_config_path: /config/llm.yml`, and explicit network enablement.
Assessment stays disabled. Public scenarios require genuine writer provenance,
policy approval and human review of each variant in the application publication flow.

Verify `/healthz`, `/readyz`, learner and expert pages after apply, then read the
authoritative journal/result for actual browser runs. CI checks do not replace that.
Backup is daily at 03:20 server time to `/srv/backups/human-range` with 14-day retention.
Manual and scheduled backups share a `flock` lock; a unique temporary file is
atomically renamed only after `pg_dump` succeeds. Concurrent invocations wait for
one another and cannot overwrite a dump created in the same second.
Run `sudo /usr/local/sbin/human-range-backup`, then
`sudo /usr/local/sbin/human-range-restore-check /srv/backups/human-range/FILE.dump`.
The restore check creates a separate empty database, restores all tables and prints
only migration/count summaries, then removes that temporary database. It never
restores over the application database. Capture this actual result before accepting П9.

Not implemented in MVP: public Internet exposure, TLS/domain provisioning, multi-admin
identity, high availability, automatic restore over production, or silent model selection.
