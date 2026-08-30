# World And Scenario Authoring Contract

## Status And Authority

This is the Decision 043 authoring contract for the new offline RP core. It
currently supports exactly one World: `day-watch-moscow-v2`.

The production Pydantic models and loader in
`roles/apps/files/rp-stack/rp-gateway/app/rp/content.py` are the only executable
truth. This document is author guidance, not a second schema. Do not add marker,
regex, or prose checks for this format to `scripts/validate-repository.py`.

The new definitions are not yet consumed by Light GUI, its API, or the deployed
Gateway. A local test, commit, PR, or merge does not make them deployed, visible,
or playable.

## Ownership Boundary

World owns reusable setting truth:

- laws and setting rules;
- factions and places through referenced canon material;
- the base NPC catalog;
- canon and seed lore;
- relationship ontology.

Scenario owns one starting configuration inside a World:

- player role and abilities expressed in the full starting state;
- style, output format, optional difficulty, and detail level;
- opening and complete initial state;
- active NPC selection and starting relationships;
- bounded local deviations from World defaults.

World must not own `player_role`, openings, presets, a state seed, or
narrator-supervision config. The closed production model rejects all undeclared
fields.

## Source Layout

```text
roles/apps/files/rp-stack/worldpacks/day-watch-moscow-v2/
  world.json
  scenario-presets/
    <scenario-id>.json
  campaign-bible.md
  world-info/index.md
  characters/index.md
  rules/checks.md
  relationships/model.json
  lore-cards/core.json
  scenario-experience/<style>-system.md
  scenario-experience/<style>-note.md
  prompts/openings/<start>/opening-scene.md
  prompts/openings/<start>/state-seed.json
```

Definitions reference existing committed assets. All paths are relative to the
World root, use forward slashes, stay inside that root, and resolve to an
existing file. Absolute paths, backslashes, `..`, missing files, directories in
place of files, and symlink escapes fail closed.

Existing `manifest.json` and root aliases can coexist until later cutover, but
the Decision 043 loader never reads them. They are not a fallback for missing or
invalid new definitions. The still-active legacy runtime also reads
`presets/**`; Decision 043 Scenario narrator assets live under
`scenario-experience/` so offline authoring does not change that runtime.

## `world.json`

The definition uses the exact closed `WorldDefinition` shape:

```json
{
  "schema_version": "rp-world.v1",
  "id": "day-watch-moscow-v2",
  "title": "Дневной Дозор: Москва — четыре начала",
  "language": "ru",
  "premise": "Книжная Москва в начале событий «Дневного Дозора»: Светлые и Тёмные Иные живут под Великим Договором, Ночной и Дневной Дозоры контролируют противоположные стороны, а Инквизиция сохраняет равновесие.",
  "canon_files": [
    "campaign-bible.md",
    "world-info/index.md"
  ],
  "setting_rules_file": "rules/checks.md",
  "characters_file": "characters/index.md",
  "relationship_ontology_file": "relationships/model.json",
  "lore_card_files": [
    "lore-cards/core.json"
  ]
}
```

Rules:

- `schema_version` is exactly `rp-world.v1`.
- `id` is exactly `day-watch-moscow-v2` and equals the directory name.
- `title`, `language`, and `premise` contain non-whitespace text.
- `canon_files` and `lore_card_files` are non-empty.
- Every referenced source path is unique across the whole definition.
- Referenced text is non-empty; referenced JSON is an object.
- Do not add a provider, model, player, opening, preset, state, or runtime party
  field.

Materialization resolves the references into `WorldSnapshot` with schema
`rp-world-snapshot.v1`. Host paths and source formatting do not enter the
snapshot hash.

## `scenario-presets/<scenario-id>.json`

Each preset uses the exact closed `ScenarioPresetDefinition` shape. This is an
illustrative complete definition; authored values must match the committed
source:

```json
{
  "schema_version": "rp-scenario-preset.v1",
  "id": "book-independent",
  "title": "Книжный: Независимый старт",
  "world_id": "day-watch-moscow-v2",
  "player_role": "Независимый зарегистрированный Иной. Имя, сторона, уровень силы, способности, биография, связи и личные ограничения задаются в карточке персонажа.",
  "style": "book",
  "format": "plain_scene_text",
  "difficulty": null,
  "detail_level": "default",
  "world_system_prompt_file": "scenario-experience/book-system.md",
  "world_authors_note_file": "scenario-experience/book-note.md",
  "opening_file": "prompts/openings/independent/opening-scene.md",
  "initial_state_file": "prompts/openings/independent/state-seed.json",
  "active_character_ids": [
    "zabulon",
    "gesser",
    "alisa-donnikova",
    "edgar",
    "anton-gorodetsky",
    "svetlana-nazarova",
    "igor-teplov",
    "olga",
    "tiger-cub",
    "maxim",
    "anna-tikhonovna"
  ],
  "local_overrides": {}
}
```

Rules:

- `schema_version` is exactly `rp-scenario-preset.v1`.
- The filename stem equals `id`; IDs are stable lowercase ASCII identifiers.
- `world_id` is exactly `day-watch-moscow-v2`.
- `player_role`, `style`, `format`, and `detail_level` are explicit non-empty
  values. `difficulty` is either non-empty text or `null`; keep it `null` when
  the World authors no difficulty semantics instead of inventing mechanics.
- Prompt, note, opening, and initial-state paths resolve through the same safe
  World-root boundary as `world.json`.
- `active_character_ids` is non-empty, unique, and every ID exists in
  `initial_state.characters`.
- The initial state contains object-valued `player`, `characters`, `factions`,
  `locations`, and `relationships`.
- Materialized `starting_relationships` equals
  `initial_state.relationships` exactly.
- `local_overrides` is an object and contains only deliberate Scenario-local
  deviations. Do not copy the whole World into it.

The committed source preserves all twelve combinations of these dimensions:

- styles: `book`, `action`, `strategic`;
- starts: `independent`, `night-trainee`, `day-witch`,
  `inquisition-observer`.

A preset is complete. It is not a fragment to combine with another preset.

## Free Scenarios

A free Scenario is runtime input, not another authored file catalog. It must
materialize through `WorldScenarioLoader.materialize_free_scenario()` into the
same `ScenarioSnapshot` shape as a preset. It must obey the same state,
relationship, active-character, and World-ID invariants.

Do not add a scenarios database table or a second definition schema for free
input.

## Immutable Party Snapshots

At party creation, materialize and persist independently:

- canonical World snapshot JSON and SHA-256 hash;
- canonical Scenario snapshot JSON and SHA-256 hash.

Both snapshots are immutable party source. Later edits to `world.json`, preset
definitions, or referenced assets affect only future parties. Reopening an
existing party must reconstruct its World and Scenario from stored snapshots,
not from current source files.

Two parties may share the same World hash and have different Scenario hashes.
Reusing a party ID with different source snapshots must fail closed. Updates to
the four snapshot columns must be rejected by SQLite.

## Validation Evidence

From the Gateway root, use the bundled Python and run:

```powershell
& $python -m pytest -q tests\test_rp_world_scenario.py tests\test_rp_turn_engine.py
```

The focused tests are expected to exercise the committed source through the
production loader/schema, including:

- one supported World and twelve preset definitions;
- all referenced assets and closed-model rejection;
- directory/filename identity and World-ID matching;
- unsafe path rejection;
- state, active-character, and relationship consistency;
- preset and free Scenario materialization;
- stable canonical hashes and immutable persisted snapshots;
- source edits not mutating an existing party.

From the repository root, also run:

```powershell
& $python scripts\validate-repository.py
```

This second command covers other repository contracts only. It intentionally
does not mirror or validate World/Scenario JSON semantics. Do not treat it as
proof that the new source loads.

Run full local CI when the shared contract changes or before merge. Offline
tests and CI are not live runtime proof.

## Merge And Runtime Boundaries

Source delivery follows:

```text
isolated worktree on a codex/ branch -> validation -> commit -> push branch
-> open a non-draft PR -> wait for green CI -> Codex performs the merge into main
```

Direct pushes to `main` are prohibited.

For the current slice, a verified merge is the terminal delivery state. The new
format is still not deployed or visible, so do not run server apply and do not
claim a Light GUI UX change.

A later explicitly authorized integration/cutover must use
`abykovserv-iac-deploy`, then separately prove the applied commit, healthy
containers, HTTP discovery, actual party creation, stored snapshots, and a real
turn. Only that evidence can establish live functionality.
