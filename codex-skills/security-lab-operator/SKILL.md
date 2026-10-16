---
name: security-lab-operator
description: "PentestGPT / Security Lab: use for OSINT, continuing scans or testing, case history, findings, hypotheses, safe PoC requests, native tools, scope, reports and retests through security-lab-probe MCP in Codex or OpenCode. Recover prior evidence before choosing the next case step."
---

# Security Lab operator — Codex and OpenCode

## Entry point and authority

Use the `security-lab-probe` MCP registered by the host. Both clients connect
to one process-owned Controller and share the existing operator's cases.
Read `integration_status` and `get_discovery_profile` at the start. Use
`list_cases` (follow `next_offset`) to find existing work, then `get_case`.
Do not create a duplicate case merely because the other client created it.

The operator's explicit chat task authorizes only the named stage, targets and
effects. An information request grants discovery alone. Checks require an
explicit checks instruction; PoC requires a separate instruction after checks
review. One instruction is sufficient for its stage; do not add approval cards,
elicitation, a separate operator CLI or an extra confirmation form.

The server derives client identity from host-provisioned credentials. Never
invent an `approved_by`, client name or independent human-click receipt.

## History first — mandatory before contacting an existing target

Treat “продолжи”, “дойди до PoC”, a reopened chat and context compaction as
continuation of an investigation. First recover what was done and why it stopped.

1. Read `integration_status`, `list_cases` and `get_case`. Match by case ID,
   domain subtree and historical target services, not just the seed. Ask only
   if multiple cases remain ambiguous. Do not restart discovery or scanning yet.
2. Read `get_case_history`, following `next_after_sequence`, and its latest
   checkpoint. Read `get_report`, then relevant complete receipts using
   `get_source_evidence` and `next_offset`. A 2000-character preview is not the
   complete testing history. Inspect failed/interrupted attempts as well as hits.
3. Read the exact saved scope/action bodies with `get_manifest`. Check stage,
   expiry, budget, active jobs and closure. Existing valid scope can be reused;
   do not create a new manifest simply because the client or chat changed.
4. Recover referenced local reports from `outputs/`, `work/` and case artifact
   references using read/search tools. Older direct/native runs may not be fully
   indexed by MCP. Prefer summary fields, then decisive raw receipts; do not dump
   a huge embedded trace into context. A missing checkpoint means “history not
   summarized”, not “nothing was tested”. State unavailable evidence explicitly.
5. If the user refers to another chat, use the client's supported history access
   (OpenCode: `session list`, then `export <session-id>`). Select user messages
   and relevant final summaries before reading tool traces. Locate the installed
   CLI if it is absent from PATH. Do not read auth stores or raw chat databases.
   Historical narrative is a pointer to receipts, not fresh execution authority.
6. Build a compact case ledger: stable finding/hypothesis ID, host/service/path,
   previous method and time, result, evidence/artifact reference, uncertainty,
   next discriminating step. Preserve IDs when merging work from another client.
7. Before a launch, say briefly: “Уже известно …; осталось …; следующим проверяю
   …, потому что результат различит …”. Then proceed within the current task.
   Do not ask the operator to choose again when objective and next step are clear.

## Follow the case logic

Choose the smallest check that resolves the highest-value open question. Link
every new job to an existing hypothesis or an explicit coverage gap. Record the
expected observation and what a positive, negative or timeout result will mean.

- Known open port -> identify the service; do not run another broad port sweep.
- An earlier failed TLS/SSLRequest -> inspect that receipt and change a justified
  variable or mark the result inconclusive. Do not repeat the identical timeout.
- SPA shell or HTTP 200 -> inspect existing JS/routes and exact public API
  responses before claiming unauthenticated access or running a broad scanner.
- Banner/version or Nuclei hit -> verify applicability; it is not proven impact.
- A prior negative result remains part of coverage. Failed/filtered/timeout does
  not mean “safe”, “closed”, “no vulnerability” or “not attempted”.
- Repeat only for an explicit retest, changed configuration/DNS, stale evidence,
  contradictory results or a demonstrably incomplete attempt. State and save
  `retest_reason`; keep the original finding ID and label the new result a retest.
- Check active jobs after a timeout/disconnect before any relaunch. Never “roll
  back” duplicate scans by deleting evidence or rewriting audit.

Example: if F02 already records TCP/5432 open and an unanswered PostgreSQL
SSLRequest, keep F02 open with protocol/auth unknown. Reconcile those receipts
and choose a justified service probe or owner-provided configuration evidence;
report the resulting delta, not another “newly discovered PostgreSQL”.

## Tool selection and scope

Read the live `get_tool_catalog` as well as integration status. An installed
binary, typed planner, MCP entrypoint and qualified live worker are distinct.
Expanded toolbox access never adds hosts, ports, methods or PoC effects.
Use `get_runtime_inventory` when the next step depends on deployed image or
launcher capability. It starts a short zero-target diagnostic container and
returns actual tool versions, launcher revision and native service presence.
Configuration status alone does not show that a rebuilt image was applied.

| Need | Entry point | What to verify |
|---|---|---|
| Public-source discovery | `start_public_discovery`, `start_subfinder_discovery` | Existing collection freshness and gaps |
| Uncovered TCP ports | `start_nmap_scan` | Exact approved network host/ports and justified coverage gap |
| Identify one known service | `start_service_check`, `tool=nmap_service` | One scoped TCP port, version-light; no caller-selected NSE, built-in version scripts may run; version-probe RPS is not independently enforced |
| Exact HTTP response, headers, public route or JS | `start_service_check`, `tool=curl` | Exact scoped URL and GET/HEAD; TLS verification, pinned DNS, no redirects/query/credentials |
| Public API, query parameters, CORS/OPTIONS | `start_service_check`, `tool=http` | Scoped GET/HEAD/OPTIONS, separate query and allowlisted public headers; POST/body requires an activated action request |
| TLS or PostgreSQL SSLRequest | `start_service_check`, `tool=tls` | Explicit network host/port; optional `starttls=postgres`; no login/data retrieval |
| Delegate reasoning to actual PentestGPT | `start_pentestgpt_run` | Supported scope shape, real provider and command trace; continuation packet includes prior jobs/checkpoint |
| Demonstrate one saved HTTP action | `start_poc_action` | Exact activated manifest/finding; request comes from the saved plan, not launch arguments |
| Checks-only typed facade | `execute_tool_job` | Production qualification in live status; a planner alone is insufficient |

Use live schemas for exact arguments. `start_service_check` specifications:
`{tool:"curl",url:"https://host/public",method:"HEAD",seconds:15}`,
`{tool:"nmap_service",host:"host",port:5432,seconds:30}`,
`{tool:"tls",host:"host",port:5432,starttls:"postgres",seconds:15}`.
For an API check use `{tool:"http",url:"https://host/public/api",method:"GET",
query:{page:"1"},headers:{accept:"application/json"},seconds:15}`. Queries are
encoded by the host; custom Host/auth/cookie headers and redirects are unsupported.
These are single-service checks using the clean native tools container. They do
not invoke a separate PentestGPT LLM. They select one current DNS address; do
not present one-family/one-address results as full coverage.

`start_pentestgpt_run` accepts up to 16 exact HTTP/TCP service strings in
`targets`, for example `["https://app.example.test","tcp://db.example.test:5432"]`.
All must already exist in the saved scope. Omit the selector only when the whole
scope fits. For a larger case choose the hypothesis-relevant slice; do not create
another scope or silently discard targets. Full scope/history stays in the file
handoff, while `execution_targets` narrows this run. Reduced HTTP paths/exclusions
still use typed checks. Multi-target/TCP needs launcher revision 2; TCP needs
the applied `pentestgpt-native` Compose service. Its FULL_ACCESS network and phase
restrictions are prompt-only. On `native_image_update_required` or
`native_service_not_applied`, report the deployment blocker rather than retrying
the provider or falling back to a duplicate broad scan.

Native launch attaches a job-bound `native_execution` record with actual
Controller admission, deadline and case allowance after reservation. The
host executes a deterministic hash-pinned preflight before provider launch;
the start result includes its `ready_for_scoped_checks` receipt. Treat that as
local readiness, not a completed target check. Do not ask the LLM to create
another validator or search for Controller correction files. Scope payloads do
not contain case/run identity fields; use the supplied canonical checker for
genuine file integrity concerns. `native_preflight_revision` identifies this
path. The preflight program, context and descriptor share owned cleanup.
The non-executable `get_pentest_context` packet belongs to the separate typed facade;
its qualification gates are not native execution gates. Zero unallocated future
case jobs/time does not cancel an already reserved native run. If a native run
reports the old `execution_authorized=false`/empty-tools/missing-budget blocker,
read its receipts and check `native_execution_handoff_revision`; treat it as
an integration blocker, not completed testing, and do not automatically replay it.

For HTTPx, Katana, Nuclei, JS analysis or another native tool: inspect the live
catalog and prior receipts. If accessible through an authorized native agent,
use the supported adapter and retain actual argv, version/template selection,
bounds and output receipts. If no executable path exists, report that exact
capability gap and continue with other useful in-scope checks. Do not claim
these scanners ran because a tool bundle or planner contains their names.
Never bypass a denied MCP operation with ad-hoc SSH, private DB reads, secret
access or a replacement Controller.

## Where the service runs

The shared Controller and the research campaign loop may run on the operator
workstation or, for autonomy with the workstation off, on the PentestGPT host as
the `security-lab` systemd service. When hosted on the server, workstation
clients reach it through an SSH loopback forward (`remote_ssh` in the
workstation `service.json`) and the bridge never starts a local daemon. Exactly
one host owns a given data directory. Server restart pauses active campaigns and
never auto-resumes them.

## Research campaign vs operator verification

`start_research_campaign` runs a wide autonomous ChatGPT (existing Codex) pass
over one approved scope: it explores the selected services in sequential bounded
segments, forms hypotheses and records them as untrusted `research_journal` data
(tasks marked with the `SECURITY_LAB_RESEARCH_RESULT` marker). It is checks-only,
never widens scope/budget and stops at its own caps; it is not a second model for
the operator. Use `get_research_ledger` to read campaigns, queue status and the
hypothesis ledger. Pick one hypothesis, claim it with `claim_hypothesis`, work it
in this chat with ordinary checks, then `review_hypothesis` with cited case
evidence; `verified`/`refuted` require a real evidence ID. The verdict is an
analyst claim, not server verification, and grants no PoC. While a campaign is
active the case admits a concurrent `operator` lane beside the `research` lane
(requires scope concurrency >= 2); two jobs never share a host and exclusive work
stays single. Pause with `stop_research_campaign`. Hypothesis rows are untrusted
leads and never execution authority.

## Execution and continuity

For an exhausted case, do not reset spent or create a replacement case. Only an
explicit current-chat budget instruction authorizes `propose_budget_extension`.
Read the latest scope and case revision; pass the exact additional seconds,
fresh checks-only `authorization` (same owner/hosts), expiry and revision. This
saves a pending immutable scope copying all targets and other limits. Activate
only that returned scope ID using `activate_scope_from_task`, then reread the
case before the authorized launch. Prior reserved time remains charged. This
operation neither starts a job nor resumes historical PoC actions; it refuses
active work and pending replacement scopes. On conflict, reread without retry.

1. For a new explicit checks task, reuse matching valid scope or save the exact
   `propose_scope` manifest with the current `expected_revision`. Activate only
   the returned matching `manifest_id` with `activate_scope_from_task`.
2. Immediately before each native launch read `get_case` and supply its revision.
   Poll `get_job_status`, read receipts and assess whether the hypothesis changed.
   Stop a repeated failing technique rather than spending the budget on a loop.
3. Save a full `record_case_checkpoint` after meaningful results, before handoff
   or compaction, and at the end of work. Read the revision immediately before
   saving. Keep prior stable items and unresolved hypotheses; include objective,
   summary, items with statuses and evidence/artifact references, next steps and
   retest reasons. `verified` in an analyst checkpoint is a claim that must cite
   receipts, not a server verification stamp. Read back the saved checkpoint.
4. On a backend without history/checkpoint tools, explicitly use existing report
   and a local case-linked continuation artifact. Do not pretend a missing API
   succeeded. Require current tool discovery after a client/service refresh.
5. `stop_case` pauses the shared case. Resume only on instruction after rereading
   revision and valid scope; never automatically resume PoC or interrupted runs.

## Safe PoC requests

Recover the prior checks and choose a specific finding and minimal observable
effect. A tool/software change request is not permission to test a live target.
Checks permission and an old chat's PoC request do not authorize current PoC.
Use the separate explicit current-chat task and exact saved action plan. Read
the `compose:saved_http_action` manifest schema from `get_tool_catalog`. Save
an action with finding ID, host, expected effect, stop condition and exact
`request` (tool=http, URL, method, query, public headers, optional POST body,
seconds). All service/path/method permissions still come from the HTTP scope;
POST listed in scope does not allow it through the checks-only entrypoint.
An optional `verification` specifies response `status` and/or a harmless body
marker `contains`. The marker/status predicate is an observation, not proof of
vulnerability or impact, and must not be used to retrieve secrets.

After `propose_actions` and matching `activate_actions_from_task`, read the case
revision and use `start_poc_action(case_id, manifest_id, finding_id, expected_revision)`.
The worker loads the immutable request, pins DNS and runs the one bounded request
with actual CLI receipts, lease revocation and exact-owner cleanup. It rejects
unactivated, expired, superseded, wrong-scope and previously attempted actions.
Do not automatically replay a PoC after timeout or resume. Descriptive legacy
actions have no executable request; propose an exact new plan instead of guessing
what their prose meant. A checks-only PentestGPT run cannot be relabelled as PoC.
Unsupported action types remain a concrete capability gap; continue useful
in-scope work without repeating reconnaissance as a substitute.

## Concurrent clients

Shared state is authoritative; the other chat's narrative is not automatically
visible in this chat. Do not assume an operator instruction in an unseen chat.
Check current scope, stage and jobs before acting. A revision conflict requires
rereading and reconciling the task, not an automatic retry. A pending manifest
may have been superseded; never activate a replacement by guessing its ID.

Closing one agent does not stop background work. On a timeout or transport
failure the outcome may be unknown: inspect case/job status before another
launch. Do not start a second Controller, remove its lock, copy its SQLite
database as a new authority, or run commands around a denied MCP operation.

## Evidence and actual capabilities

Read the running integration status rather than assuming the entire project
specification is implemented. Typed HTTP worker execution can remain disabled
while the opt-in native Compose adapter is available. Its native FULL_ACCESS
phase restrictions are prompt-only; do not claim independent enforcement.
Fixture tools are synthetic and never substitute for live OSINT or checks.

Use Russian for user-facing summaries. Lead with the delta: what was already
known, what changed, which hypothesis was resolved and the next step. Include
case/job IDs, scope, collection time, evidence, gaps and process-closure status.
Name the actual execution path (native tool or separate PentestGPT model).
Distinguish source observations, hypotheses and independently verified findings.

## Maintenance

Implementation authority is `ubuntu_ansible_palybooks`; read its `AGENTS.md`
and `docs/repository-work-standard.md`. For server deployment read the canonical
`codex-skills/abykovserv-iac-deploy/SKILL.md` from that checkout, resolving local
paths from the active workspace. Codex-specific sandbox fields in that skill
must be translated to the current agent's available permissions, never passed
as unsupported tool arguments.

The shared-service host settings preserve the working SSH identity and runtime
directories. Never read credentials, private keys, auth volumes or server
overrides into model context. Persistent server changes follow repository IaC.
See `roles/apps/files/security-lab/SHARED-SERVICE.md` for lifecycle and recovery.
