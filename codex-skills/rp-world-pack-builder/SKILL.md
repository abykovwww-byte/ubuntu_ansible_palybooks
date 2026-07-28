---
name: rp-world-pack-builder
description: Build or update rp-gateway and Light GUI world packs for roleplay and collaborative-novel parties from natural-language requests. Use for campaign worlds, world information, character notes, player-role seeds, RP or novel prompts, D20 rules, and canonical state seeds. Route deterministic scored learning scenarios to training-world-pack-builder. For live deployment to abykovserv, also use abykovserv-iac-deploy.
---

# RP World Pack Builder

## Scope

Create or update reviewable world-pack source artifacts for `rp` and `novel`
parties. Gateway canonical state is authoritative; prompts and world information
provide context only. Light GUI creates isolated party state from
`state-seed.json`.

Use `training-world-pack-builder` for deterministic, scored, or debrief-driven
learning scenarios. Use `abykovserv-iac-deploy` for GitHub and live server work.

## Boundaries

- Never install or run the stack on Windows.
- Never make durable manual edits under server `/srv` or `/opt` paths.
- Do not hard-code model credentials or model selection into a world pack.
- Do not auto-select the scenario type; the user chooses `rp` or `novel`.
- Preserve player agency and treat canonical state and Gateway outcomes as final.
- Keep raw turns durable and party-scoped. Memory chapters are chronological
  context, not a substitute for state.

## Intake

Ask only for missing essentials, at most three questions at once:

1. World premise and whether it is original, historical, or fandom-based.
2. Starting player role, status, power level, and constraints.
3. Supported scenario types and the recommended type.

Record assumptions in `manifest.json` when the user asks you to proceed without
answers.

## Discover

Before editing, read `references/rp-stack-paths.md` and
`references/world-pack-contract.md`, verify paths with `rg --files`, and inspect
a current world pack as an example.

Default source location:

```text
roles/apps/files/rp-stack/worldpacks/<slug>/
```

## Build

Create:

```text
manifest.json
state-seed.json
campaign-bible.md
prompts/gm-system.md
prompts/authors-note.md
prompts/opening-scene.md
world-info/index.md
characters/index.md
rules/checks.md
quick-replies/notes.md
setup-flow.md
```

The manifest must include `player_role`, `scenario_types.recommended`, and
`scenario_types.supported`. World information should be focused and separate
confirmed facts, rumors, and unresolved mysteries. NPCs need goals,
constraints, relationships, speech style, and secrets.

Prompt rules:

- `rp`: Gateway D20/check outcomes are authoritative; failure cannot become an
  equivalent hidden success.
- `novel`: no dice, checks, difficulty, result labels, or game menus; prioritize
  collaborative prose, character voice, continuity, pacing, and consent.

Prompt order remains scenario contract, world system, author note, episodic
chapters, recent raw turns, relevant characters, dynamic state,
`AUTHORITATIVE_OUTCOME`, and current player action.

## Validate

- Parse every JSON file.
- Confirm the recommended type is supported and only uses known scenario types.
- Validate the seed from the RP Stack source root:

```powershell
python scripts\validate-state.py --state worldpacks\<slug>\state-seed.json --schema state\schema.json
```

- Run Gateway tests when code or behavior changed.
- Scan narrowly for credentials and API-key-looking values.

## Deploy and present

Unless the user requested draft-only, continue through the authoritative IaC
workflow: validate, commit, push, server apply, and runtime verification.

Verify the deployed manifest appears in `/api/worldpacks`, a party can be
created with an explicitly supported scenario type, and its state is isolated.
Report separately whether work is local, pushed, applied, and live-verified.
