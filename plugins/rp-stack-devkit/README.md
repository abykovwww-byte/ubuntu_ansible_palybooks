# RP Stack Devkit plugin

Repository-scoped Codex plugin for safe RP Stack development and operations.

It packages:

- an orchestration skill for the repository's established IaC, Wiki, WorldPack, and Graphify workflows;
- a read-only `rp-stack-ops` MCP server and CLI;
- a bounded three-layer eval runner;
- policy hooks that block common destructive or secret-bearing tool calls;
- a browser smoke checklist for post-deployment verification.

The plugin does not contain credentials and has no live mutation or deployment tool.

## Decision 022 readiness contract

RP Stack readiness reports use only `каркас` (code and module tests are green),
`подключено` (execution in the real turn path), `наблюдается` (effect in the
authoritative mechanic store and in a later real-party prompt), and `держится`
(later scenes repeatedly account for the effect without drift). Bare
`implemented`, `working`, `ready`, `реализовано`, `работает`, or `готово` claims
are not evidence. Green CI is necessary, but it is insufficient for
`наблюдается` or `держится`.

The acceptance oracle in
`roles/apps/files/rp-stack/evals/acceptance/manifest.yml` and
`roles/apps/files/rp-stack/evals/acceptance/corpus/**` is independent,
user-owned, and read-only. The runner reads its thresholds from the manifest and
reports `event_precision`, `event_recall`, `character_id_accuracy`,
`empty_scene_false_positive_rate`, `positive_trust_recall`, and
`correction_retention` separately, including per-event-class metrics when the
manifest requires them.

Evidence stays split across three layers: offline uses schemas and saved
responses without providers; provider-canary uses a real prompt and model via
admin-autotest without mutating the source party; production-endurance uses a
long live party and `causal_probe` through later scene consequences. Only
production-endurance can establish `держится`.

Deployment must pause when a revision introduces or materially expands exact
diagnostic prompt/response capture (`service_call_log`, `turn_trace_events`), or
changes retention/redaction, until the user confirms retention and redaction
depth. An accepted ADR for that exact revision is confirmation; a green PR or
configured default is not.

Remote diagnostics pass `%USERPROFILE%\.ssh\id_ed25519_codex_abykovserv` with
`-i` by default when that file exists. `RP_STACK_OPS_IDENTITY_FILE` can select a
different existing private-key file; the key path remains local and is never
stored in the repository. `RP_STACK_OPS_HOST` and `RP_STACK_OPS_SSH` can
override the default target and SSH executable. See
`docs/repository-work-standard.md` for the checked workstation contract.

## Install from this repository

```powershell
codex plugin marketplace add C:\Users\<user>\Documents\Tavern\ubuntu_ansible_palybooks
codex plugin add rp-stack-devkit@tavern-rp-stack
```

Start a new Codex task after installation so the new skill, hooks, and MCP server are loaded.
