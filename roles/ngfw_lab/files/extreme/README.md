# Extreme workload adapter

This directory is a workload/manifest adapter, not a second VM lifecycle runner.
The root operator owns snapshot/profile switching, the existing exclusive host
lock, isolation receipts, native collection, health guards, process watchdogs,
mixed start synchronization, recovery, and restoration. `plan.py` does not run
its output. `load.py` runs a bounded client or an explicitly started receiver.
Neither changes routes, EDR, AuditD, VM resources, or firewall policies.
The single-window controller below uses the existing host lock; it does not
automate an entire profile-switching campaign.

## Execution contract

Deploy the directory beside `traffic/` using the repository Ansible delivery.
Run `python3 extreme/plan.py campaign.json --output plan.json --require-ready`.
The supplied `pending-readiness.json` is the reviewable all-workload intake:
initial project targets are distinguished from unknown capacity, hashes and
receipts are empty, and current profile facts are null. Generate its review:

```text
python3 extreme/plan.py extreme/pending-readiness.json --output pending-plan.json --require-ready
```

Expected exit2. HTTP, UDP capacity and mixed shares remain explicitly unselected;
the output retains them as `unselected_workloads`, never as zero-load windows.
Copy and fill a campaign configuration only from actual live evidence. The
historical target/source addresses are labelled unverified; no extra IP is invented.
Missing receipts write a reviewable plan and exit 2; an unready plan is not a
live acceptance. Receipts refer to actual archived evidence; flags alone do not
prove the content. The controller must validate content and freshness against
current topology before executing any argv.

The existing `traffic/job.py` can supervise the emitted `python3`/`iperf3` argv;
it accepts watchdog timeouts up to 4500 seconds. For held sessions, add
`ceil(sessions/open_rate)+10` buildup time and a bounded cleanup margin to the
measurement deadline. The client refuses a held buildup schedule above 600
seconds, leaving room for a 3600-second measurement inside the bounded watchdog.
Ansible installs the public tool directory with 0755 traversal for UID 65534;
Compose mounts it read-only at `/opt/extreme` on both endpoints.
Do not silently shorten endurance. Capture stdout JSONL and stderr from start,
including nonzero exits, cancellation, failed buildup and every five-second
progress record. Send each measurement phase to a unique run directory. A
`final` record is workload completion, not proof that a window is valid.

`load.py serve --bind <lab-server-ip> --max-sessions 60000 --with-iperf` replaces the legacy
receiver process for TCP9000/HTTP8080/DNS5353; never start both on these ports.
It uses the existing `held_receiver.Receiver` protocol and state file but raises
its explicit session bound. `--with-iperf` supervises iperf3 for TCP/UDP5201,
detects premature exit and terminates/kills it during signal-driven cleanup.
Omit that flag only when iperf3 is already supervised separately. A single
iperf3 server supports one active test, so mixed plans permit only one iperf
component plus DNS and new TCP/HTTP connections. The receiver uses a separate
thread for held echoes; measure receiver CPU and FD pressure at direct capacity.

Extreme Compose reserves the base service ports on both endpoints and each
configured policy-listener range additionally on the receiver. This prevents
namespace ephemeral allocation (including Docker's embedded DNS sockets) from
occupying a wildcard policy listener's port before receiver startup. The client
keeps its separate 64,508-port configured pool; reserving receiver policy ranges
on the client would reduce the headroom for 60,000 held sessions. Actual occupied
ports and resource margins still require read-back. Direct and NGFW modes use
the same per-endpoint reservations; changing these settings requires fresh
endpoint identity and control-capacity evidence.

Client example after resources and source addresses are provisioned:

```text
python3 extreme/load.py client --kind held --target <lab-server-ip> --source-ip <client-ip-1> --source-ip <client-ip-2> --sessions 60000 --rate 5000 --duration 300 --run-id <unique-id>
```

No endpoint address is guessed by this adapter. Runtime must verify source IP
ownership and route isolation. Linux FD soft/hard limits must cover sockets on
both endpoints plus reserve, conntrack/NAT capacity must be read back, and the
actual ephemeral port range times provisioned source addresses must exceed
required concurrent sessions with headroom. At 5000 CPS, repeated runs can
exhaust tuple space/TIME_WAIT even when concurrency is low; direct capacity
must cover the full five-minute or endurance window and the recovery cycle.
Container memory/cgroup/pids limits, host CPU allocation and receiver capacity
must be inspected. Source IPs are configuration, never automatically assigned.

## Workload selection

Project starting targets, not PT ratings:

| Workload | Initial upper load and escalation |
|---|---|
| TCP | 900 Mbps, then above if direct path proves more bandwidth |
| Small UDP | Highest direct-path confirmed PPS; payload64 bytes separately labelled |
| Large UDP | Confirmed path bandwidth; e.g. payload1200, never called frame size |
| Short TCP | 5000 CPS; higher only with a prepared direct-capacity receipt |
| Held | 20000 then 60000 live sockets, every socket echo-verified during measure |
| HTTP | Maximum reproducible direct-control RPS with successful complete body |
| DNS | 5000 QPS then highest confirmed generator capacity; fixed lab.invalid |
| Mixed | Fixed per-component bps, DNS QPS and TCP CPS/RPS, identical across A/B |

For IPv4 UDP, application offered PPS = bps/(8*payload_bytes). Ethernet frame
bytes = max(64, 14+20+8+payload_bytes+4) without VLAN; on-wire occupancy adds
8-byte preamble/SFD and 12-byte interframe gap. VLAN/tunnels/IPv6 need their own
overhead calculation. iperf bitrate is payload bitrate, split per parallel stream.

Begin with a short low-load admission only, then ramp/stabilize within60s at
the upper target. If degraded, preserve that window and bisect/coarsely reduce;
if stable, double within prepared bounds. `next_load` is only a numeric helper;
changing offered load invalidates the former capacity receipt. No15300 ceiling
is encoded. Stable and overload phases must remain separate in analysis.

For each workload the plan emits ABA, BAB, ABA: three independent paired blocks
plus return controls,300s measurement and at least120s recovery **and** recovery
to baseline. Held buildup is separate. A has EDR absent and stock administrative
audit; B has EDR installed with read-back of the116-rule full profile. Thus A/B
estimates the whole EDR+AuditD profile. Per-profile endurance requires a separate
stable-load evidence receipt and3600s measure. The plan deliberately leaves it
pending until that evidence exists.

## Evidence and limits

`load.py` returns JSONL with Unix epoch seconds `time_start`, `measure_start`,
`time_end`, actual attempt/success/error counters and rates. `requested_rate` is
offered requests/s except held where it is **buildup connections/s**, not heartbeat
rate. Held reports `active_sessions_at_measure_end`, `opened_sessions` and
`confirmed_measurement_sessions`, `minimum_active_measurement_sessions` and
`stale_echo_sessions` (age greater than twice heartbeat); reject a held window
unless all requested sessions stayed active and each echoed during measurement.
Join five-second progress records and receiver counters to verify sustained
heartbeat coverage; one final union of confirmed IDs alone is insufficient. Independently compare
receiver run counters keyed by the first8 bytes of SHA256(run_id), big-endian.
Held controller windows additionally retain `held-receiver.ndjson` and a hashed
`held-receiver-window.json` proof. The bounded endpoint read runs only for held,
at the existing observer cadence plus initial/terminal reads. It checks receiver
instance continuity, fresh published states, measurement bracketing, exact run
population, no disconnect during measurement, and minimum echo-round progression
across all connections. Receiver snapshots are sampled evidence, not packet
capture. The normalizer rechecks both raw source hashes and the proof's content;
`held_receiver_window_validated: true` alone is insufficient. Failed readbacks
remain in the raw timeline and invalidate the proof. These reads change the held
observer source/configuration identity and require new observer qualification
before a live held comparison; they do not add probes to TCP windows.
Each proof and raw source read is bounded to16MiB; oversized evidence is rejected.

An endurance `stable_receipt` is a `{path, sha256}` reference to a prior derived
receipt with validated stable outcome for the same profile, load and invariant
hashes. Before load, the controller verifies its hashed original controller
receipt, completion/recovery, native completeness evidence and hashed successful
workload bytes with at least300 actual seconds. It also verifies actual offered
iperf/request settings and held receiver proof where applicable. This proves a
previous window against its hashed, predeclared `stable_criterion` (exact offered
load, duration and bounded capacity thresholds). The derived receipt must retain
ordered `stable_workload_evidence` entries with `component_path`, `kind` and a
hashed `reference` to the preserved raw bytes. Component paths identify the
original invocation; they are not resolved relative to relocated controller files.
Receiver throughput, error ratio and UDP loss are recomputed using the shared
direct-capacity criteria; native export must have status
`COMPLETE_WITHIN_DECLARED_CATALOG_AND_CADENCE`. Mixed windows additionally need
the full common measurement interval after timestamp uncertainty. Older receipts
without the explicit mapping and criterion cannot authorize endurance. This proves a
previous stable load, not maximum capacity; a separate matching3600s direct
capacity receipt remains required. Transport lifetime and full-hour native export
must also be provisioned by the caller; the controller does not extend them.

Mixed measurement explicitly distinguishes nominal `measure_seconds`300/3600
from `component_measure_seconds`302/3602. Every component executes the padded
duration with the same offered settings and run ID. The2s allowance covers the
existing1s launch bound plus iperf timestamp resolution; it does not prove
simultaneity. Controller, direct capacity and normalization require every actual
component to cover its full declared duration and the conservative common
interval to cover the nominal duration. They retain the complete component union
in `mixed_measurement_window`; native export must cover that union, including
the padding. Stabilization and standalone commands retain their prior durations.
The mixed stable criterion must bind both durations before the window starts.

Recovery examines every appended sample, including samples produced by `pause()`.
After the mandatory minimum wait, an intervening failed baseline check resets
the consecutive-success count. A failed held readback is retained but cannot
replace an already raised safety-stop exception.
Successful latency histogram has noncumulative `counts`, upper `bounds_ms`, and
one extra unbounded overflow bucket. Do not average precalculated quantiles.
Failed attempts are outside the successful latency histogram and counted separately.
Operations cut short by the measurement boundary are `censored_at_boundary`,
not synthetic network errors; `pending_requests` exposes other unresolved attempts.

Short requests use fixed worker slots and skip missed slots instead of catch-up
bursts. `offered_rate_shortfall` is observational; generator limitation requires
direct-control/resource attribution. Failed/late capacity cannot prove NGFW saturation. For held, socket count
alone is not a success; failed buildup prevents measurement. Missing progress
after process/host failure is an explicit collection gap, never zero. Progress
stdout survives only if the controller durably stores it outside the endpoint.

Stop on loss of management, restart/OOM, critical disk, mandatory audit loss,
or agreed physical safety conditions; stop load first, preserve evidence, recover.
OS CPU100% alone is diagnostic. `classify()` requires complete equal-invariant
evidence and affirmative attribution before returning NGFW_LIMIT. Generator/host
limitation and invalid measurement stay separate. Actual policy hits require
native counters/read-back; ACL/NAT inventory count alone is not coverage.

No live capacity or safety claim is established by unit tests of this adapter.

## One-window guarded controller

`controller.py` is the executable integration with the existing `runner.Backend`,
`Runner.monitor/pause`, basic AuditD guard and extended measurement guard. It
holds the same `reports_dir/.runner.lock` with nonblocking `fcntl`, rejects reused
run directories, recomputes argv from the selected offered load and target, and
requires `cpu_action: warn`. It uses the configured observer sample cadence and
does not accelerate it. This still does not establish a tested live harness:
guest access and the actual receipt-producing adapters remain prerequisites.

```text
python3 extreme/controller.py --campaign campaign.json --run-id <selected-run-id> --config runner.json --measurement-config measurement.json --measurement-argv-file /etc/ngfw-lab/measurement-argv.json --identity-argv-file /etc/ngfw-lab/identity-argv.json --native-health /srv/app-data/ngfw-lab/native-health.json --isolation /srv/app-data/ngfw-lab/reports/isolation.json
```

The identity command must be a read-only collector configured by the operator;
it is executed before and after the window, not supplied by a remote receipt.
Measurement argv must terminate in `python3 -` for the existing bounded probe.
No credentials belong in these evidence documents or the Git repository.

The controller executes **one** selected comparison or endurance window. It
never switches EDR, restores snapshots, changes policies or provisions routes.
Mixed components start together with recorded process timestamps; launch skew
greater than1s invalidates the window. These are process timestamps, not exact
iperf measurement boundaries. Each component is supervised by `traffic/job.py`
with duration plus held buildup and cleanup, bounded by4500s. Error, guard or
signal stops all jobs belonging to this window. Raw stdout/stderr are opened
before launch and flushed/fsynced on success or failure. Failed windows remain.

Recovery reuses monitored pauses for at least120s, then requires two consecutive
healthy observations: management/VM/dataplane available, no missing metrics,
AuditD lost0, guest/host memory within5 percentage points of baseline, backlog
within10 entries and service probe latency below max(20ms, twice baseline).
CPU saturation alone is not a recovery failure. Other existing guard conditions
including mandatory audit, disk, temperature and sustained service latency
remain active. Default normalization deadline is600s; a failed/deadline recovery
is an explicit SAFETY_STOP and never starts another workload.

### Hashed prerequisite contract

For controller execution, each `campaign.receipts` entry and the selected
`control_capacity.receipt` must be `{ "path": "...", "sha256": "..." }`, not a
string or boolean. Paths are relative to the campaign file or absolute. Every
JSON prerequisite requires `kind` matching its entry name, `scope` equal to
`isolated-ngfw-lab`, `campaign_id`, `target`, `config_hashes` exactly matching the
campaign invariants, timezone-aware `observed_at` no older than24h and an
`evidence` list of hash-checked raw-file references. Missing producer data blocks
execution. These schemas describe required evidence; writing matching labels
does not replace actual collection.

- `snapshot_A` and `snapshot_B` additionally name `snapshot_name`, `vm` and
  `creation_state: "shut off"`. Current read-only `virsh snapshot-info` must
  confirm that exact VM, snapshot and shutoff state. Its output is saved.
- `rollback` includes a nonempty `steps` array and hashed
  `restore_verification_evidence`. The procedure remains operator-controlled.
- `observer_impact.observer_configuration_sha256` must match the observation
  invariant. Its raw comparison evidence must cover the chosen observer.
- `resource_reservations.headroom` contains positive `fd_slots`,
  `source_port_tuples`, `receiver_sessions`, `host_memory_bytes`, with the
  underlying resource read-back in `evidence`.
- The actual `control_capacity` JSON requires `status: "VALID"`, `load_hash`,
  `topology_hash` matching the DUT campaign, `offered_achieved: true`,
  `resources_unlimited: true`, `duration_seconds` covering the selected window,
  `path_kind: "direct_control"`, `traverses_ngfw: false`, and
  `endpoint_config_hashes` equal to the generator/receiver invariants. Its
  distinct `control_topology_hash` must match canonical JSON content from
  `control_topology_evidence`; a copy of the DUT topology cannot qualify.

### Current identity command output

The identity argv returns JSON with `collected_at` (age<=60s), `profile`,
`guest_boot_id`, `versions`, `config_hashes`, `profile_evidence` and `artifacts`.
The `artifacts` map contains hashed-file references for every plan invariant and
`profile`, relative to the identity argv file. All non-topology invariant hashes
are SHA256 of exact artifact bytes. The topology artifact is parsed JSON equal
to `Backend.topology()`, and its invariant uses `plan.fingerprint(topology)`.
The profile artifact's JSON equals `profile_evidence`.

`profile_evidence` requires `edr_installed`, `audit_profile` (`stock` or `full`),
`rules_sha256` and `auditd_conf_sha256`; B additionally requires
`audit_rule_count:116`. A requires package absence, not an inactive service.
The actual extended guest inventory must independently match the effective
rules/config hashes and boot identity. The controller copies identity evidence
and inventories into the run directory and repeats current identity checks after
load/recovery. No current provider for the additional package/policy/build
identity adapter is claimed by this implementation.

### Native-health producer output

An independent local-DB reader must refresh `--native-health` atomically. The
receipt includes `collected_at`, `source_interval_seconds` (actual PT interval),
`database`, `guest_boot_id`, `required_measurements` from the observed catalog,
and `query_evidence` as a hashed-file reference relative to that receipt.
That query JSON must contain the same `database`, `guest_boot_id`,
`partial:false` and `points:[{"measurement":"...","time":"RFC3339"}]`.
The producer implementation is `roles/ngfw_metrics/files/native_health.py`.
Its hashed query and clock evidence must support every selected catalog field.
Each required point's clock-adjusted upper age must be no greater than the
smaller of 30 seconds and twice the source interval plus 10 seconds; receipt
freshness is checked separately. Deployment and live verification belong in
the evidence reports. Freshness alone never proves full-window completeness.

The controller reads each atomic receipt once and hashes those exact bytes.
Only an otherwise fully validated HEALTHY generation whose source ages have
expired since its own evaluation may wait for a replacement: at most one second,
polling every 50 milliseconds. The replacement must advance query chronology,
retain boot, database, selected catalog and source identities, and pass the
unchanged age limit at actual acceptance. Missing points, future timestamps,
BLOCKED receipts, corrupt evidence or invalid replacements stop immediately.
No producer query or cadence change is made by this consumer wait. Synchronous
filesystem stalls cannot be preempted; a late result is rejected on return.

`native-health-refresh.ndjson` preserves exact receipt bytes and hashes with
PENDING, provisional CANDIDATE_VALIDATED and failure events. Only the accepted
entry in `native-health.ndjson` carries `refresh_wait.status:RESOLVED`, after
evidence recording and final age/deadline checks. Unchanged stale data or a
failed replacement remains a failure; failed receipt bytes are retained
separately. Pure offline `validate_native` never waits or replaces a snapshot.

### Terminal output and analysis

Each directory preserves `window.json`, `receipt.json`, inventories, topology,
snapshot and identity evidence, monitor/native JSONL, every component raw/stderr,
`receipts-index.json` and SHA256 `manifest.json`. Terminal outcomes are
COMPLETED, INVALID_MEASUREMENT or SAFETY_STOP; resource/NGFW limit attribution is
left to evidence analysis. `valid:false`, `window_validated:false` and
`regime:"unclassified"` remain until actual full-window native completeness and
workload evidence are reconciled. Controller sample counts explicitly use
`scope:"controller_monitor_not_native"`; they cannot stand in for native data.

The index has `{"receipts":["receipt.json"]}` relative to its directory and is
accepted by `python3 -m native_analysis.normalize --plan plan.json --receipts
receipts-index.json --output normalized.json`. Normalization does not invent
validity for incomplete receipts or turn process times into measurement times.
