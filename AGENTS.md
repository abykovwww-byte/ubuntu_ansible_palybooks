# RP Stack repository instructions

## Scope and authority

- This repository is the durable authority for `abykovserv`. Do not hot-edit `/srv/apps/rp-stack` as a normal implementation path.
- `rp-gateway` owns canonical party state, turn commits, scoring, memory, provider policy, and training evidence. Light GUI and Showroom are presentation clients and must not become authorities for canonical data.
- Keep delivery states distinct: local edit, tested, committed, pushed, Ansible-applied, container-tested, HTTP-verified, and browser-verified.
- Never read, print, copy, or commit `/etc/ansible/local-overrides.yml`, `.env` values, API keys, cookies, passwords, PATs, or monitoring credentials.

## Required workflows

- For RP Stack architecture or significant behavior changes, use `codex-skills/rp-stack-wiki/SKILL.md` and update the Wiki in the same change.
- For `abykovserv` deployment, use `codex-skills/abykovserv-iac-deploy/SKILL.md`. Deployment is `commit -> push -> ansible-local-apply.service -> runtime verification`.
- For RP/novel WorldPacks, use `codex-skills/rp-world-pack-builder/SKILL.md`.
- For deterministic training WorldPacks, use `codex-skills/training-world-pack-builder/SKILL.md`.
- For architecture and relationship questions, query the repository Graphify graph first when `graphify-out/` is present, then confirm decisive claims against source.

## Development discipline

- Work on a `codex/` branch or an isolated worktree. Keep unrelated user changes intact.
- Do not start local RP Stack application servers. Windows checks are static/offline evidence; the rebuilt Gateway container is the authoritative runtime test environment.
- Prefer focused tests while iterating, then run `powershell.exe -File scripts/ci.ps1` before publishing.
- Provider canaries must use the existing admin autotest branch flow. Never advance or mutate the source party.
- Read-only diagnostics should use the `rp-stack-ops` MCP/CLI. It intentionally has no deploy, restore, or mutation tools.
- Scheduled maintenance may inspect and report. It must not merge, push, deploy, restore, rotate secrets, or modify live state without a new explicit instruction.

## Documentation and generated state

- Keep `docs/wiki/README.md` navigation valid and update the relevant page for architecture, operations, security, testing, provider, training, or deployment changes.
- Do not commit Graphify runtime caches or generated environment folders. A Graphify update is complete only after source changes, semantic merge/health checks, and a focused query confirm the updated graph.
- Do not add Sentry, OpenTelemetry, PostHog, or other application telemetry as part of the RP Stack devkit work; instrumentation requires a separate scoped decision.
