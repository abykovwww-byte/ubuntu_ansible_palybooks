# RP Stack repository instructions

## Scope and authority

- This repository is the durable authority for `abykovserv`. Do not hot-edit `/srv/apps/rp-stack` as a normal implementation path.
- `rp-gateway` owns canonical party state, turn commits, scoring, memory, provider policy, and training evidence. Light GUI and Showroom are presentation clients and must not become authorities for canonical data.
- Keep delivery states distinct: local edit, tested, committed, pushed, Ansible-applied, container-tested, HTTP-verified, and browser-verified.
- Never read, print, copy, or commit `/etc/ansible/local-overrides.yml`, `.env` values, API keys, cookies, passwords, PATs, or monitoring credentials.

## Required workflows

- At the start of repository work, read `docs/repository-work-standard.md` for the current workstation, SSH, sudo, toolchain, plugin, Graphify, and skill-sync facts.
- For RP Stack architecture or significant behavior changes, use `codex-skills/rp-stack-wiki/SKILL.md` and update the Wiki in the same change.
- For `abykovserv` deployment, use `codex-skills/abykovserv-iac-deploy/SKILL.md`. Deployment is `commit -> push the working branch -> non-draft PR -> green CI -> merge into main -> ansible-local-apply.service -> runtime verification`.
- For RP WorldPacks, use `codex-skills/rp-world-pack-builder/SKILL.md`.
- For deterministic training WorldPacks, use `codex-skills/training-world-pack-builder/SKILL.md`.
- For architecture and relationship questions, query the repository Graphify graph first when `graphify-out/` is present, then confirm decisive claims against source.

## Development discipline

- Work on a `codex/` branch or an isolated worktree. Keep unrelated user changes intact.
- Push only the working branch, open a non-draft pull request, and merge it into `main` after CI is green. Direct pushes to `main` are prohibited; do not leave merge-ready work on the branch.
- Do not start local RP Stack application servers. Windows checks are static/offline evidence; the rebuilt Gateway container is the authoritative runtime test environment.
- Prefer focused tests while iterating, then run `powershell.exe -File scripts/ci.ps1` before publishing.
- Scoped exception: implementation slices of Decision 043 follow its
  stage-specific focused checks. The full local `scripts/ci.ps1` run is required
  at integration/cutover, not for every slice; the existing GitHub workflow is
  unchanged until the separate global simplification track.
- Provider canaries must use the existing admin autotest branch flow. Never advance or mutate the source party.
- Read-only diagnostics should use the `rp-stack-ops` MCP/CLI. It intentionally has no deploy, restore, or mutation tools.
- Scheduled maintenance may inspect and report. It must not merge, push, deploy, restore, rotate secrets, or modify live state without a new explicit instruction.

## Readiness and semantic evidence

- Report RP Stack readiness only as `каркас`, `подключено`, `наблюдается`, or `держится`. Green CI is necessary delivery evidence, but it is not proof that a mechanic is observed or durable on a real party.
- Keep Decision status separate from implementation readiness. Requirements live in `roles/apps/files/rp-stack/docs/decisions/registry/NNN.yml` and are checked by `scripts/validate-adr-registry.py`.
- Decision 043 is its own rebuild anchor and intentionally has no registry entry;
  its implementation evidence is recorded at the mechanism acceptance and
  cutover boundaries defined there, not in a per-slice readiness registry.
- Treat `roles/apps/files/rp-stack/evals/acceptance/manifest.yml` and `evals/acceptance/corpus/**` as an independent user-owned, read-only oracle. Never change labels or thresholds in the same change as mechanism code.
- Report event precision, event recall, character attribution accuracy, empty-scene false-positive rate, positive-trust recall, and correction retention separately; do not replace them with one aggregate score.
- Keep evidence layers separate: offline uses saved responses and no providers; provider-canary uses a real model through an isolated admin-autotest branch; production-endurance follows `causal_probe` through later scene consequences.
- Before applying a revision that introduces or materially expands exact diagnostic prompt/response capture (`service_call_log`, `turn_trace_events`), or changes its retention/redaction, stop for the user's explicit retention and redaction decision. An accepted ADR for that exact revision satisfies the gate; green CI or a configured default does not.

## Documentation and generated state

- Treat any change to a working-format contract (path, identity key, permissions, tool availability, plugin location, or contract schema) as incomplete until `docs/repository-work-standard.md`, every affected repository skill/plugin instruction, and the corresponding `scripts/validate-repository.py` guard are updated in the same change.
- Scoped exception: Decision 043 World/Scenario implementation uses the
  production schema/loader as the executable source of truth and updates only
  the directly used authoring instruction when its external contract changes.
  Do not mirror its internal schema into repository standard, unrelated
  skills/plugins, Wiki pages, registry entries, or repository-validator guards.
  Update the Wiki once when the deployed architecture or operator workflow
  actually changes, following the cutover stages in that decision.
- Keep `docs/wiki/README.md` navigation valid and update the relevant page for architecture, operations, security, testing, provider, training, or deployment changes.
- Do not commit Graphify runtime caches or generated environment folders. A Graphify update is complete only after source changes, semantic merge/health checks, and a focused query confirm the updated graph.
- Do not add Sentry, OpenTelemetry, PostHog, or other application telemetry as part of the RP Stack devkit work; instrumentation requires a separate scoped decision.
