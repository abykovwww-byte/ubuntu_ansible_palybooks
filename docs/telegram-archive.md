# Shared PostgreSQL and Telegram Archive storage

The optional `context_postgres` role runs one version-pinned PostgreSQL 17
instance. `telegram_archive` adds the `tgarchive` database, its three roles,
and two SFTP accounts. Both are disabled by default; enabling the archive
also enables its shared instance. A later context-registry database can add
its exact client rule and bootstrap to this instance without another container.
No Telegram client, session, API credential or message collector runs on the server.

## Boundaries

| Resource | Location and access |
|---|---|
| Compose/config | `/srv/apps/context-postgres`, root-owned |
| PostgreSQL data | `/srv/app-data/context-postgres/data`, private to container UID 999 |
| Server secret files | `/srv/app-data/context-postgres/secrets`, directory 0700, files 0400 |
| Database listener | `100.117.52.16:5432`, exact Tailscale bind, one Compose bridge |
| Client authentication | `pg_hba` allows only the configured laptop `/32` and named database/roles, SCRAM; other TCP sources rejected |
| Local administration | Container-local postgres OS identity via Unix peer authentication; no remote superuser HBA entry |
| Archive owner | `tg_owner`: owns database/schema, runs migrations |
| Archive writer | `tg_writer`: SELECT/INSERT/UPDATE/DELETE and sequence access, no schema CREATE or TEMP privilege |
| Archive reader | `tg_reader`: SELECT only, read-only default and 5s statement timeout; no role membership granting writes |
| SFTP chroot | `/srv/data/tg-archive`, root:root 0755 |
| Media directory | `/srv/data/tg-archive/media`, tgmedia-rw:tgmedia 2750; client path `/media` |
| SFTP write account | `tgmedia-rw`, forced internal-sftp, umask 0027, no shell/forwarding |
| SFTP read account | `tgmedia-ro`, forced internal-sftp -R, no shell/forwarding |
| SFTP source check | SSH Match denies both archive users outside the laptop's exact Tailscale IP; root-owned key files additionally use `from="<laptop Tailscale IP>",restrict` |

`/srv/app-data` is group-writable in this repository. OpenSSH requires every
chroot ancestor to be root-owned and not writable by group/others, so it cannot
be used as the SFTP chroot parent. The role validates existing ancestors before
creating `/srv/data/tg-archive` and does not weaken shared directory permissions.
See [OpenSSH ChrootDirectory](https://man.openbsd.org/sshd_config#ChrootDirectory).

The Compose bridge is dedicated to this instance and not attached to other app
networks. The published host address and HBA provide its inbound boundary.
See [Docker port publishing](https://docs.docker.com/engine/network/port-publishing/)
and [PostgreSQL HBA](https://www.postgresql.org/docs/17/auth-pg-hba-conf.html).
Docker does not persist PostgreSQL stdout/stderr (`logging.driver: none`):
an SQL error DETAIL can contain row text even with statement logging disabled.
Ordinary SQL error logging and parameter/statement logging are disabled in PostgreSQL.
Bootstrap output is covered by Ansible `no_log`. Image `postgres:17.11-bookworm`
is pinned to the upstream [17.11 release](https://www.postgresql.org/docs/release/17.11/).

No backups are configured for this archive, as required by its specification.
The shared instance must gain a separate explicit backup policy if a future
consumer needs one; this archive role does not decide it for other consumers.

## Prepare and apply

1. From the laptop project run `.venv\Scripts\python.exe scripts\prepare_phase1.py`.
   It creates/reuses two private SSH keys under the user's `.ssh`, writes only
   public keys to this repository, fills separate ignored `sync.env`/`mcp.env`,
   and stages a 0700 directory with a 0600 overrides fragment on the server.
   Existing foreign DSNs and private keys are not overwritten.
2. Include the public keys in the reviewed IaC branch, commit/push it, open a
   non-draft PR, wait for green CI, then merge. Production application code stays
   in the local `telegram-archive` project; only IaC, tests and public keys go
   into this repository.
3. After merge, the owner runs the generated `sudo sh .../apply.sh` command
   interactively on abykovserv. It appends the new role variables to
   `/etc/ansible/local-overrides.yml` without printing them, removes staging,
   and starts `ansible-local-apply.service`. Never send a sudo password to Codex.
4. Inspect apply status and run local `scripts\verify_phase1.py`. It creates
   only uniquely named synthetic acceptance data, removes it afterward, and
   verifies DB privileges, SFTP read/write boundaries, shell denial and rejection
   from a non-laptop source. Do not call Phase 1 accepted from CI or a running container alone.
5. `TG_DB_OWNER_DSN` is needed only for acceptance/migrations. Remove it from
   `sync.env` after the owner migrations are finished; the scheduled collector
   runs only with writer credentials. MCP uses only its reader env/key.

Preparation is not deployment: it does not run sudo, start services, read
the existing server overrides, copy a Telegram session or access Telegram.
All secret-bearing Ansible tasks use `no_log`; passwords enter PostgreSQL from
mounted private files, never from committed SQL literals or process arguments.
Bootstrap is repeatable for both a new and an existing database and grants
default privileges for future tables created by `tg_owner`.

Focused playbook: `playbooks/telegram-archive.yml`; normal durable delivery
uses `playbooks/site.yml` through the local apply service. Docker is a prerequisite.
Changing client identity requires updating the server-private client IP through
IaC and re-running acceptance; do not broaden it to a subnet.

## Validation and recovery

`python -m unittest discover -s tests -p test_telegram_archive.py -v` renders
security boundaries with synthetic client addresses. CI also runs
`scripts/test-telegram-archive-runtime.py`: PostgreSQL in an isolated container
with no network, no published port, tmpfs data and synthetic passwords. It
checks real grants, DDL denials, read-only bypass resistance and repeat bootstrap.
`scripts/test-telegram-archive-ssh.py` checks OpenSSH parsing and effective Match
rules without starting a daemon. Ansible syntax checks cover both playbooks.

These fixtures do not prove production source filtering or filesystem access.
The laptop live verifier after apply is required for those boundaries.

To stop archive access, the owner can stop the `context-postgres` Compose
project and revoke its two authorized key files while preparing an IaC revert.
Keep the existing abykov admin SSH session open during SSH changes. Do not
delete PostgreSQL/media data as part of rollback. A revert of this optional
role does not delete accounts or storage automatically; teardown is a separate
owner-approved operation. A shared instance must not be stopped once another
consumer depends on it.
