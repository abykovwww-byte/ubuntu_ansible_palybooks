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

### Owner-generated evidence without changing live access

The separate `playbooks/ngfw-metrics-evidence.yml` is opt-in and is never invoked
by the normal role or `site.yml`. After active measurement windows have closed,
an authorized operator can run it through the existing privileged local Ansible
path. It installs only `storage_evidence.py`; it does not provision/restart a
service, change the spool ACL, configure a sink, or read private `spool.json`.

Supply a private, non-secret request vars file with explicit values:

```yaml
ngfw_metrics_evidence_enabled: true
ngfw_metrics_evidence_request_id: example-storage-observation
ngfw_metrics_evidence_operator: existing_operator_account
ngfw_metrics_evidence_start_epoch: 1600000020
ngfw_metrics_evidence_end_epoch: 1600000620
```

The names/times above are placeholders, not actual host evidence. Replace them
with the existing recipient account and the real **host** bounds of a completed
workload. From the deployed checkout, using its installed Ansible environment:

```sh
sudo .venv/bin/ansible-playbook -i inventories/local/hosts.yml \
  playbooks/ngfw-metrics-evidence.yml -e @/approved/private/storage-request.yml
```

This uses the already installed spool/helper paths from inventory. It does not
require runtime credentials. The source must exist; a missing source is never
created. The account `ngfw-metrics` runs the existing online-backup CLI under an
exclusive evidence lock. Each child command has at most120 seconds; the evidence
deadline defaults to180 seconds, with an outer process-group timeout and kill
grace. The default snapshot bound is1GiB and free-space admission requires three
times that bound plus the existing live disk reserve (snapshot, working copy,
operator export). The bound and deadline are explicit variables, not measured
capacity. Partial/failed output remains private and requires a new request ID;
it is never published as a completed snapshot.

An immutable `backup.sqlite` and SHA sidecar preserve committed WAL state, exact
batch payloads and independent cursors. Restore/status/sizing operate on a new
working copy. Source file logical/allocated sizes are captured separately;
`status.json` filesystem sizes describe that working copy. The bounded
`ingest-minutes.json` includes at most10080 complete minutes (seven days), exact
accepted-byte/LP-line counters, missing minutes and the selected host bounds.
It excludes partial workload-edge and snapshot minutes. Missing minutes suppress
queue/retention estimates. The retained `sizing.json` is the existing CLI's
all-history observation, not the workload-specific peak proof.

Completed artifacts are copied by privileged Ansible into
`{{ ngfw_metrics_data }}/evidence-export/REQUEST_ID`: files0600, recipient
directory0700. Their hashes and sizes are compared to the owner originals before
handover. The root-controlled parent permits traversal but not listing or
replacement. The live spool remains0700 and no supplemental group, HTTP endpoint,
Docker bind or sudo allowlist is added. An identical completed request can be
verified/exported again; changed inputs or hashes are refused. Backups, working
copies and exported copies are not automatically deleted: retain or remove them
under the operator's evidence/archive policy after verified transfer.

`COLLECTED_SQLITE_SNAPSHOT` is a collection result, never storage acceptance.
The raw export can support an **observed-workload** peak-minute calculation:
`queue24_payload_with_30pct = peak_minute_bytes * 1872`. Physical SQLite/Influx
overhead, bursts, long-duration retention and outage/drain behavior require
separate evidence. An absent external endpoint still blocks external delivery
acceptance. Snapshot/replay tests on local fixtures are not live spool recovery.

### Individual CLI operations

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

## Native measurement-liveness health producer

`files/native_health.py` is a read-only InfluxDB API reader for the extreme-run
controller. It proves recent observations of one verified field per required
measurement for an exact observed host selector. It does **not** prove freshness
of every field/series, complete sampling through an experiment window, NTP
synchronization, throughput, or downstream EDR event receipt. The deployment
and periodic invocation are owned by the integration playbook; this component
does not install a timer, modify Telegraf, or choose credentials/SSH endpoints.

The operator supplies four paths and one private configuration. No actual host,
measurement family, field, database name, or boot ID is guessed by the code:

```sh
python3 native_health.py \
  --config /private/native-health-config.json \
  --catalog /private/native-health-catalog.json \
  --clock-argv-file /private/guest-clock-argv.json \
  --output /private/native-health-latest.json \
  --evidence-dir /private/native-health-evidence
```

The configuration requires `influx_url`, a bare HTTP(S) origin without userinfo,
query or fragment. The deployed unauthenticated loopback API needs no credentials.
Default limits are `query_timeout_seconds: 2`, `query_budget_seconds: 10`,
`max_response_bytes: 65536`, `max_uncertainty_seconds: 1`,
`max_offset_spread_seconds: 0.5`, and `max_clock_step_seconds: 0.05`.
Clock limits may be tightened; widening beyond these research gate ceilings is
refused. These are methodology limits, not PT specifications. The complete set
of queries must fit the overall monotonic budget; each clock command has its
own five-second deadline and bounded stdout/stderr. The clock adapter must be
a fixed, operator-controlled, read-only command with bounded transport behavior.

The catalog has this schema (values below are placeholders, not discovered facts):

```json
{
  "schema_version": 1,
  "database": "observed_database",
  "source_interval_seconds": 10,
  "host_selector": {"tag": "observed_host_tag", "value": "observed_host_value"},
  "measurements": [{"name": "observed_measurement", "field": "observed_field"}],
  "observation_evidence": {"path": "raw-observed-api.json", "sha256": "ACTUAL_64_HEX_SHA256"},
  "interval_evidence": {
    "path": "raw-source-config-discovery.json",
    "sha256": "ACTUAL_64_HEX_SHA256",
    "json_pointer": "/observed_interval"
  }
}
```

Both evidence references must hash-match actual source receipts. Relative paths
resolve against the catalog directory. The observation receipt must contain
raw Influx `series` objects with integer ns time, the exact selected field, a
non-null value, and the exact host tag/value; nested baseline query receipts are
supported. A field declaration without an observed point/host does not qualify.
The JSON pointer must resolve to the configured numeric interval or its `Ns`
string form in the hashed source discovery receipt. Do not point this at an
invented interval declaration. The caller owns source provenance; a hash detects
file changes but does not authenticate a fabricated source. Catalog and source
receipts are copied into the content-addressed evidence directory.

The clock argv file is a JSON array of fixed command arguments, executed without
a shell and without stdin. No SSH password, sudo, config inspection, or network
endpoint discovery is implemented here. The independent clock adapter must emit
exactly this JSON shape, with positive integer nanoseconds and the actual boot ID:

```json
{"guest_epoch_ns": 1790000000000000000, "guest_monotonic_ns": 123000000000, "guest_boot_id": "11111111-2222-4333-8444-555555555555"}
```

The producer brackets this command with actual host realtime and monotonic
timestamps before and after the bounded API-query phase. Boot IDs must match.
Guest monotonic uptime must already exceed the freshness bound (30 seconds at
the observed 10-second interval) so an immediate reboot cannot qualify a recent
ordinary preboot sample. Boot association is via the fresh clock identity and
verified host selector; native points do not gain an invented boot-ID tag.
It detects host/guest realtime-versus-monotonic steps and limits the entire
host-minus-guest offset envelope to 0.5 seconds. An absolute guest clock lag of
22 seconds is neither erased nor treated as synchronization: the offset remains
in explicit evidence. Separate before/after **experiment-window** drift checks
belong to the controller; these per-query checks cannot replace them.

For each verified measurement, the query is a bounded
`SELECT LAST("field") AS "value" ... WHERE "host_tag"='exact_host' AND time >= ... AND time <= ...`
with `epoch=ns`. The source-clock window derives from the fresh clock bracket,
not host `now()`. There is no wildcard `SELECT *`. Empty/malformed/partial,
truncated, error, wrong-measurement, null-value or float-timestamp responses fail
closed. Each received HTTP body is saved byte-for-byte with its SHA256, bounded
request/response times, SQL text, database, endpoint and completeness metadata.
No API response is silently shortened and then accepted as complete.

The latest receipt is atomically replaced after each poll, including failures.
It has `status: HEALTHY|BLOCKED`, actual `collected_at`, source interval, required
measurement names, database, boot ID, hashed `query_evidence`, and hashed
`clock_alignment`. Original source timestamps remain both integer
`source_timestamp_ns` and exact RFC3339 `time`; they are never shifted to make
the controller pass. Raw query evidence retains `query_bounds`, request receipts
and points. Alignment contains before/after host/guest clock brackets, hashed
three-field guest evidence, the same query bounds, and `query_evidence_sha256`,
binding the clock proof to that exact query receipt without a circular hash.

Freshness uses the full possible age interval
`[host_now - (source_time + offset_max), host_now - (source_time + offset_min)]`.
The entire interval must lie in `[0, 2 * source_interval + 10 seconds]`.
Controller integration must independently validate the hashes/brackets and
recompute age at consumption time; a producer's `HEALTHY` string is insufficient.
Content-addressed files are never overwritten by the producer, and existing
digest mismatches fail. This is integrity checking, not filesystem WORM storage.
Failed polls retain available raw API/clock evidence and cannot leave a prior
healthy latest receipt in place. Store-unavailable failures require the caller
to treat a missing/unchanged poll result as a failed producer invocation.

Focused tests: `python -m unittest discover -s roles/ngfw_metrics/tests -p test_native_health.py -v`.
They use injected clocks/API responses, including a guest lagging 22 seconds,
and assert unchanged source ns values, conservative freshness, raw/hash retention,
tamper rejection, partial/truncation refusal, boot/clock-step detection, explicit
catalog provenance and atomic failure publication. They are local validation;
actual database/transport polling is a separate live check.
