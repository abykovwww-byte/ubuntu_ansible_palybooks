---
name: rp-world-pack-builder
description: Build or update the Decision 043 World and Scenario authoring source for RP parties. Use when Codex needs to modify world canon, characters, relationship ontology, seed lore, scenario presets, player roles, openings, initial state, narrator style, format, difficulty, or local scenario overrides. Route deterministic scored learning scenarios to training-world-pack-builder. For a later authorized live deployment to abykovserv / 192.168.1.88, also use abykovserv-iac-deploy.
---

# RP World And Scenario Builder

## Current Boundary

Author the reviewable `WorldDefinition` and `ScenarioPresetDefinition` source
introduced by Decision 043. The currently supported executable World is exactly
`day-watch-moscow-v2`.

The new source is offline-only in the current slice. Light GUI, its API, and the
deployed Gateway do not discover or select it yet. Creating or merging these
files does not make the new World/Scenario format deployed, visible, or
playable. Do not apply this slice to the server as proof of runtime support.

The production loader and Pydantic schema in
`roles/apps/files/rp-stack/rp-gateway/app/rp/content.py` are the only executable
truth for this authoring format. This skill explains how to use that contract;
it must not duplicate it as another schema or validator.

This skill owns:

- `world.json` and `scenario-presets/*.json` authoring;
- referenced canon, character, relationship, lore, prompt, opening, and state
  assets;
- focused source/materialization validation;
- a clear report of source, merge, deployment, and runtime visibility states.

This skill does not own:

- production loader/schema behavior;
- party persistence or turn execution;
- Light GUI/API integration or live cutover;
- server deployment procedure;
- deterministic scored training content.

Use `training-world-pack-builder` for scored training. Use
`abykovserv-iac-deploy` only when a later task explicitly integrates and deploys
the new format.

## Non-Negotiables

- Read `app/rp/content.py` before editing authoring files. If this prose and the
  production model differ, the production model wins and this skill must be
  corrected in the same change.
- Support only `day-watch-moscow-v2` until the production loader explicitly
  broadens that boundary. Do not present a second World as executable.
- Keep World and Scenario ownership separate. `WorldDefinition` must not declare
  a player role, openings, presets, a state seed, or narrator-supervision config.
- A Scenario owns the starting player, narrator style and format, optional
  difficulty, detail level, opening, initial state, active NPC selection, starting
  relationships, and bounded local deviations.
- Reference committed assets by safe forward-slash paths relative to the World
  root. Never use absolute paths, backslashes, `..`, missing files, or symlinks
  that escape the World root.
- Keep stable World, Scenario, character, faction, and location IDs. Never infer
  IDs from display labels at runtime.
- Do not hard-code provider or model selection into World or Scenario source.
- Keep secrets and provider keys out of all authored files.
- Do not write directly to party SQLite from the builder. Party creation owns
  immutable materialized World and Scenario snapshots and their hashes.
- A preset Scenario and a free Scenario materialize to the same
  `ScenarioSnapshot` contract. Do not introduce a separate runtime scenario
  registry or table.
- Existing `manifest.json` and legacy root aliases may remain in the pack until
  cutover, but they are not inputs to the Decision 043 loader. Do not edit them
  as a substitute for `world.json` or `scenario-presets/*.json`.
- Do not add marker or prose checks for this format to
  `scripts/validate-repository.py`. Executable validation belongs to the
  production loader/schema and focused tests.

## Intake

For the current slice, update only the already-approved
`day-watch-moscow-v2` World. Ask at most three missing questions at a time:

1. Which canon, NPC, faction, place, or relationship fact changes in the World?
2. Which preset dimension changes: player start, style, format, difficulty,
   detail, opening, initial state, active NPCs, or local deviation?
3. Must existing source combinations remain unchanged?

If the user asks for a different executable World, gather the brief but stop
before writing the new format: the current production loader rejects it. Do not
work around that guard through a legacy manifest.

Read `references/intake-questions.md` only when the content request remains
ambiguous.

## Discover

Before editing:

1. Read `references/rp-stack-paths.md` and
   `references/world-pack-contract.md`.
2. Read the production definitions and loader in `app/rp/content.py`.
3. Inspect the committed `day-watch-moscow-v2` definitions and referenced
   assets.
4. Verify paths with `rg --files`; never assume the checkout layout.
5. Preserve unrelated tracked and untracked work. Use an isolated worktree for
   implementation.

## Author

The new authored surface is:

```text
worldpacks/day-watch-moscow-v2/
  world.json
  scenario-presets/<scenario-id>.json
  campaign-bible.md
  world-info/index.md
  characters/index.md
  rules/checks.md
  relationships/model.json
  lore-cards/*.json
  scenario-experience/<style>-system.md
  scenario-experience/<style>-note.md
  prompts/openings/<start>/opening-scene.md
  prompts/openings/<start>/state-seed.json
```

`world.json` owns only reusable World material:

- stable ID, title, language, and premise;
- one or more canon files;
- setting rules;
- base character catalog;
- relationship ontology;
- one or more seed lore-card files.

Each `scenario-presets/<scenario-id>.json` owns one complete authored starting
configuration:

- stable ID, title, and matching World ID;
- complete player-role text;
- style, output format, nullable difficulty, and detail level;
- complete narrator-system and author-note asset references;
- one opening and one full initial-state reference;
- a non-empty, unique list of active character IDs;
- bounded `local_overrides` when a Scenario deliberately deviates from World
  defaults.

New Scenario narrator assets live under `scenario-experience/`. The legacy
`presets/**` assets remain inputs of the still-active manifest runtime until
cutover and must not be edited as the Decision 043 Scenario source.

The preset filename stem must equal its ID. Preserve the committed cross-product
of the three authored styles and four authored starts for
`day-watch-moscow-v2`; do not silently drop a combination. A preset is complete,
not a fragment to merge with another preset.

All definition objects are closed. Add a field only by changing the production
model as part of an explicitly authorized architecture slice, not by relying on
ignored JSON properties.

## Content Rules

- Keep canon, rumors, and unresolved mysteries distinguishable.
- Give NPCs goals, constraints, secrets, and relationships, not just physical
  descriptions.
- Keep narrator rules and notes complete for each style; do not depend on a
  hidden legacy alias.
- Preserve player agency and established World facts. A Scenario may narrow a
  start but must not silently rewrite the reusable World canon.
- Keep `initial_state.characters`, `player`, `factions`, `locations`, and
  `relationships` as objects.
- Keep `difficulty` as `null` when the World does not author difficulty
  semantics; do not invent a mechanical label merely to fill the field.
- Every `active_character_ids` value must exist in
  `initial_state.characters`.
- Materialized `starting_relationships` must exactly match
  `initial_state.relationships`.
- Reuse the existing full initial-state files for the four approved starts.
  Do not invent fragment-merge behavior.
- Preserve the transferred three narrator styles and four starts from the
  approved `day-watch-moscow-v2` content unless the user explicitly changes the
  product requirement.

## Validate

Use the bundled Python runtime, not an arbitrary `python` from `PATH`:

```powershell
$python = "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
```

Run the executable World/Scenario and snapshot tests from the Gateway root:

```powershell
Set-Location roles\apps\files\rp-stack\rp-gateway
& $python -m pytest -q tests\test_rp_world_scenario.py tests\test_rp_turn_engine.py
```

Run the repository validator for the remaining repository-wide contracts:

```powershell
Set-Location <repository-root>
& $python scripts\validate-repository.py
```

That repository validator intentionally does not validate the new authoring
format. Do not interpret its success as World/Scenario proof. The focused tests
must load the committed definitions through `WorldScenarioLoader`, materialize
World and Scenario snapshots, and exercise immutable party persistence.

When relevant, also verify:

- exactly one supported World and the complete committed preset set load;
- definition IDs match their directory or filename identity;
- unsafe or missing asset paths fail closed;
- unknown active character IDs and mismatched starting relationships fail;
- two Scenarios can share one World hash while retaining different Scenario
  hashes;
- editing source after party creation does not mutate stored snapshots;
- a free Scenario and a preset Scenario use the same snapshot contract;
- existing party snapshot columns cannot be updated.

Run the full local CI gate for a shared contract change or before merge. Test
success proves only source and offline behavior; it is not server apply or live
UX proof.

## Merge Policy

Use this repository route:

```text
isolated worktree on a codex/ branch -> validate -> commit -> push branch
-> open a non-draft PR -> wait for green CI -> Codex performs the merge into main
```

Direct pushes to `main` are prohibited. A pushed branch is not a terminal state
when the change is merge-ready.

For the current offline slice, stop after the verified merge. Do not run Ansible
apply and do not claim Light GUI visibility. A later authorized integration and
cutover must follow `abykovserv-iac-deploy`, then verify the applied revision,
containers, HTTP surface, actual party creation, and turn execution.

## Present

Report the result as separate states:

- authored source and functional consequence;
- focused loader/materialization evidence;
- commit, PR, green-CI, and merge state;
- deployment/apply state;
- Light GUI/API visibility and real party-play state.

For this slice, say explicitly that the new format is not deployed or visible.
Do not call a repository validator, unit test, PR, or merge a live UX check.

## References

- `references/intake-questions.md`: bounded questions for the supported World.
- `references/rp-stack-paths.md`: source, loader, test, and runtime paths.
- `references/world-pack-contract.md`: exact authoring boundary and examples.
