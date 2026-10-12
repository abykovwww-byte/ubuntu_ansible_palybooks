# Native metrics evidence and paired analysis

`roles/ngfw_lab/files/native_analysis` is a standard-library Python package. It
does not control workloads, profiles or live systems. The unit tests use
explicitly synthetic observations; their success is not an NGFW performance
result. Run from `roles/ngfw_lab/files` with `python -m native_analysis`.

## Source and catalog boundary

The [PT NGFW 1.11 guide](https://help.ptsecurity.com/ru-RU/projects/ngfw/1.11/qsguide/9442700043)
names Telegraf, InfluxDB and Grafana, with Linux.json, PT NGFW.json,
pt-ngfw-lite.json and cluster/hardware dashboards. At this implementation
stage the supplied [storage link](https://storage.ptsecurity.com/d/8128ed7c4b644ee4a569/)
reports that the link does not exist. Dashboard contents, the installed
Telegraf interval, actual plugins and actual measurement names are unavailable.
The guide's Grafana datasource minimum interval is not evidence of the
Telegraf collection interval. Do not invent a native utilization measurement.

When the official files are available, preserve originals and extract each:

```text
python -m native_analysis catalog "PT NGFW.json" --source-url https://storage.ptsecurity.com/d/8128ed7c4b644ee4a569/ --output native-catalog.json
```

Catalog rows include source hash, panel identifier, original target, variable
definitions, field configuration, transformations, units and explicit unknowns.
`--native-interval` may be supplied only from the observed Telegraf configuration.
The extractor preserves definitions; it does not execute arbitrary Grafana
expressions, resolve Grafana variables or attest live availability. Counter/gauge
type and missing units remain unknown until checked against the actual input
plugin or metric producer. Any SQL transcription must be reconciled against the
original target and source hash before its result is called a native metric.
OS CPU from `/proc/stat` remains distinct from useful dataplane utilization.

## Direct InfluxDB API receipts

```text
python -m native_analysis query --endpoint http://127.0.0.1:8086 --version 1 --database telegraf --query-file measurements.influxql --output receipts/discovery-unique-id
```

An initial query file can contain `SHOW MEASUREMENTS`; follow with
`SHOW FIELD KEYS FROM "<observed measurement>"` and `SHOW TAG KEYS FROM
"<observed measurement>"` only after its name is observed. Use time-bounded
SELECT queries for experiment windows. Capture uses nanosecond timestamps for
InfluxQL and saves exact raw JSON. API version 2 saves exact annotated CSV from
Flux and requires `--org` and `--credential-env` referring to a **read-only**
token. Version 2 support is an explicit adapter, not evidence that PT shipped
InfluxDB 2. Version 1 supports `--username` with password supplied through
`--credential-env`. Credentials must never be placed in query files or URLs.

Each unique destination includes a query, capture time, response hash and
content type in `receipt.json`. Existing destinations are never overwritten.
HTTP or query failures are not interpreted as empty measurements. A successful
HTTP response does not prove completeness. Choose bounded query windows to
avoid large in-memory API responses; this client is an evidence query tool,
not a streaming full database backup. Use the storage stack's backup/restore
workflow for full archives.

## Normalized manifest contract

The analyzer consumes `{ "schema_version": 1, "synthetic": false, "runs": [...] }`.
Each run has these fields:

| Field | Contract |
|---|---|
| `run_id`, `block_id`, `repeat`, `position` | Unique run; block of three windows; repeat identifier; position 0, 1 or 2 |
| `profile`, `workload`, `regime` | A/B; scenario; stable, overload or endurance |
| `start`, `end` | Actual measurement bounds with UTC offsets, excluding ramp and recovery |
| `offered_load` | Nonempty object, including every mixed-workload share and rate; equal within a block |
| `versions` | Nonempty invariant version object; EDR version belongs in profile evidence |
| `config_hashes` | Nonempty invariant object covering build, policy, security features, VM resources, topology, generator, receiver, observation and log forwarding; profile-specific audit hash belongs in profile evidence |
| `outcome`, `valid` | COMPLETED, STABLE_LIMIT_NOT_ESTABLISHED, NGFW_LIMIT, GENERATOR_LIMITED, HOST_LIMITED, INVALID_MEASUREMENT or SAFETY_STOP; separately assessed Boolean validity |
| `evidence` | Nonempty list of preserved raw receipt paths; references alone do not establish authenticity or completeness |
| `achieved` | `{ "value": 123, "unit": "cps" }`; actual successful work rate, independently derived from raw generator/receiver evidence |
| `completeness` | Expected and observed **unique expected sample slots** and missing slots, each integer; duplicate timestamps never inflate observed |
| `profile_evidence` | A: `edr_installed:false`, `audit_profile:"stock"`; B: `edr_installed:true`, `audit_profile:"full"`, `audit_rule_count:116`; attach actual package/rule/hash receipts |
| `latency_ms` | Optional raw successful-operation latency observations; no precomputed percentile values |
| `latency_histogram` | Alternative: `upper_bounds_ms` increasing, optional final `"+Inf"`, and equal-length noncumulative integer `counts` |
| `errors`, `loss` | Optional observed evidence carried through without substituting zeros |

```text
python -m native_analysis analyze campaign-normalized.json --output campaign-analysis.json
```

The workload planner is not a normalized evidence manifest. A plan's intended
offered load, duration, validity or profile does not establish that the actual
run met it. The integration step must join actual workload receipts, observation
receipts, invariant hashes and profile discovery before creating the above
contract. The implemented adapter below joins those receipts without promoting
plan flags to observed facts. Unknown completeness or profile evidence blocks paired
inference; failed windows remain in output with reasons.

## Workload receipt adapter

Run the controller's receipt index through the adapter, then the analyzer:

```text
python -m native_analysis.normalize --plan plan.json --receipts receipts-index.json --output normalized.json
python -m native_analysis analyze normalized.json --output campaign-analysis.json
```

`receipts-index.json` is `{"receipts":["run-directory/receipt.json", "..."]}`.
Paths are relative to the index directory; raw component paths are relative to
their receipt directory. Absolute archived paths are also supported. The output
is created exclusively and cannot overwrite an earlier normalized manifest.
Original plan/index and workload files are hashed. All referenced controller
receipt filenames remain in the normalized artifact, including unreadable ones.

The controller receipt supplies actual `run_id`, `profile`, `workload`, `regime`,
`offered_load`, `versions`, `config_hashes`, `profile_evidence`, `outcome`, `valid`,
`window_validated` and `evidence`; the adapter does not fill them from plan
readiness flags. `valid:true` and `window_validated:true` mean a controller has
independently checked the complete actual window, not merely run a process.
Each receipt has `components:[{kind,phase,path,exit_code,...}]` in planned component
order. Only explicit `phase:"measure"` components are analyzed; stabilization
and unknown-phase components remain separately referenced. A failed exit, missing output, truncated JSONL, duplicate final, identity
mismatch or unknown profile preserves the window and marks it invalid.

Native observation completeness comes from `native_completeness` with
`expected`, `observed`, `missing` and nonempty `evidence` references. Alternatively
`completeness` may explicitly carry `scope:"native_telemetry"` plus those fields.
The controller's ordinary health-monitor cadence stays in
`controller_completeness`; it never proves native point completeness. Current
controller receipts intentionally remain invalid until actual native full-window
read-back and regime classification exist. `unclassified` is not silently
converted to stable. A missing producer therefore blocks real acceptance.

For `extreme/load.py` JSONL, the adapter uses actual `measure_start`/`time_end`,
checks duration and counter conservation, and recomputes successful work per
second. `achieved_rate` in that generator currently means **attempted** rate and
is never used as successful throughput. Histogram counts must sum to successful
requests; the extra overflow bucket gets an explicit `+Inf` upper bound. Failed,
censored-at-boundary and pending operations remain separate. A held window
additionally requires all requested sessions active throughout, no stale final
echoes, and `held_receiver_window_validated:true` from independent receiver and
progress read-back. Its achieved rate is successful echoes/s, not session capacity.

For iperf JSON, only a receiver summary supplies successful bytes/s; sender
throughput is never substituted. Receiver `bytes*8/seconds` gives bit/s, and the
adapter checks any supplied rate for agreement. Exact controller
`measurement_window:{start,end,time_source}` is accepted only when duration
matches the receiver. Otherwise the adapter combines iperf's actual
`start.timestamp.timesecs` with receiver start/end offsets and labels its
one-second wall-clock resolution. Controller process start/end are not called
exact measurement bounds. UDP loss counters are preserved; version-dependent
packet counters are not silently converted to PPS. Receiver-only UDP schemas
use `sender:false`; absent receiver evidence makes the window invalid.

Mixed components become separate runs with stable component suffixes, keeping
the complete offered mixture as the comparison condition. Rates with different
units are never summed. Every planned window/component remains represented
even when the receipt is missing. Unplanned receipts are separately retained as
invalid. The analyzer exposes normalization failure reasons and actual workload
stop reasons, so an unsuccessful run is inspectable after report generation.

For each eligible ABA or BAB block the analyzer compares B with A, averaging
only same-profile achieved **rates** inside that block. It also reports the
relative discrepancy between the two return-control rates. Performance effect
is marked reproduced only with at least three distinct repeats, both orders,
and every eligible block exceeding both a 5% drop and its return-control scatter.
Host/generator-limited blocks do not support an isolated EDR effect conclusion.
Grouping includes workload, stable/overload/endurance regime, offered load,
units, versions and invariant hashes. No grouping mixes different conditions.
Minimum measurement length is 300 seconds or 3600 seconds for endurance.

The 5% criterion is a research convention, not a PT specification. The compared
effect is EDR **plus** the full AuditD profile. Raw latency uses empirical
nearest-rank quantiles. Histogram quantiles are reported as bucket bounds,
including unbounded overflow; no interpolation precision is invented and no
ready-made quantiles are averaged. Errors, losses, session capacity, useful
dataplane utilization, central EDR receipt and observer effect require separate
evidence and interpretation. This package does not certify those automatically.

## Local checks and remaining acceptance

```text
python -m unittest discover -s roles/ngfw_lab/tests -p test_native_analysis.py -v
```

On the restricted Windows host, set TEMP/TMP to a unique writable project
subdirectory before running tests that need temporary files. Tests cover parity,
profile identity, completeness, control scatter, mixed regimes/configurations,
duplicate run rejection, duration, raw/histogram quantiles, catalog preservation
and query failure receipts. CI, deployment, live native catalog, reproduced PT
dashboard formulas, real paired trials and endurance remain separate acceptance
items. Neither these tests nor a local query-client response establishes them.
