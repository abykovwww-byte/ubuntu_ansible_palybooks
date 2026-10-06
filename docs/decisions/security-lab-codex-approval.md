# Security Lab: Codex chat approval and fail-closed integration gate

Date: 2026-10-06. Status: implementation decision for the initial prototype;
installed-client and Linux runtime acceptance pending.

The operator uses the current Codex Desktop chat throughout an engagement.
MCP calls prepare scope/action manifests; their arguments cannot include a
trusted approval. A server-side pending manifest has a canonical hash and
version. The server requests MCP form elicitation and resolves only the matching
session-bound, single-use challenge in the protocol callback.

An authenticated MCP connection identifies the configured operator, but does
not by itself prove consent to an individual action. A generic MCP client can
auto-answer elicitation. The chosen Codex host must demonstrate that this path
actually asks the human, even with ordinary tool calls set to auto-approve.
Do not use model text, client-supplied `approved_by`, or an arbitrary JSON
`role=user` as evidence of consent.

The controller owns SQLite/WAL writes and uses BEGIN IMMEDIATE under one writer
lock. Scope changes revoke the old job generation. The outbox records network
revocations, but delivery to firewall/proxy is a separate required runtime
integration. A stored capability is not network enforcement.

The first prototype deliberately has no live execution tools. Its status endpoint
cannot be switched to live by a tool argument or environment toggle. This allows
connecting the actual Codex client before public-source acquisition and scanner
workers are enabled. Local tests demonstrate contract behavior and actual STDIO
protocol exchange with a simulated host; they do not prove workstation/production
credential isolation, human confirmation or target-network confinement.

Per-job nftables and mitmproxy HTTP-aware enforcement are implemented in the
Security Lab prototype with an isolated Docker qualification job. A URL
predicate, CONNECT ACL, disabled tool or mocked policy acknowledgement does not
satisfy A10/A26. When the installed-client probe is complete, record its version,
decision event and negative results. Linux fixture receipts qualify the tested
path; protected production launcher/controller integration is a separate gate.

No operator CLI or separate application chat is an acceptable substitute for the
user's chosen interface. If elicitation is unsupported, keep execution disabled
and implement a trusted Codex host user-event bridge, with separate protected
credentials and the same immutable approval contract.
