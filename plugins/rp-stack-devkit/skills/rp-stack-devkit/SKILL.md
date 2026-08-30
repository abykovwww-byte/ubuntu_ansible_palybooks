---
name: rp-stack-devkit
description: Develop, test, diagnose, publish, and verify the Tavern RP application and its deployment boundary to the standalone Showroom/Training application through GitHub IaC and the abykovserv pull-based workflow. Use for RP Gateway, RP Light GUI, RP WorldPacks, provider canaries, live diagnostics, CI, Graphify, or cross-stack cutover work; route Showroom/Awareness application source changes to tavern-awareness-showroom.
---

# RP Stack Devkit

## Start here

1. Read the repository `AGENTS.md` and the closest nested `AGENTS.md`.
2. Read `docs/repository-work-standard.md` for the checked workstation, SSH,
   sudo, toolchain, plugin, Graphify, and skill-sync contract.
3. Read the relevant repository skill completely:
   - `codex-skills/abykovserv-iac-deploy/SKILL.md` for deployment or live verification;
   - `codex-skills/rp-stack-wiki/SKILL.md` for architecture or significant behavior;
   - `codex-skills/rp-world-pack-builder/SKILL.md` for RP worlds;
   - `codex-skills/training-world-pack-builder/SKILL.md` for deterministic training in `tavern-awareness-showroom`.
4. Query Graphify first for architecture or relationship questions when `graphify-out/` exists, then confirm decisive claims in source.

## Development path

1. Work in a `codex/` branch or isolated worktree and inspect the dirty tree before editing. Push only the working branch; direct pushes to `main` are prohibited.
2. Preserve RP Gateway authority for RP only. The standalone Training Gateway owns training state, scoring, evidence, and provider calls; keep both SQLite databases and route surfaces isolated.
3. Add focused checks only for a changed player-visible or safety boundary, and remove dedicated tests, fixtures, and guards with the mechanism they protected. Update the RP Stack Wiki only when deployed architecture, an external contract, or an operator workflow changes.
4. Run the focused checks for the changed surface. Run `powershell.exe -File scripts/ci.ps1` only for shared-gate changes, cross-component integration/cutover, or before deployment.
5. Use `scripts/run-rp-stack-evals.ps1 -Mode Offline` only when gameplay, prompt, extraction, or evaluation semantics can change.
6. Publish intentionally: commit, push the working branch, open a non-draft PR,
   wait for green CI, and merge it into `main`; do not leave merge-ready work on
   the branch.
7. Stop at `merged` for the user's interactive sudo apply when deployment was
   requested; never request or capture the sudo password.
8. After apply, run container, HTTP, and—when UI behavior changed—authenticated browser verification.

Awareness/training source changes and their deterministic scoring checks run in
`tavern-awareness-showroom`; this repository owns only RP behavior, the exact
standalone application pin, and the cross-stack deployment boundary.

## Readiness evidence contract

For Decision 022 requirements, report readiness only with this dictionary:

- `каркас`: code exists and module tests are green; this says nothing
  about a real party;
- `подключено`: the code executes in the real turn path and its input reaches it;
- `наблюдается`: the effect is present in the mechanic's authoritative store and
  the causal chain reaches the next-turn prompt on a real party;
- `держится`: the chain repeatedly reaches a scene that accounts for the effect,
  without drift.

Do not replace these levels with bare `implemented`, `working`, `ready`,
`реализовано`, `работает`, or `готово` claims. Green CI is necessary evidence,
but is insufficient for `наблюдается` or `держится`.

The acceptance oracle is independent, user-owned, and read-only:
`roles/apps/files/rp-stack/evals/acceptance/manifest.yml` and
`roles/apps/files/rp-stack/evals/acceptance/corpus/**`. Do not relabel examples,
change thresholds, or overwrite these files. Read every threshold from the
manifest and report `event_precision`, `event_recall`,
`character_id_accuracy`, `empty_scene_false_positive_rate`,
`positive_trust_recall`, and `correction_retention` separately, including
per-event-class results when required by the manifest.

Keep the three evidence layers distinct:

- offline: schemas, saved responses, tautology detection, and separate metrics;
  never call a provider;
- provider-canary: real prompt and model, repeated through admin-autotest; never
  advance or mutate the source party;
- production-endurance: a long live party and `causal_probe` through “scene
  accounts for the effect”; only this layer can establish `держится`.

Before deploying a revision that introduces or materially expands exact
diagnostic prompt/response capture (`service_call_log`, `turn_trace_events`), or
changes retention/redaction, stop and obtain the user's decision on retention
and redaction depth. An accepted ADR for that exact revision satisfies the gate;
do not infer approval from a green PR, a configured default, or an earlier
environment setting.

## Safe operations

Use the `rp-stack-ops` MCP tools for read-only diagnostics:

- `local_revision`, `server_revision`, `ansible_status`, `compose_status`;
- `http_smoke`, RP-only `gateway_test`, RP-only `recent_logs`;
- `provider_summary`, `request_trace`, `backup_status`.

`causal_probe` accepts the registered expectations
`seed_trust_influences_plot`, `relationship_pressure_reaches_next_turn_prompt`,
`relationship_event_has_canonical_character_attribution`,
`relationship_badge_has_canonical_character_attribution`, and
`trust_gained_reaches_next_turn_prompt`. Treat the reported `break_at` as the
evidence boundary; do not turn an earlier passing step into a readiness claim.

The MCP intentionally exposes no deploy, restore, delete, or live-write operation. It redacts probable credentials and validates all variable arguments before constructing a remote command.

## Eval layers

- Offline: `scripts/run-rp-stack-evals.ps1 -Mode Offline`; providers are not
  called, and the report keeps the acceptance metrics separate.
- Provider-canary: `scripts/run-rp-stack-evals.ps1 -Mode ProviderCanary ...
  -ConfirmProviderRun`; this calls the existing admin autotest API, creates a
  checkpoint branch, and leaves the source party unchanged.
- Production-endurance: exercise a long live party and use `causal_probe` to
  prove the chain through later scene consequences without drift.

Browser smoke remains a post-deployment interface check: follow
`assets/browser-smoke-checklist.md` with the Browser skill against the deployed
revision and save exact UI/API evidence. It does not replace production
endurance.

## Scheduled work

Scheduled tasks may inspect repository drift, CI state, Graphify health, live health, and backups. They must report findings only: no merge, push, deploy, restore, secret rotation, or mutation of live party state.

This plugin does not install Sentry, OpenTelemetry, PostHog, or application telemetry. That work requires a separately approved design.
