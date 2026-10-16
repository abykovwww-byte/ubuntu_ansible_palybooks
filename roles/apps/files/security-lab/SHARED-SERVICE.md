# Shared Security Lab MCP: Codex and OpenCode

## Ownership and transport

`shared_server.py` runs one process-owned `ServerState`: Controller, SQLite
writer, discovery tasks, native run service and dispatchers. MCP request/session
lifetimes borrow that state and never close it. One authenticated loopback HTTP
endpoint serves both operator clients; it is not a new public API or Web UI.

Each client starts `client_proxy.py --settings <private-service.json> --client
codex|opencode` as its local STDIO MCP server. This bridge forwards JSON-RPC,
preserving request IDs, resources and structured results. It starts the daemon
on demand under a startup lock, then reconnects to the same daemon on subsequent
agent starts. No scheduler, administrator service installation or open terminal
is required. STDIO EOF does not stop engagements. A failed tool call is never
automatically replayed.

On Windows the user-owned daemon is launched through `Win32_Process.Create`
rather than as a bridge child: MCP SDK hosts terminate bridge descendant trees
on disconnect, even for detached children. The real-process test verifies the
daemon survives both clients closing.

The HTTP listener binds only `127.0.0.1`. Separate private client credentials
select the request-local client identity, never an MCP argument. Exact Host,
route and absence of browser Origin/query credentials are checked. The daemon
does not trust forwarded headers or environment proxies. Credentials live in a
user-private directory outside Git and are not included in either agent config.

The existing operator principal and data directory remain authoritative. No
SQLite schema migration is required. Historical approved_by/source fields remain
unchanged; new approvals use `delegated:codex:<principal>` or
`delegated:opencode:<principal>`. New audit details include the client identity.
Agent delegation is still not independently authenticated human-click evidence.

## Install and switch

Use the existing `security-lab/.venv/Scripts/python.exe` on Windows. From this
directory, `configure_shared.py` performs a dry-run by default. `--apply` changes
only the Security Lab registration in the current Codex/OpenCode configs,
preserves legacy store/principal/SSH options, installs the operator skill for
Codex and registers canonical skill paths for OpenCode. It refuses the switch
while durable jobs or discovery runs are active. Private host settings default
to `%USERPROFILE%/.local/share/security-lab/service.json`.

The global OpenCode JSONC file currently uses plain JSON. The installer requires
that parseable format and refuses an unsupported/commented format rather than
silently stripping comments. Both config updates are parsed and checked for
unrelated structural changes before writing.

Before starting the shared daemon, refresh/disconnect the old Codex STDIO server
so that it releases its Controller lock. Existing idle legacy processes may be
closed during the one-time switch only after checking there is no active work.
On Windows, `configure_shared.py --apply --retire-idle-legacy` performs that
explicit one-time cleanup for the exact registered script and case directory.
Do not kill an active worker or delete the lock file to bypass ownership.
Restart OpenCode and refresh/restart Codex so their new MCP configuration loads.

## Lifecycle

Host maintenance commands use the same bridge, adding `--service start`,
`--service status`, or `--service stop`. These commands are not required for an
engagement and are not MCP tools. `stop` is graceful and refuses while any
durable work is active; use the case stop tool first. The private
`service.service.log` contains daemon startup diagnostics, with HTTP access
logging disabled. Never print its whole contents without checking for sensitive
diagnostics. The service starts again on the next bridge connection after a
host reboot or an explicit shutdown.

A process restart retains existing conservative recovery: interrupted jobs are
revoked/paused and discovery is marked interrupted. No scanner, provider or PoC
automatically resumes. Do not equate HTTP readiness with server runtime or
provider qualification; use `integration_status` and actual job receipts.

## Concurrent operations

`list_cases(offset, limit)` discovers the common operator history. `get_case`
includes an audit-cursor `revision`. Shared-mode proposals, native checks
launches and resume require that value as `expected_revision`. Proposal checks
and writes are serialized; exact pending-manifest IDs protect activation from
replay or supersession. Existing generation, scope-hash, lease and service
admission checks remain authoritative during asynchronous native preparation.
Emergency case stop intentionally requires no revision.

Parallel client connections do not increase native runtime concurrency. The
same job pool rejects a duplicate native launch. A disconnect only closes a
bridge/session; an explicit case stop from either agent revokes the case and
joins its owned workers. A stale command returns a conflict and requires a new
read/reconciliation, never an automatic mutation retry.

## Explicit time-budget renewal

`propose_budget_extension(case_id, additional_seconds, authorization, expires_at,
expected_revision)` handles an operator's explicit current-chat extension task.
It copies the latest approved scope into a new **pending** immutable scope,
increasing only its cumulative time limit and refreshing checks authorization
and expiry. Exact services, address grants, paths, methods, job count, RPS and
concurrency are retained. Even an expired scope can be renewed; active work,
pending scope replacements and stale revisions are refused.

Read the previous authorization with `get_manifest`. Supply its same owner and
hosts, checks-only actions, and a fresh task reference/basis/window. The
`budget_extension_proposed` audit event links old/new manifest IDs and hashes,
old limit, exact delta, new limit and unchanged spent. This is a proposal, not
execution authority: activate its exact returned ID with
`activate_scope_from_task`, then reread the case before launching. Existing
reservations stay fully charged; evidence and previous manifests are unchanged.
No job, provider call or PoC starts automatically. Ordinary `propose_scope`
cannot inflate time beyond the initial allowance or a previously activated
extension. The running status advertises `case_budget_extension_revision`.

## Server-resident deployment (workstation optional)

The same shared service runs on the operator workstation or on the PentestGPT
host (`abykovserv`). The `apps` role deploys an opt-in systemd service,
`security-lab`, that owns the Controller, the hypothesis ledger and the research
campaign loop on the server. The server service uses the local Compose runtime
(`pentestgpt_mode: local`), so native launches need no SSH hop. An active
campaign therefore continues while the Windows workstation is off or OpenCode is
closed.

- Enable with `security_lab_enabled: true` and distinct
  `security_lab_codex_token` / `security_lab_opencode_token` (>=40 chars) in
  server-private overrides only. The service binds loopback `security_lab_port`.
- Files: source `security_lab_project_dir` (e.g. `/srv/apps/security-lab`), venv
  next to it, private `security_lab_config_dir` (`/etc/security-lab`,
  `service.json` + `credentials.json`, `0640`), data `security_lab_data_dir`
  (`/srv/app-data/security-lab`). The unit runs as the operator user with
  `SupplementaryGroups=docker` and `Restart=always`.
- The workstation clients reach it through an SSH loopback forward instead of a
  local daemon: add `remote_ssh` (e.g. `abykov@192.168.1.88`) and
  `remote_ssh_identity` to the workstation `service.json`. `client_proxy.py`
  then opens `-L 127.0.0.1:<port>:127.0.0.1:<port>` and never starts a local
  service. The client tokens in the workstation `credentials.json` must equal
  the server's two tokens.
- One store is authoritative. Do not run the workstation service against the
  server data directory, and do not point two hosts at one data directory. To
  move an existing engagement, copy `control.db` (and its WAL) into the server
  data directory while both services are stopped; case IDs, evidence and
  checkpoints are preserved.
- Closing a client only closes its tunnel; the server continues. Server restart
  still conservatively pauses active campaigns and never auto-resumes them.

## Research campaign and operator verification lanes

`start_research_campaign(case_id, config, expected_revision)` runs a wide
autonomous ChatGPT (existing Codex) campaign inside one approved scope. It uses
the same PentestGPT Supervisor/Executor adapter and the same native launch path;
there is no second provider for the operator's own model. Configure
`config.targets` (<=256 exact scope services), `objective`, `max_seconds`,
`max_jobs` and `segment_seconds`. The host partitions the selection into
`<=16`-service jobs and runs them **sequentially**. The campaign stops at its own
`max_seconds`/`max_jobs`, when the queue drains, or on the first failed segment;
it never renews itself and never widens scope or budget.

Because the goal already sits near the upstream 4000-character cap, the campaign
brief is delivered in the context file, not the goal: `research_task` (the
instruction plus `SECURITY_LAB_RESEARCH_RESULT` marker), `policy.research_rule`,
and `untrusted_osint_data.research_journal` with prior hypotheses. The model
prints one `SECURITY_LAB_RESEARCH_RESULT {json}` line; the host parses only that
bounded marker from the run receipts, before the prose receipt is truncated. Each
imported hypothesis keeps the exact job evidence ID and is stored as an
**untrusted model proposal**, never a verified finding.

The operator (this OpenCode/DeepSeek chat) reads the ledger with
`get_research_ledger` and works a chosen hypothesis with ordinary checks. While a
campaign is active the case gains a second execution lane:

- `research` lane: the campaign's native agent job, owning its selected hosts.
- `operator` lane: typed checks and operator agent runs, admitted only outside the
  research hosts.
- `exclusive` lane: all legacy work; still globally single.

Lane admission requires the approved scope `budget.concurrency >= 2`. Two jobs
never overlap the same host, even across lanes or cases, and an exclusive job
cannot start while any lane is active. Stop the campaign with
`stop_research_campaign` (or `stop_case`); it records `paused`, cancels only its
own owned worker and never resumes automatically. `get_case` reports
`research_campaign_active`; `integration_status` advertises
`research_campaign_revision`.

`claim_hypothesis(case_id, hypothesis_id, expected_version)` leases one
hypothesis to the operator lane and returns a private token (never print or
store it). `review_hypothesis(...)` records the operator's verdict; `verified` or
`refuted` require at least one real evidence ID from the same case. The verdict
is the operator's analyst claim, not server verification, and grants no PoC.

## Case continuity and native service checks

Before continuing an engagement, read `get_case`, `get_case_history` (follow
`next_after_sequence`), the relevant report receipts and exact `get_manifest`
bodies. Legacy direct-run reports may also live in the workstation's `outputs/`
and `work/`; a missing checkpoint is not proof that testing did not happen.

`record_case_checkpoint` appends an analyst snapshot to the existing audit log,
requiring the current `expected_revision`. It preserves finding IDs, uncertainty,
evidence/artifact references, next steps and reasons for retests. Evidence IDs
must belong to the same case. Checkpoints do not alter immutable evidence,
manifests, stage or authorization. There is no schema migration. The native
PentestGPT handoff now includes `case_continuation` with the latest checkpoint
and projected job history; checkpoint text remains untrusted analyst context.

`start_service_check` extends the managed `pentest-tools` toolbox with fixed
single-service profiles selected by `specification.tool`:

- `curl`: exact approved GET/HEAD URL, standard TLS verification, host DNS pin,
  no redirects, query, credentials, custom headers or payloads;
- `http`: pinned GET/HEAD/OPTIONS with separately encoded query parameters and
  public Accept/Content-Type/Origin/X-Requested-With/Accept-Language headers;
  custom Host, credentials and redirects are unsupported;
- `nmap_service`: one explicit network-scoped TCP port, version-light, no
  caller-selected NSE; Nmap's built-in version-category scripts may run;
  a wall deadline bounds the operation, not a separate version-probe RPS limit;
- `tls`: one explicit network-scoped TLS handshake with hostname/chain
  verification, optionally `starttls=postgres` for PostgreSQL SSLRequest.

Each launch requires the current case revision and consumes existing case
budgets. DNS is consulted only after target authorization, one selected address
is pinned, and special/private addresses require the existing address-grant
policy. These jobs share native admission, leases, cancellation and evidence
closure with Nmap. They do not invoke a model. POST/body execution belongs to
the separately activated action worker described below.
The runtime recompiles the typed plan before sending argv to the trusted host
program; model-supplied flags, executable, image, environment and mounts are
unsupported. The tools image explicitly installs OpenSSL for the TLS profile.

`get_tool_catalog` distinguishes `compose:*` profiles from unqualified typed
worker planners. Configuration/availability does not claim live qualification;
retain real CLI receipts after applying the tools image. Refresh the shared
daemon and restart clients after source/skill updates so schemas are rediscovered.

## Executable saved actions

`Action.request` optionally holds an exact typed HTTP request. `Action.verification`
optionally holds an expected status/body marker. Absent fields are omitted when
serializing, preserving historical action hashes. Descriptive legacy actions
remain readable but non-executable. Every saved request must match the current
HTTP service, path and method permissions as well as the action host/time bounds.

The operator's separate PoC task activates an exact action manifest. Thereafter
`start_poc_action(case_id, manifest_id, finding_id, expected_revision)` loads that
saved request; it accepts no replacement payload. The Controller seals action
ID/hash/finding alongside current scope/hash/generation, checks authorization and
expiry throughout the job, and charges the same case budget. State transitions
are `actions_approved -> poc_running -> checks_review`. Other unattempted actions
in the same still-valid activated plan can be selected individually from review.
An attempted action is not replayed; stop/restart/resume never resumes PoC.

Execution uses the clean tools container, not the full-access model. GET/HEAD/
OPTIONS and explicitly saved POST bodies are supported. POST cannot run through
the checks entrypoint. Actual CLI output, response predicate, finding/manifest
binding and closure are evidence. An expected response match is not an
independent vulnerability/impact verdict; truncated or failed output remains
inconclusive. No database migration or historical evidence rewrite is needed.

The SSH host adapter captures tool pipes as bytes. For a non-UTF-8 HTTP body it
retains the response headers/status and a bounded binary summary (byte count,
SHA-256 and 16-byte hex prefix), rather than decoding a DOCX/ZIP as text. Status
predicates can match that response; body-marker predicates remain inconclusive
when the body is summarized. These are response observations, not validation of
the returned document or vulnerability impact. Refresh the idle shared daemon
after updating this adapter; its trusted host program is sent over SSH per job,
so this fix does not require rebuilding the tools image or an Ansible apply.

## Multi-target and TCP native agent

### Native execution handoff

The native runner receives a schema-2 packet with a separate `native_execution`
record, not the non-executable `get_pentest_context` typed-facade packet. The
Controller creates that record only for an actual prepared checks reservation
and seals it against current job/scope/hash/generation, selection and accounting
before provider launch. Capability tokens never enter model context. File size
and goal limits are preflighted with an explicitly non-executable preview;
the receiving runtime refuses previews, facade packets, mismatched bindings and
expired reservations.

The record provides the absolute job deadline and exact case counters after
charging the reservation. `unallocated` case allowance is the budget for future
Controller jobs, not a reason to deny the currently reserved run. Preparation
consumes the same reservation; the container timeout is reduced to the remaining
absolute window. Historical jobs/checkpoints remain context and are not used to
guess current allowance. Native toolbox admission does not mark typed workers
qualified or claim independent FULL_ACCESS network enforcement.

The fixed native goal uses this admission contract for single-HTTP, TCP and mixed
runs alike. A deterministic stdlib verifier is staged read-only beside the JSON,
with its hash pinned in the transport descriptor. It shares the caller's canonical
binding/selection implementation; scope payloads are not required to repeat case
identity or self-referential context hashes. The descriptor includes the native
scope version and exact execution selection explicitly.

The host runs that verifier before starting any provider. A failed/expired
preflight prevents provider launch and uses owned cleanup. A compact verified
`ready_for_scoped_checks` receipt is appended to the goal and returned by
`start_pentestgpt_run`, and retained in final evidence separately from model
observations. The LLM starts with the smallest justified target check, not a
new gate/recovery task. An exact code-hash-pinned command is supplied for genuine
local integrity rechecks; the LLM must not invent a validator or wait for an
unimplemented Controller correction-file response channel. The code artifact
is verified and cleaned under the same ownership/no-follow rules as the JSON.

This distinguishes the native shell from typed-dispatcher qualification.
The typed facade remains non-executable until its own qualification requirements
are met. `native_execution_handoff_revision` in integration status identifies the
updated host-side adapter; no image rebuild is needed for this context-only fix.
`native_preflight_revision` identifies the deterministic verifier. Readiness is
local admission/file evidence, not proof of target coverage or vulnerability.

`start_pentestgpt_run(..., targets=[...])` selects 1..16 exact HTTP/TCP services
from the existing immutable scope. The upstream limit is per run, not per case.
Larger cases select a focused slice without reauthorizing a different scope;
the complete original scope and history stay in the hash-bound file handoff.
The `execution_targets` array is the narrower run selection. Reduced HTTP path/
exclusion scopes remain unsupported by the full-access agent and use typed HTTP.

The managed launcher now forwards repeated `--target` arguments and accepts
exact `tcp://hostname:port` targets; TCP does not inherit domain wildcard grants.
`--capabilities` reports revision 2 without provider/auth access. Before any
multi-target/TCP provider launch, the host verifies that deployed capability,
preventing old single-target launchers silently keeping only the last argument.

The IaC `pentestgpt-native` service extends the existing agent with direct egress
and empty HTTP/HTTPS/ALL proxy variables for TCP tasks. It retains the existing
provider configuration, workspace, runs, FULL_ACCESS and container resource
bounds. Network/phase restrictions are prompt-only, not a receiving-side firewall.
HTTP-only runs keep the existing proxy service. Missing image/service updates
return `native_image_update_required` or `native_service_not_applied` before
provider launch, rather than an ambiguous network failure.

`get_runtime_inventory` performs only clean-container version/capability checks;
it reports actual curl/Nmap/OpenSSL availability, launcher revision and native
Compose service presence, with zero target requests/provider calls. It does not
qualify arbitrary target actions. `lab/qualify_stack_runtime.py` verifies this
through the installed STDIO bridge while checking existing case revisions remain
unchanged. Apply image/Compose changes through the repository IaC workflow.

## Recovery and rollback

1. Read current case/job states and stop active work explicitly if needed.
2. Stop the shared service gracefully with the host command.
3. Restore the saved nonsecret legacy Security Lab registration from
   `legacy-registration.json` to the one managed Codex section; remove only the
   OpenCode Security Lab registration if returning to Codex-only operation.
4. Restart clients. Continue using the same principal and case directory.

Do not copy a live database, rewrite historical audit, delete evidence or remove
credentials as part of a normal rollback. Source fixes remain in the IaC repo;
permanent server changes follow its normal deployment workflow.

## Verification

`tests/test_shared_service.py` exercises two independent real MCP SDK clients
against the common ASGI app: shared history, audit attribution, revision races,
disconnect survival, duplicate launch rejection and cross-client native stop.
`tests/test_shared_proxy.py` runs actual STDIO subprocesses and a loopback daemon,
including simultaneous automatic startup, durable cases after both bridges exit
and graceful daemon shutdown. These tests use synthetic runtimes, not providers
or external targets.

`lab/qualify_shared_clients.py` uses the actual installed registrations for a
read-only simultaneous-client check. Add `--check-migration` immediately after
cutover to compare history with the saved migration fingerprint; omit that flag
after legitimate engagement changes have advanced the history.
