# Security Lab: authorization through delegated Codex chat tasks

Date: 2026-10-06. Status: accepted for the local prototype by the operator's
explicit instruction. This supersedes obligatory form elicitation and independent
host-event confirmation in the earlier decision and TZ versions 1.1–1.2.

## Decision

The operator's explicit task in the current Codex chat is sufficient authorization
for its targets, stage and effects. Codex is the authorized delegate. No extra
card, popup, push confirmation, operator CLI or mandatory signed-user-event
bridge is required. A find-information command grants discovery; a checks command
grants checks for its specified targets; PoC requires a separate explicit command
after checks review. Newly discovered assets are not silently included in checks.

Codex proposes the strict immutable manifest, then activates its exact ID through
`activate_scope_from_task` or `activate_actions_from_task`. The Controller
validates ownership, pending status, canonical hash, expiry, stage and ROE and
records activation atomically. It derives `approved_by` from its configured
principal with a `delegated:codex:` prefix and records the source
`codex_operator_delegation` and `independent_human_receipt=false` in audit.

This is delegated authority, not independent verification of a human UI event.
The backend trusts its Codex delegate to act from the actual chat task; it cannot
cryptographically distinguish an authorized task from a compromised delegate's
claim. External-source prompt injection is tested at agent level. Workers never receive activation
credentials. The local workstation prototype does not prove protected production
store/credential isolation or independently verified OpenAI identity.

The retired request-confirmation tools and challenge handler are removed. Existing
challenge rows are historical only; new databases do not create that table.
Historical transport responses are not rewritten as observed human refusals or
consent. Recorded approvals retain their original provenance.

## Preserved implementation boundaries

The Controller owns SQLite/WAL with one process writer and BEGIN IMMEDIATE.
Scope changes revoke the prior generation; network delivery uses an outbox and
must be acknowledged before a job runs. A capability record is not enforcement.
Expiry/replay/tampering, foreign cases and automatic progression to PoC are denied.

Lifecycle revision 2 fixes reproduced inventory startup contention on Windows.
Only a case operation opens the Controller; inventory/status do not create or
lock its store. Competing case sessions remain denied and can retry after release.
Production multi-client routing to one Controller is still required.

The native form probe returned rapid decline/cancel without any visible form,
confirmed by the operator. The last revised request cancelled in 32 ms. This is
history explaining the superseded approach, no longer a blocking integration gate.

## Readiness

Production target checks/PoC execution remains disabled. Local version 0.3.0
adds typed dispatch, install-before-start ACK, lease sequence/renewal, protected
lease sequence contracts, cancellation-safe closure receipts,
conservative durable budgets/schema v2 and a checks-only PentestGPT context.
The local bound five-tool facade and authenticated transport now connect a
scripted checks adapter to the real PentestGPT loop. Scope reservation is atomic;
host DNS snapshots are revalidated before install/renewal. Recovery candidates
must pass the same bound worker evidence checks, and model-runner resume remains
closed. This uses no approval interface or second Controller writer.
The operator has removed the interception implementation, certificate generation,
its network harness and associated CI job. HTTP plans now specify direct scoped
GET/HEAD with standard TLS verification and explicit proxy bypass. Controller
scope checks, typed URL validation and DNS snapshots remain admission contracts,
not independent host/network enforcement. Production direct-worker isolation,
killable private DNS, network scope and a qualified real-model PentestGPT tool
consumer remain unfinished. The public MCP dispatch path executes only the
zero-network fixture. A direct Docker curl candidate exists for trusted owned
laboratory qualification: UID1001/cap0, canonical pinned address, empty environment,
namespace nftables timed destination membership with strict read-back, protected
lease and independent PID1 watchdog. It is not production-qualified. The frozen
v6 direct implementation passed three owned Linux runs on 2026-10-07 with ten
checks each, using real curl/TLS and receiving-side connection receipts; the
final local suite passed 618 tests. Operator stop closed the receiving connection
in 0.522–0.524 seconds, and heartbeat loss in 3.556–3.807 seconds on this fixture.
All six workers per run were removed, then exact temporary lab image tags and
directories were cleaned. Public dispatch and the conservative
`direct_worker_candidate_unqualified` status remain unchanged: these receipts
qualify only the owned curl fixture, not production DNS, another scanner or a
real-model PentestGPT consumer. Negative TCP probes ran in the IPv4-selected
worker; positive IPv6 is separate and UDP DNS is not traffic-qualified.
No HTTP/TLS interception is introduced. Historical interception fixture artifacts
do not qualify direct execution or any real scanner. MCP SDK tests prove real
STDIO activation without form callbacks; installed-client read-back must be
reported separately. Version 0.2.0 adds credential-free public-source collection
with durable receipts and a portable surface graph; this does not require
checks authorization or grant checks scope. Source lookup and target execution
have separate readiness fields. Full OSINT → PentestGPT acceptance is still pending.

Version 0.3.0 adds an opt-in adapter for the existing Compose PentestGPT runtime,
explicit network-target scope for fixed Nmap TCP-connect scans and passive
Subfinder CLI collection. The native model agent retains full shell access;
checks-only behavior is prompt-only and reduced path/exclusion scopes are refused.
Typed production HTTP dispatch remains disabled. The new tools-only profile has
direct egress without model credentials; its launcher checks are not a firewall.

On 2026-10-07 the operator authorized delivery through the Git/IaC workflow.
Delivery is working-branch commit/push, PR, green CI, merge and the server's
normal pull/Ansible apply. Direct SCP delivery is cancelled. Local tests,
CI, merged source and actual server/image verification remain separate evidence.
