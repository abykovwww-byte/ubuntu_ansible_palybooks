# Extreme workload adapter

This directory is a workload/manifest adapter, not a second VM lifecycle runner.
The root operator owns snapshot/profile switching, the existing exclusive host
lock, isolation receipts, native collection, health guards, process watchdogs,
mixed start synchronization, recovery, and restoration. `plan.py` does not run
its output. `load.py` runs a bounded client or an explicitly started receiver.
Neither changes routes, EDR, AuditD, VM resources, or firewall policies.
There is no unlocked live campaign controller in this directory.

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
it accepts watchdog timeouts up to 3620 seconds. For held sessions, add
`ceil(sessions/open_rate)+10` buildup time to the measurement deadline.
**A 3600-second held endurance exceeds the legacy job watchdog including buildup:**
integration must extend that watchdog with an explicit bounded held deadline
or run the duration-bounded client through an equivalent controller watchdog.
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
