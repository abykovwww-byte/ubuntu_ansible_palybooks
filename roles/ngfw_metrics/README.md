# NGFW native metrics: collection and delivery

The server profile pins InfluxDB1.8.10 after a bounded compatibility fixture
using actual guest Telegraf1.33 output serialization:157 native LP rows,
126 points, exact field types/values/tags/ns timestamps, repeat and offline
spool backup/restore/replay passed. This is not confirmation of PT's bundled
image, continuous collection, external delivery or performance acceptance.
No external server has been selected. This role does not modify the guest
Telegraf configuration. Generic defaults remain disabled. It requires
an explicit, verified `influxdb:1.x.x@sha256:...` image plus the compatibility flag;
this is a deployment constraint, not a discovered PT-recommended version.
If PT requires another API/image family, adapt and validate the role first.

## Flow and guarantee boundary

NGFW Telegraf -> isolated host HTTP ingest -> SQLite WAL/FULL commit -> 204.
Independent ordered workers then write identical raw line-protocol bytes to
the local InfluxDB and, when configured, the external endpoint. Only explicit
integer nanosecond timestamps are accepted. The request may omit precision
(Influx v1 defaults to ns) or specify `precision=ns` or `n`. Native
measurements, tags, fields and timestamps are not rewritten. There is no run_id
tag injection. Run windows are an independent manifest.

An acknowledged batch survives sender process restart subject to the durability
of the underlying disk/filesystem. This is not a guarantee against loss of the
host/disk, a full spool, or an unacknowledged Telegraf buffer. Delivery is at least
once; identical measurement + tag set + timestamp writes in InfluxDB are
idempotent. This must be confirmed against the actual external destination.
Other APIs with additive write semantics are unsupported without a verified
idempotency adapter. A connection failure after remote commit triggers replay
of the same bytes. New destination URLs require a new named cursor: a changed
destination must never silently inherit the old destination's checkpoint.

If a sink fails, its cursor stays put and its failure count increases. The other
sink continues. A malformed or permanently rejected batch blocks that sink and
remains available for inspection; it is never silently dropped. Minimal ingress
validation checks UTF-8 and explicit timestamps, not the complete Influx grammar.
Errors in the source's field types are therefore visible sink failures. A full
spool produces HTTP 507; unavailable storage produces HTTP 503. Source retry
capacity is unknown until the installed Telegraf configuration/binary is checked.
In-memory Telegraf buffering is not crash safe. Source loss detection still needs
source internal counters, expected timestamps and the analyzer completeness check.

## Source integration prerequisites

- Confirm actual binary version, input plugins and original interval from PT's
  installed configuration; retain its hash and original output configuration.
- Verify `outputs.influxdb` supports `skip_database_creation = true` and use it.
  The proxy supports `/ping` and `/write`; it does not execute `/query` database
  creation requests. Bounded gzip and HTTP chunked framing support the installed
  output without changing its source configuration. Both wire size and decoded
  size are limited; invalid framing and damaged gzip are rejected before ACK.
- Keep explicit ns timestamps. Omitted precision, `precision=ns` and
  `precision=n` are accepted; other or duplicate precisions are rejected.
- Bind to the verified management bridge address only; set the NGFW management
  source `/32` in `ngfw_metrics_allowed_sources`. The default is loopback only.
  Non-loopback deployment checks the actual `br-ngfw-mgmt` interface/address,
  restricts the application ACL to the guest and host management `/32` addresses,
  and installs only the guest-to-host TCP18086 firewall rule. Verify negative
  access tests after apply; an assertion variable alone is not isolation proof.
- Optional ingest auth is the exact `Authorization: Token ...` header; confirm
  the installed output supports custom headers before using it. The allowlist
  always applies. Secret headers live only in server-local overrides and the
  root-owned, group-readable runtime config. HTTP is permitted only on the
  isolated management path; external transport should use verified HTTPS.
- The InfluxDB API binds only to `127.0.0.1` and is unauthenticated there. Never
  expose this endpoint to a bridge or public interface.

## Retention and storage sizing

InfluxDB default raw retention is at least 30 days. The spool retains raw batches
for at least 90 days and never prunes a batch pending for any registered sink.
`prune` is explicit; schedule it only after delivery destinations and archive
policy are fixed. SQLite freed pages remain allocated and are reused; a live
VACUUM is deliberately not automatic. Preserve the complete spool snapshot as
the reproducible archive, including exact batch payloads and cursor state.

`sizing` uses accepted byte counters grouped into completed 60-second windows,
not a fabricated telemetry rate. It reports the observed maximum minute, 24h
queue and 30/90d payload lower bounds with 30% margin. Measure SQLite/WAL/index
amplification, InfluxDB disk growth, backups, free reserve and sub-minute bursts
separately during actual peak workload. One spool stores both 90d archive and
pending delivery; its payload quota must cover the larger combined requirement,
not just 24h export. An unresolved target or unmeasured peak cannot pass the
24h guarantee. Existing resource caps are conservative starting parameters,
not observer-impact proof; tune only from matched A/B observations.

The server profile provisions a bounded bootstrap allocation: 128GiB retained
payload and 32GiB free-disk reserve, with free-space admission before apply.
The observed one-shot 113203-byte sample at the configured 10-second interval
projects about 88GB for 90 days before headroom. This is an allocation basis,
not a peak-rate measurement, a physical reservation, or retention acceptance.
`ngfw_metrics_storage_budget_basis` distinguishes bootstrap from measured-peak
provisioning; neither label replaces actual capacity and recovery evidence.

## Operator commands (on the deployed host)

Use the installed `/usr/bin/python3` and paths rendered by Ansible:

```sh
python3 metrics_spool.py status --spool /srv/app-data/ngfw-metrics/spool/metrics.sqlite
python3 metrics_spool.py sizing --spool /srv/app-data/ngfw-metrics/spool/metrics.sqlite
python3 metrics_spool.py backup --spool /srv/app-data/ngfw-metrics/spool/metrics.sqlite --file /approved/archive/run.sqlite
python3 metrics_spool.py raw-export --spool /srv/app-data/ngfw-metrics/spool/metrics.sqlite --file /approved/archive/run.lp
python3 metrics_spool.py restore --file /approved/archive/run.sqlite --spool /approved/restore/new.sqlite
python3 metrics_spool.py prune --spool /srv/app-data/ngfw-metrics/spool/metrics.sqlite --config /srv/apps/ngfw-metrics/spool.json
```

The named operator lifecycle is manual: after reviewing sink backlog and archive
backups, run the `prune` command at least daily. It reads the rendered
`archive_days` from runtime config; `--retention-days N` can explicitly override
it, and values below 90 are refused. There is no automatic pruning timer in this
disabled scaffold. Capacity admission must include the operator's maximum delay.

Commands run as the service account or an authorized operator. Backups use the
SQLite online backup API and produce SHA-256 sidecars. Restore refuses to
overwrite any existing target or WAL/SHM sidecar and verifies hash/integrity.
Restore to a new path with the sender stopped; verify the manifest and cursors
before pointing a service at it. Replaying archived points into a new temporary
database uses a new named sink, followed by the same analysis queries. Backup
tests establish spool recovery, not a live InfluxDB backup or repeated report.

For external acceptance, save actual bounded local and remote Influx API query
receipts using `epoch=ns`, `SELECT *`, and `GROUP BY *` for all observed
measurements over the same run interval. Preserve query text, endpoint identity,
receipt time and pagination/chunk completeness alongside each JSON response.
Run `python3 influx_readback.py local.json remote.json`. It compares all rows,
tags, field values and integer ns timestamps, and refuses empty/error responses.
Its equality is only for the supplied queries; truncated query receipts do not
prove global completeness. Explicit `partial: true` flags at any depth are
rejected even when both receipts are otherwise identical. No stdout/file should
contain auth tokens.

## Focused validation

`python -m unittest discover -s roles/ngfw_metrics/tests -v` includes an actual
temporary loopback HTTP destination, unavailable-then-reconnected replay,
independent sink failure, lost acknowledgement, backup/restore, quota refusal,
retention protection and ns readback. On the Windows host use the bundled
interpreter and a writable workspace TEMP. Socket tests may require the narrowly
approved loopback execution outside the sandbox. The temporary endpoint is not
the external server; it cannot satisfy external delivery acceptance.

Remaining live proof: installed PT compatibility and source buffer behavior,
local InfluxDB writes/queries, measured peak storage rate and observer impact,
source-loss alarms, actual sender process crash and machine restart, configured
external API idempotency/readback, InfluxDB backup/restore and repeat analysis.
