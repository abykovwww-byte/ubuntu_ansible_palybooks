---
name: rp-stack-devkit
description: Develop, test, diagnose, publish, and verify the Tavern RP Stack through its GitHub IaC and abykovserv pull-based deployment workflow. Use for Gateway, Light GUI, Showroom, WorldPacks, provider canaries, live diagnostics, CI, Graphify, or RP Stack deployment work.
---

# RP Stack Devkit

## Start here

1. Read the repository `AGENTS.md` and the closest nested `AGENTS.md`.
2. Read the relevant repository skill completely:
   - `codex-skills/abykovserv-iac-deploy/SKILL.md` for deployment or live verification;
   - `codex-skills/rp-stack-wiki/SKILL.md` for architecture or significant behavior;
   - `codex-skills/rp-world-pack-builder/SKILL.md` for RP/novel worlds;
   - `codex-skills/training-world-pack-builder/SKILL.md` for deterministic training.
3. Query Graphify first for architecture or relationship questions when `graphify-out/` exists, then confirm decisive claims in source.

## Development path

1. Work in a `codex/` branch or isolated worktree and inspect the dirty tree before editing.
2. Preserve Gateway authority and keep UI changes presentation-only unless the API contract is deliberately changed.
3. Add focused tests and update the RP Stack Wiki in the same change where required.
4. Run `powershell.exe -File scripts/ci.ps1` for the deterministic local gate.
5. Use `scripts/run-rp-stack-evals.ps1 -Mode Offline` for the offline eval report.
6. Publish intentionally: commit, push, draft PR, then apply only when explicitly requested.
7. After apply, run container, HTTP, and—when UI behavior changed—authenticated browser verification.

## Safe operations

Use the `rp-stack-ops` MCP tools for read-only diagnostics:

- `local_revision`, `server_revision`, `ansible_status`, `compose_status`;
- `http_smoke`, `gateway_test`, `recent_logs`;
- `provider_summary`, `request_trace`, `backup_status`.

The MCP intentionally exposes no deploy, restore, delete, or live-write operation. It redacts probable credentials and validates all variable arguments before constructing a remote command.

## Eval layers

- Offline: schemas, deterministic WorldPack runtime, Gateway tests, UI tests, hook policy, Wiki links, and plugin validation.
- Provider canary: `scripts/run-rp-stack-evals.ps1 -Mode ProviderCanary ... -ConfirmProviderRun`; this calls the existing admin autotest API, creates a checkpoint branch, and leaves the source party unchanged.
- Browser smoke: follow `assets/browser-smoke-checklist.md` with the Browser skill against the deployed revision and save exact UI/API evidence.

## Scheduled work

Scheduled tasks may inspect repository drift, CI state, Graphify health, live health, and backups. They must report findings only: no merge, push, deploy, restore, secret rotation, or mutation of live party state.

This plugin does not install Sentry, OpenTelemetry, PostHog, or application telemetry. That work requires a separately approved design.
