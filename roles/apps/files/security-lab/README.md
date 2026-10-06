# Security Lab control plane — integration prototype

This is the first implementation slice of the OSINT → scope review → PentestGPT
workflow. The user's working interface is the existing Codex Desktop chat.
There is no operator CLI and no separate approval website.

Implemented locally:

- strict, versioned domain/service scope, authorization and action contracts;
- a single-writer SQLite/WAL case controller with process ownership lock;
- immutable manifest versions, exact hashes, audit, revoke generations and outbox;
- single-use, session-bound, five-minute confirmation challenges;
- server-initiated MCP form elicitation, with no model-callable approval endpoint;
- worker/profile-bound hashed capabilities with a thirty-second lease;
- pause/resume that checks scope validity and preserves the recorded budget;
- offline, explicitly synthetic discovery fixtures and escaped portable HTML;
- actual STDIO MCP SDK protocol tests, including unsupported elicitation.

**Live collection and live jobs are disabled.** `integration_status` always
reports this. No scanner, HTTP acquisition or external API is exposed by this
prototype. A synthetic fixture and an automated SDK confirmation are not
acceptance of the installed Codex Desktop or Linux network isolation.

## Integration gates

1. Register the prototype MCP server in the installed Codex client.
2. In that client, create a synthetic case and pending scope. Verify that
   `request_scope_confirmation` shows the saved targets/limits/authorization
   and collects a human response inside the chat. Decline/replay/changed scope
   and auto-approve without a human answer must not grant authorization.
3. The trusted host/transport and server data must be isolated from the model's
   shell. This local developer prototype has ordinary workstation file rights;
   it **does not prove** production isolation of its database or host identity.
4. Verify nftables gateway and mitmproxy addon in an isolated Linux job with
   positive and negative TCP/IPv6/HTTPS tests, including revocation of an
   existing connection. They are not implemented by a Python URL predicate.
5. Only after these gates may the next slices add live OSINT providers,
   HTTP acquisition, scanner workers and PentestGPT episode orchestration.

The SDK documents that a generic MCP client can handle elicitation
automatically. Therefore an `accept` response is meaningful only from the
verified, trusted Codex confirmation transport; MCP support by itself does not
prove a human clicked or replied. See the [pinned SDK context implementation](https://github.com/modelcontextprotocol/python-sdk/blob/v1.26.0/src/mcp/server/fastmcp/server.py).

## Developer checks

Use Python 3.12 and the tracked `uv.lock`:

```text
uv sync --frozen
uv run --frozen pytest
```

On this Windows host, use the project `.venv/Scripts/python.exe` and an approved
unique pytest basetemp under the workspace when the sandbox blocks temp files.
No OpenAI API key is needed or introduced.

`server.py --data-dir <private-directory> --principal <host-provisioned-identity>`
is the host launch contract for the STDIO probe, not a command the user must run
to conduct an engagement. The principal is configuration for the local probe;
it is not a claim of verified OpenAI account identity. Production requires a
separately provisioned authenticated connection and protected controller store.

## Current limitations

There is no live dispatcher, capability renewal, budget accounting from actual
workers, retention/purge worker, schema migration beyond version-one guard,
live OSINT adapter, scanner image or PentestGPT handoff yet. The action contract
and confirmation handler exist, but the checks stage must be implemented before
the action path can run normally. No A01–A36 full-cycle acceptance is claimed.

The existing PentestGPT Compose/runtime remains the running path until the new
control plane passes integration. Deployment must use repository Ansible
delivery; this prototype is not automatically enabled in server inventory.
