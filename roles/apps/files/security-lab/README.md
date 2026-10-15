# Security Lab control plane — integration prototype

## Shared operator clients

Codex and OpenCode can now share the same cases concurrently through one
process-owned local MCP service. See [SHARED-SERVICE.md](SHARED-SERVICE.md) for
installation, lifecycle, audit identity, revision conflicts and recovery. The
canonical `codex-skills/security-lab-operator` skill covers both clients. The
historical implementation/qualification notes below retain their original dates
and do not imply new runtime qualification from this client integration.

Continuation now uses `get_case_history`, `get_manifest` and append-only
`record_case_checkpoint` snapshots before choosing further checks. The native
handoff includes prior job history and the latest checkpoint. The managed
`start_service_check` entrypoint adds pinned curl GET/HEAD, single-port Nmap
version-light, typed public HTTP/API and TLS/PostgreSQL SSLRequest profiles. See
[case continuity and native service checks](SHARED-SERVICE.md#case-continuity-and-native-service-checks)
for contracts and qualification limits. `start_poc_action` executes one exact
saved HTTP action after separate action activation. Native PentestGPT now accepts
an explicit slice of up to 16 HTTP/TCP services from the existing scope; the
updated image and native TCP Compose service must be applied first. Use
`get_runtime_inventory` to distinguish implemented adapters from deployed runtime
capabilities. Response predicates do not independently verify vulnerability impact.

The operator uses the Codex or OpenCode chat throughout OSINT → surface
review → checks → separately requested PoC. On 2026-10-06 the operator explicitly
delegated authorization to tasks in that chat. No extra confirmation form,
elicitation callback, operator CLI or approval website is used.

## Task authorization

Codex saves an immutable scope, then calls `activate_scope_from_task` only for
an explicit checks command covering those targets. A find-information request
authorizes discovery alone. Discovered related assets are candidates until the
operator includes them in a task. A checks command does not authorize PoC;
`activate_actions_from_task` requires a separate instruction after checks review.
One instruction is enough for its stage: no second popup or per-tool consent.

The Controller checks ownership, manifest kind/status/hash, ROE, expiration and
stage. It records `approved_by=delegated:codex:<principal>` and the source
`codex_operator_delegation`, with `independent_human_receipt=false`. This is an
intentional delegation of authority to Codex, not independently authenticated
user-event or UI-click evidence. The backend trusts its configured delegate to
match the actual operator task. Workers must have no activation credentials.
Model-supplied `approved_by` is rejected by the manifest schema. Replay, stale
scope, altered manifests and expired actions are rejected by the Controller.

The old `request_scope_confirmation`/`request_action_confirmation` tools and
challenge handler are removed. Existing database challenge rows, where present,
are retired history with no consumer or authority. No new challenges table is
created; prior audit records are not relabelled as observed human decisions.

## Implemented locally

- strict versioned scope, authorization and action contracts;
- single-writer SQLite/WAL controller with BEGIN IMMEDIATE and ownership lock;
- immutable versions/hashes, audit, revocation generation and outbox;
- task-delegated scope/action activation through typed MCP tools;
- worker/profile-bound hashed capabilities with thirty-second leases;
- pause/resume preserving validity/budget and never automatically resuming PoC;
- explicitly synthetic offline fixtures and escaped portable HTML;
- real STDIO SDK roundtrips without an elicitation callback;
- bounded background public-source acquisition with per-source receipts,
  observations and relationship history, explicit errors/quotas/truncation;
- CT, Wayback CDX, public GitHub repository search, seven Google DoH record types,
  IANA/serving-registry RDAP, RIPEstat prefix/ASN and IPv4 InternetDB adapters;
- portable SVG surface overview and complete object/relationship tables linked
  to evidence, with no external scripts or CDN;
- an adapted LLM workflow and source-lock for the three requested repositories;
- direct curl GET/HEAD plans with standard TLS verification, explicit proxy bypass,
  production-closed tool catalog and a typed dispatcher;
- install ACK before start, sequence-bound lease renewal, cancellation-safe
  cleanup, bounded sanitized worker evidence and revocation delivery receipts;
- canonical-hashed PentestGPT context containing exact scope/ROE, surface,
  evidence index, coverage and candidate hypotheses without raw source bodies;
- schema-two job ledger with restart revocation and conservative duration charges.
- five-tool PentestGPT facade bound to the exact case/scope/hash/generation;
- private authenticated in-process HTTP MCP transport, without a new database
  writer, approval interface or public listener;
- complete host-owned DNS snapshots checked before install and every renewal;
- a checks adapter around the real pinned PentestGPT loop with strict worker
  receipt grounding and an explicitly synthetic laboratory backend.

**Public-source OSINT is available locally; target acquisition and checks remain
disabled.** `integration_status` reports version 0.3.0, discovery revision 1,
task authorization revision 1 and MCP lifecycle revision 2. `live_execution=false`
refers to target checks/PoC, not public-source lookup. Elicitation is not a gate.

## Checks dispatch and PentestGPT context

`get_pentest_context` reads one locked snapshot of the latest approved scope and
case. Discovered assets and source strings are untrusted data, never instructions
or expanded scope. Only explicitly available and qualified tools can appear
executable; the current production catalog marks every scanner unavailable.
This packet is context, not a capability or proof that PentestGPT has consumed it.

`execute_tool_job` accepts a strict tool-specific specification, not a shell
command. It currently refuses production jobs before issuing a capability or
charging a budget. Curl planning supports exactly one scoped direct GET/HEAD with
standard certificate verification and `--noproxy '*'`, without queries, redirects,
credentials, user flags, environment or custom headers. Nmap is preview-only:
the HTTP scope is insufficient TCP scan authorization. Other scanner policies,
images, versions, templates and wordlists still need implementation/qualification.
There is no interception certificate or HTTP interception process. Controller
and typed URL checks constrain admitted plans; they are not independent network
enforcement. A qualified direct worker and host-managed network isolation are
still required before production execution can be enabled.

`execute_fixture_check` exercises the same dispatcher on fixture.example.test
with an activated `lab:isolated-docker-fixture` scope. It uses a labelled
synthetic backend with zero network and no binary; it is not receiving-side
enforcement evidence. `get_job_status` returns durable metadata and evidence IDs,
never capabilities. The operator still controls cases exclusively through chat.

The dispatcher seals its policy hash and host-owned plan before installation.
The public MCP path currently executes these contracts with the zero-network fixture;
an installation ACK is not proof of a working production network policy.
Start requires an exact current ACK and repeats generation, scope integrity,
stage and lease checks. Renewal increments a sequence and requires installation
ACK before another renewal. Expired access cannot be revived. Stop revokes the
generation first, then closes transports/workers and acknowledges delivery only
when their closure receipts exist. Cancelling an MCP stop request cannot cancel
the dispatcher-owned cleanup. An uncertain closure remains unacknowledged.

Each prepared job is charged its full requested window once (up to 1800s), even
if installation fails or a worker stops early. This conservative first ledger
does not refund unused time. Case/job count and duration charges survive restart
and resume. Schema v1 jobs lacking the ledger are charged once during the
transactional v2 migration. Active jobs become revoked/interrupted and cases
pause on restart; no worker is automatically reissued. The controller remains
long-lived across multiple bounded worker episodes.

## Bound PentestGPT role adapter

The protected host injects one existing Controller and Dispatcher into
`BoundPentestTools`. Its five model tools are `get_context`, `list_tools`,
`execute_tool_job`, `get_job_status` and `get_evidence`. No case/scope/generation,
approval, capability, arbitrary shell, image or installer argument is accepted
from a role. Every operation validates the fixed binding; job preparation repeats
it atomically under the Controller transaction. A scope change during a host DNS
lookup is rejected before a job or budget charge. Worker evidence belongs to the
same scope/generation; model/context claims cannot substitute for it.

`build_transport` embeds that facade in the owning host's event loop. It mints
an ephemeral bearer for client configuration, never a prompt. Exact loopback URL,
Host, path, auth header, TTL and current binding are required. Browser origins,
auth discovery, redirects, query tokens and extra routes are rejected. The
factory opens no listener; shutdown closes only its transport, not the common
Controller. A real MCP SDK/ASGI roundtrip exercises this boundary without sockets.

`run_checks_trial` uses optional pinned PentestGPT/UnifiedAgent sources, a local
checks-only loop subclass and new Supervisor/Executor instructions. Both roles
use READ_ONLY and receive only injected laboratory backends; legacy FULL_ACCESS
role construction is not used. Test/verify/recover tasks are permitted, while
discovery/enumeration/exploit and foreign targets are denied. The upstream
planning SQLite is a labelled projection; Controller remains scope/job/evidence
authority. The runner segment remains bounded by 1800s, independently of the
long-lived case. No automatic PoC or model-runner resume is enabled.

Upstream trace recovery must pass the same current worker-evidence checks as a
normal result. Merely raising an upstream AgentContractError is insufficient:
the original loop may recover the rejected result from its trace. The adapted
loop validates recovered candidates before allowing the upstream memory commit.
Case resume remains available; model trace resume is closed until qualified.

The developer harness `lab/qualify_pentest_bridge.py` executes the actual upstream
Supervisor/Executor loop with scripted events through the real authenticated
HTTP MCP SDK. It checks the normal dispatch/evidence path and refusal of foreign
targets, exploitation, context-as-worker-evidence and native tools. It calls no
provider and uses a zero-network fixture; this is not proof of LLM decisions,
real curl traffic or a production launch. Optional source-fixture tests require
`SECURITY_LAB_PENTEST_SOURCE` and `SECURITY_LAB_UNIFIED_SOURCE`; the additional
UnifiedAgent import dependency is pinned in `lab/requirements-pentest-bridge.txt`.

`codex_tool_profile` builds a candidate restricted profile from actual host model
metadata for pinned Codex 0.160.0. Disabling shell alone does not disable patch;
both model metadata and configuration are restricted. Complete observed tool
inventory and negative probes are still required. The unmodified HTTP MCP path
adds three native resource tools and therefore fails the exact-five-tool gate.
The optional `codex_dynamic_backend` implements the replacement protocol:
`thread/start.dynamicTools` exposes exactly five function schemas, and the public
low-level SDK's `item/tool/call` callback forwards to the authenticated facade.
Codex receives no MCP registration; native command/file approval requests are
declined. Exact thread/turn/call IDs, context-first order, argument schemas and
call budgets are enforced. Native item events, replay and foreign bindings fail
closed. Delayed SDK start/initialize and cancellation are joined before cleanup
can finish. Hard process deadlines remain the isolated runner's responsibility.

This is an implemented candidate backend, still independently refused by the
real-provider gate in `run_checks_trial`. Actual full CLI tool inventory and
negative probes have not been measured. The SDK serialization/router tests and
fake notification tests call no model. Optional actual SDK tests use
`SECURITY_LAB_CODEX_SDK_SOURCE` for the inspected 0.1.0b3 package. A generated
configuration or a synthetic receipt never enables production execution.
Real-provider qualification also requires bounded/redacted trace capture,
explicit exclusion of inherited UnifiedAgent monitors, and decoded-body
grounding for multiline/quoted worker receipts. Current JSON escaping can cause
a false refusal to finish; it cannot grant additional target authority.

When a host TargetResolver is configured, complete DNS snapshots must match the
host-owned policy before installation and on renewal. Any added/removed/changed
address stops the job rather than expanding admitted targets. These contracts
do not prove that a real direct worker uses the checked addresses. The laboratory
candidate pins the selected address using curl `--resolve`; its Linux execution
has not yet been qualified. The resolver interface has no default live DNS
implementation; killable production resolver workers remain required.

## Public-source workflow

Read `get_discovery_profile` (or `security-lab://profiles/osint-domain`), create a
case, then call `start_public_discovery(case_id)`. It returns immediately; poll
`get_case`, read `get_report` and open the case HTML resource. The current local
pass caps 64 requests, five DNS host pivots (including seed), three public-IP
metadata pivots and 180 seconds. Each source operation has an eight-second DNS
and HTTP waiting deadline. Requests are sequential, normally one per second.
An exhausted budget is partial coverage with explicit omitted-source receipts.

The HTTPS client checks fixed provider endpoints and query forms, public-only
DNS answers, a pinned provider IP, verified TLS/SNI, response byte limits and
socket deadlines. Redirects are not followed. Serving RDAP is chosen only from
IANA HTTPS bootstrap. No seed-site HTTP, port scans or vulnerability checks are
performed. No adapter credentials are read or sent by this baseline.

Secret-labelled JSON values and known credential/URL formats are redacted before
storage. SHA256 identifies the sanitized saved body, not the original wire body.
Incomplete byte-limited bodies are discarded rather than retaining cut-off
credential fields. This redaction is not a complete Gitleaks/secret scanner.
Model report calls return body previews; `get_source_evidence` reads bounded
slices. The portable HTML contains the complete sanitized saved bodies.

`collect_osint_fixture` runs the same parsers and pivots with labelled synthetic
answers and zero network traffic. `lab/qualify_discovery.py` is developer
qualification, not an operator CLI. It saves a map, report preview and receipt
summary through real STDIO SDK calls; `--public --seed <domain>` explicitly
selects the public-source pass.

The profile adapts domain pivots, fact verification and modular collectors from
osint-agent-skills, Spotlight and OpenOSINT. Pins, consulted files, licenses and
reuse boundaries are in `profiles/source-lock.json`. No upstream runtime/code is
vendored. Independent LLM verification is prescribed by the profile, not proven
as an implemented autonomous verifier by these parser tests.

DNSDumpster free-key adapter, GitHub code/files/history, SearXNG,
seed-site acquisition and Gitleaks are still absent and shown as coverage gaps.
Repository search matches never prove company ownership; provider IP/ASN/prefix
and historical indexed services do not automatically become approved targets.

## MCP lifecycle and registration

A reproduced Windows startup failure occurred when inventory initialization
opened an already-owned Controller byte lock. Lifecycle revision 2 defers store
ownership until a case tool/resource is invoked. Initialization, tool/resource
inventory and status create no database and acquire no store lock. A competing
case session is denied while the owner is active, and may retry after it closes.
Production multi-client routing to one Controller still needs integration.

The registration helper sets only this probe's startup timeout (30 seconds) and
tool timeout (120 seconds), preserving other servers and permissions. There is
no five-minute UI wait in this workflow. After source changes, refresh the server
and verify its advertised tools/revision instead of assuming the host reloaded.

`server.py --data-dir <private-directory> --principal <configured-identity>` is
the host launch contract, not an operator command needed for an engagement.
The principal is private configuration; it does not verify OpenAI account identity.

## Existing PentestGPT and native tools (2026-10-07)

The implementation path now uses the managed PentestGPT image and upstream
`pentestgpt-run`, rather than extending the dedicated curl worker. The host-only
Compose adapter is opt-in: inventory and OSINT calls do not start a model or
container. SSH/Compose/runs paths are protected launch configuration, never
model-selected MCP fields or operator commands required during an engagement.

`start_nmap_scan(case_id, host, ports, seconds, ipv6)` launches stock nmap with
fixed TCP-connect arguments, literal host-resolved addresses and explicitly
approved `network_targets`. Private RFC1918/ULA addresses additionally need
exact address grants in that same manifest. HTTP scope grants no TCP scan.
No NSE, UDP, arbitrary flags or provider are involved. `get_job_status` and the
case report return real stdout/exit-code receipts. The new `pentest-tools` Compose
profile uses the managed image with direct TCP egress, no auth/data volumes,
unprivileged identity and a read-only filesystem. It launches fixed commands,
not a model agent. Its source configuration still requires IaC apply. Scope is
checked by the trusted launcher; direct egress is not an independent firewall.
The explicitly selected legacy service used below has an internal target network.

An actual STDIO SDK → MCP → SSH → existing Compose image → nmap test completed
on 2026-10-07. Its owned TCP 8080 fixture was reported open by nmap XML and
independently received the connection. The temporary fixture and tool container
were removed. No model provider or external target was contacted. The local
outputs contain `security-lab-native-tools-20261007/native-nmap.json` and cleanup.
This verifies the native tool path, not deployment of the new four-tool bundle.

`start_pentestgpt_run(case_id)` starts the real existing agent and model provider
for one approved HTTP target. Its fixed checks goal includes scope and a file
descriptor for the complete untrusted discovery packet. Native command/output/tool receipts come from
upstream episode traces, not only the final summary. Existing roles retain
FULL_ACCESS; phase control is prompt-only and independent HTTP restrictions
are not enforced. Reduced paths/exclusions and network scopes are therefore
refused by this adapter. PoC is never automatically activated. This provider
path completed a bounded owned-lab trial on 2026-10-13: actual model responses,
captured native curl GET/exit 0/HTTP 200, independent receiving-side confirmation,
and exact-owner cleanup. This does not qualify every scanner or general
FULL_ACCESS isolation.

The native adapter transfers the canonical `build_handoff` packet through SSH
stdin as engagement data, never as a source-code deployment or shell argument.
It retains every surface node, relation, attribution/freshness history,
observation, source receipt, evidence index, hypothesis, coverage record and gap.
The Controller's immutable snapshot remains authoritative. Original
`context_hash` identifies that snapshot; `content_sha256` identifies the exact
UTF-8 file after external-data credential redaction. These hashes have distinct
meanings.

Before reserving a job, preflight validates the scope binding and the complete
file's 1 MiB byte limit. No map is truncated. The host writes a checked owned
artifact at `/workspace/handoffs/<run-id>/osint-context.json`, mounted from the
managed workspace directory. The goal stays within upstream's 4000-character
limit and contains only authoritative scope plus path/hash/size instructions.
The model must verify the file hash and read/query bounded parts when tool output
truncates. Context and catalog metadata grant no scope expansion or PoC.

Creation refuses collisions, invalid bindings, traversal and linked paths.
Only the exact run's known files may be removed on stop/failure; foreign ownership
or a changed artifact is not silently deleted. Read-only delivery is not
immutability against a FULL_ACCESS worker. Final receipts distinguish native
process closure from artifact cleanup. Configure the host mount with
`--pentestgpt-workspace-directory` (default
`/srv/app-data/pentestgpt/workspace`), matching the managed Compose mount.

The file path completed a fresh STDIO/native lab trial on 2026-10-13. A synthetic
103-node/100-relation packet reached the worker as a 146,020-byte file, with
independent host SHA/size/count readback. Native command receipts verified that
SHA and read a late candidate with its evidence ID before a successful exact
endpoint GET. The target independently logged GET/200; final case evidence
confirmed process closure and handoff removal separately. The fixture is
generated lab data, not public OSINT or a vulnerability finding.

The PentestGPT Dockerfile now includes locked official Subfinder 2.16.0,
ProjectDiscovery httpx 1.12.0, Nuclei 3.11.1 and Katana 1.8.0 release binaries.
Applied-image checks on the owned fixture completed on 2026-10-13: httpx
returned HTTP 200; Katana followed a benign same-host link at depth one; Nuclei
validated local info-only GET templates with one positive and zero negative
matches; unprivileged Nmap TCP connect reported the sole authorized port 8080
open. HTTP checks have receiving-side request receipts. The Nmap result has
CLI/XML proof, without an independent TCP accept log. No exploit templates,
PoC, authentication attempts or public template downloads were used.
One `pentestgpt/tool-lock.json` owns versions and archive digests for amd64/arm64;
the installer verifies before extracting, retains licenses and writes installed
version receipts. Launchers disable update checks and reject explicit updates.
Nuclei templates are not installed. New sources are included in the IaC copy
list, but this does not imply an applied server image.

`start_subfinder_discovery(case_id)` runs the actual passive CLI on the case seed,
first reading its actual version. Results enter the existing Controller surface
and evidence store without granting test scope. Nonzero exit, truncated output,
malformed records and unknown provider coverage remain visible. The tools-only
service is required: the auth-mounted model service cannot run this collector.
Stops join the owned process; late results after case stop are refused.
The parser/runtime/MCP path completed a real passive collection on 2026-10-13.
Provider completeness and related-asset ownership remain unproven; recorded
partial/error receipts are collection gaps. Public HTTPS collection remains separate.

Windows OpenSSH silently exits 255 when launched in the MCP SDK's minimal
environment without ProgramData. The adapter restores only that nonsecret
system folder through the Windows known-folder API, keeping SSH host-key
verification and the existing safe environment boundary.

## Validation and remaining integration

Use Python 3.12 with the tracked `uv.lock` and existing environment:

```text
uv sync --frozen
uv run --frozen pytest
```

On this Windows host, use the project `.venv/Scripts/python.exe` and a unique
workspace pytest basetemp with approved file access when sandbox temp is blocked.
No OpenAI API key is required by these tests.

The previous interception implementation, its dedicated Linux harness/image and
network CI job have been removed at the operator's instruction. Saved historical
reports remain historical artifacts; their receipts do not qualify the current
direct execution architecture. They must not be used to enable production jobs.
The remaining STDIO dispatcher harness uses zero-network synthetic worker
results. Developer harness arguments are not an operator interface.

The following dedicated HTTP implementation is a frozen laboratory prototype,
not the implementation route for the pentest tool stack.
`DockerDirectBackend` is a lab-only candidate admitted by exact backend type,
owned internal network/image and mandatory resolver. It launches one strict curl
plan as UID1001 with no capabilities, an empty environment and read-only mounts.
A trusted root PID1 installs namespace-local nftables before connecting the
network. Only one timed destination address/port is allowed; every install/renew
requires strict numeric nft JSON read-back. A protected read-only lease and an
independent watchdog stop curl on expiry, heartbeat loss or a host stop marker.
Cleanup uncertainty pauses the case and prevents another direct job. Cancellation
joins installation and cleanup before releasing the dispatcher reservation.

`Dockerfile.direct-lab` and `lab/qualify_direct_worker.py` prepare an owned
dual-stack HTTPS qualification: allowed IPv4/IPv6, wrong-address/port/DNS denial,
standard target TLS, pinned address use, no redirect follow, renewed stream and
stop/heartbeat-loss closure. On 2026-10-07 the frozen v6 source/image passed
three consecutive owned Linux runs, ten checks per run. Receiving-side receipts
confirmed positive IPv4/IPv6 HTTPS, standard TLS failure on wrong trust, no
redirect follow, a 35-second stream with six acknowledged renewals, operator
stop closure (0.522–0.524 seconds) and heartbeat-loss closure (3.556–3.807 seconds).
Each run confirmed six worker removals and unpoisoned backend state. These
numbers describe this fixture and are not a production timing guarantee.
The final local suite passed 618 tests with no failures, errors or skips;
cached actual SDK/runtime fixtures did not invoke a model provider.

Qualification package SHA256:
`ff48db47a068a9664418d64fa483563df810f6bed8c00db1882aae0ec4eda722`.
Image ID:
`sha256:9a65db44882a6ef2b2683b4f203a732af2c71a93a28cac42c6b139317cff3dec`.
Final receipts are stored in the local project outputs directory under
`security-lab-direct-network-20261007/attempt-v6`, including both repeats and
cleanup receipts. Exact temporary image tags/directories were removed;
owned containers/networks remaining: zero. Production services and host firewall
were not changed. Earlier failed attempts remain historical evidence.

Wrong-address/port and DNS negative receiving probes cover TCP in the
IPv4-selected worker. Positive IPv6 was tested separately; this does not prove
the negative cases in an IPv6-selected worker or UDP DNS blocking by traffic.
The harness resolver is static, so production DNS is still unqualified.
The fixture certificate is the actual endpoint certificate, without interception.
Path/method constraints are validated before launch; they are not independent
HTTP filtering. Public MCP production dispatch remains disabled. Each additional
scanner needs its own receiving-side tests; local fake Docker/nft tests cannot
substitute for kernel and network observations.

The production launcher is still incomplete: a production-qualified direct image/runtime,
host-managed network isolation, private DNS and multi-client orchestration need
integration. Dispatcher lifecycle, conservative budget accounting, schema v2 migration
and a context packet exist locally; a production backend and retention/purge worker
remain absent. The opt-in native real-model path has the bounded lab qualification
described above, with prompt-only phase/effect restrictions. Native Nmap
network scope and the Compose CLI adapter are implemented as described above;
the tools-only service and its pinned binaries have applied-image runtime receipts.
The action contract exists, but checks execution must be implemented
before the action stage runs normally. Full A01–A36 acceptance is not claimed.

Native system DNS lookups may outlive a timed-out Future in one of two bounded
slots; their late results never connect. Python process shutdown may still wait
for them. Production acquisition needs killable worker processes with protected
source-service egress and credentials. This client is not acceptance of that
production isolation, the target-network launcher or all scanner A26 paths.

The existing PentestGPT runtime remains unchanged. Deployment uses repository
Ansible delivery; this prototype is not automatically enabled in server inventory.
