# World Pack Contract

Default source folder:

```text
roles/apps/files/rp-stack/worldpacks/<world-slug>/
```

Use lowercase ASCII slugs with letters, digits, and hyphens.

## Required Files

```text
manifest.json
state-seed.json
campaign-bible.md
prompts/gm-system.md
prompts/authors-note.md
prompts/opening-scene.md
world-info/index.md
sillytavern/<world-slug>.json
characters/index.md
rules/checks.md
quick-replies/notes.md
setup-flow.md
relationships/model.json
```

`relationships/model.json` is required when `scenario_types.supported`
includes `rp` and optional otherwise.

## manifest.json

```json
{
  "id": "world-slug",
  "title": "World Title",
  "language": "ru",
  "mode": "original campaign",
  "tone": ["political intrigue", "romance"],
  "status": "playable",
  "premise": "Short player-facing premise used by Light GUI.",
  "player_role": "Starting player role used by Light GUI.",
  "created_for": "sillytavern-rp-gateway",
  "scenario_types": {
    "recommended": "rp",
    "supported": ["rp"]
  },
  "rp_contract": {
    "schema_version": "rp-core.v2",
    "revision": 7
  },
  "relationships": {
    "schema_version": "rp-relationships.v2",
    "model": "relationships/model.json"
  },
  "assumptions": [],
  "files": {
    "campaign_bible": "campaign-bible.md",
    "state_seed": "state-seed.json",
    "gm_system": "prompts/gm-system.md",
    "authors_note": "prompts/authors-note.md",
    "opening_scene": "prompts/opening-scene.md",
    "world_info": "world-info/index.md",
    "sillytavern_lorebook": "sillytavern/world-slug.json",
    "characters": "characters/index.md",
    "checks": "rules/checks.md",
    "quick_replies": "quick-replies/notes.md",
    "setup_flow": "setup-flow.md"
  }
}
```

If `scripts/install-worldpack.py` exists, `manifest.files.server_installer` may
point to `"../../scripts/install-worldpack.py"` as documentation. Do not run it
on Windows and do not use it for normal Light GUI party state.

Light GUI reads `title`, `status`, `premise`, `player_role`, and
`files.state_seed` through `GET /api/worldpacks`.

`scenario_types.recommended` must be `rp`, and `scenario_types.supported` must
contain only `rp`. Route `training` packs to `training-world-pack-builder`. The user still
chooses the party type manually; this metadata does not auto-select it.
Every pack supporting `rp` declares `rp_contract.schema_version=rp-core.v2` and
the highest cumulative `rp_contract.revision` it supports. Current authored RP
packs created by this builder use revision `7`. Gateway still caps ordinary
party creation by the observed runtime revision; the manifest does not activate
unverified behavior. Existing manifests remain pinned and are not blanket-
migrated; raise their revision only after the target runtime revision is
activated and as part of an explicit compatible update. The candidate revision-8
authoring preview below does not authorize changing the literal revision `7`
declaration.

## Revision 7 Authoring Boundary

Revision 7 changes Gateway continuity behavior without requiring another
WorldPack file or mandatory manifest field:

- use stable existing location and character IDs from `state-seed.json` in all
  pack-authored content;
- keep location names and aliases unambiguous. Explicit non-negated first-person
  movement to a named destination may let the narrator select an existing known
  location even when the phrase is not bound to one alias; this never authorizes
  an unknown location;
- do not freeze the player in the opening location when their current action
  explicitly names movement;
- do not put `scene_claims`, `scene_delta`, or the private narrator bundle in
  `gm-system.md` or `authors-note.md`. Gateway injects and validates that schema,
  drops authorized but unanchored operations with a stale marker, repairs hard
  violations once, and owns the atomic state/turn commit;
- Gateway also owns `PROMPT_AUTHORITY_HIERARCHY`, the full uncovered raw tail,
  hard-overflow force-refresh, and noncanonical fallback handling. World prompts
  must not duplicate these blocks or treat fallback narrator prose as canon.

If a world needs a stable narrative affiliation beyond canonical loyalty and
faction, it may add the optional bounded map below. Keys must be known character
IDs; values must be finite non-empty strings, preferably existing faction IDs
with authored aliases. Do not use free-text professions, biography, goals,
beliefs, emotions, or relationship-model roles.

```json
{
  "rp_contract": {
    "schema_version": "rp-core.v2",
    "revision": 7,
    "stable_affiliations": {
      "character-id": "faction-id"
    }
  }
}
```

On a revision-7 fallback turn, `story_memory_canonical=false`: Gateway-authored
narrator prose stays outside story memory, chapters, retrieval, and relationship
canon. The player input and stale/as-of scene boundary remain visible to the
next prompt.

## Candidate Revision 8 Authoring Preview

This section constrains RP content so a pack can fit the candidate prompt shape.
It is not an active manifest contract. Keep new and existing WorldPack manifests
at literal `rp_contract.revision: 7` until Gateway revision 8 is activated and
the pack is explicitly migrated; do not add speculative revision-8 manifest
fields.

### Authored Prompt Budgets

Measure each limit on the complete runtime block, not only on the source file:

- `WORLD_SYSTEM_PROMPT\n<gm-system.md>` is at most 5,000 characters, including
  the literal block name, newline, and all authored content;
- `WORLD_AUTHORS_NOTE\n<authors-note.md>` is at most 1,500 characters under the
  same counting rule;
- the serialized `PARTY_LORE_CARDS` block is at most 4,000 characters including
  its runtime header, instructions, and selected cards.

Gateway includes lore as whole cards. It may omit a card that would cross the
4,000-character total, but it must not cut card content to fit. Author compact,
independent cards and do not depend on a large card being partially injected.
These are hard authoring limits; runtime truncation is not a content strategy.

### Narrator Continuity Inputs

The candidate revision-8 narrator context keeps the union of:

- a recent raw window whose start is quantized to an eight-unit boundary and
  therefore contains 50 to 57 eligible units; and
- every eligible raw unit newer than safe story-memory coverage.

The cache-stable order is narrator rules, world rules, absolute rules, then RAW
history. Story memory, whole lore cards, corrections, relationship or world-event
pressure, author note, and current player action follow as a volatile tail. Do
not place changing IDs, turn numbers, revisions, timestamps, or counters in the
three rule blocks before RAW, and do not author a pack that requires lore cards
or the author note to precede the transcript.

An `opening_scene` counts as one unit through the narrator response; the exact
`[AUTO_START] Старт партии` player marker is suppressed. A legacy turn with
`turn_kind = null` counts as `narrative`. `world_command`, `gm_correction`, and
future non-game kinds do not count. Safe coverage is the minimum coverage of
these five independently covered memory sections:

1. `situation`
2. `threads`
3. `characters`
4. `assets_and_rules`
5. `chronology_and_hooks`

A normal memory update requests all five sections in one OpenRouter call. The
Gateway retries only a section that is absent, cannot be parsed, violates the
section schema, changes an existing `fact_id`, or arrives with
`finish_reason=length`. Validation is structural, not semantic: empty arrays and
`current_situation=null` are valid and must not be retried merely to produce
content.

A stale or failed section therefore keeps the affected raw history uncovered;
another section's newer coverage must not hide it.

The candidate narrator prompt has no scene-state, scene-boundary, or reanchor
layer; no state summary or retrieved character-state layer; no archive retrieval
or uncompacted-archive fallback; and no `LONG_TERM_PARTY_MEMORY`, legacy
episodic `memory_chapters`, or journal-recap layer. Durable state may still drive
Gateway outcomes and absolute rules, and durable history remains stored, but
WorldPack prompts must not reference those removed projections as if the
narrator could see them.

## RP Relationship Model

When `scenario_types.supported` includes `rp`, `manifest.relationships` and
`relationships/model.json` are required. For packs without RP support they are
optional. If the manifest declaration is absent, Gateway does not report an
error: it silently leaves the relationship-pressure layer disabled.

Use
`roles/apps/files/rp-stack/worldpacks/mechanist-new-world/relationships/model.json`
as the executable example instead of embedding a second model copy here. The
first slice supports only the `loyalty` axis, `wound` and `role` badges, and the
boundary events `crack | ultimatum | plot | strike | favour`. In v2 the model
also declares `characters.<id>.aliases` for every state character, positive
clocks for all five boundary events, and a monotonic linear `trust_mapping`.
Extraction returns `character_mention` plus exact turn evidence; Gateway resolves
the mention to the internal character ID. The layer has no client surface and
must not expose axis values, band labels, or active events.
Plot tells are invented by the narrator and must not be authored as model
fields.

At least one positive event must represent a concrete voluntary-help act and
declare `"resolves": ["favour"]`. Gateway closes a due favour only when that
marked event is extracted verbatim from the committed narrator scene for the
same character and turn. Do not mark generic positive causes (`trust_gained`,
`shared_risk`, an unrelated `kept_promise`): they may change relationship
pressure but do not prove that the specific favour was delivered.

The validator enforces these authoring constraints:

- every `character_weights` key exists in `state-seed.json` `characters`;
- every state character has at least one non-empty alias form, and normalized
  alias forms are unique across characters;
- every referenced `role` exists in `roles`, and every event `wound` exists in
  `wounds`;
- band boundaries do not overlap, and every band defines exactly one of
  `min`/`max`;
- event `weight` is in `[-30, 15]`, and `decay_turns` is `null` or a positive
  integer;
- at least one event with positive `weight` declares `resolves: ["favour"]`;
- `plot.discovery_chance_per_turn` is in `[0, 1]`;
- every boundary clock (`crack`, `ultimatum`, `plot`, `favour`, `strike`) is a
  positive integer;
- `trust_mapping` is a monotonic linear map with increasing integer ranges;
- only the `loyalty` axis is declared in the first slice.

## state-seed.json

The seed must match the RP gateway state schema:

```json
{
  "meta": {
    "campaign_id": "world-slug",
    "schema_version": "1.0.0",
    "state_version": 1,
    "turn": 0,
    "last_updated": "1970-01-01T00:00:00Z"
  },
  "player": {
    "location": "start-location",
    "status": "active",
    "reputation": {},
    "resources": {},
    "known_abilities": [],
    "constraints": [],
    "known_world_facts": []
  },
  "characters": {},
  "factions": {},
  "locations": {},
  "resources": {},
  "relationships": {},
  "active_threads": [],
  "completed_threads": [],
  "world_constraints": [],
  "timeline": [],
  "last_turn": {
    "turn": 0,
    "player_message": "",
    "narrator_response": "",
    "state_patch_id": ""
  },
  "uncertain_facts": []
}
```

This `state-seed.json` `relationships` object is the canonical-state collection
of NPC-to-NPC links (`from`, `to`, `trust`, and `suspicion`) defined by
`state/schema.json`. It is unrelated to `manifest.relationships`, which points
to the ADR 020 relationship-pressure model. The pressure layer stores its own
causes, bands, badges, and narrative events and never writes them into canonical
state.

When a Light GUI party is created, the gateway copies this seed into isolated
party state under `/srv/app-data/rp-stack/state/parties/<party_id>/current.json`
and rewrites `meta.campaign_id`. Do not overwrite global `state/current.json`
for normal Light GUI worlds.

For `rp-core.v2`, declare a hard rule as an object in `world_constraints` with
`kind: "absolute"`, a stable `id`, `source`, and narrowly phrased
`forbidden_claims` when a deterministic post-response contradiction check is
possible. An untyped constraint is legacy guidance and is still prompt context,
but it is not a claim of machine-enforced authority.

## Lorebook JSON

`world-info/index.md` is for review. SillyTavern needs a runtime lorebook JSON
under `sillytavern/`.

Use focused entries:

```json
{
  "name": "World Title",
  "entries": {
    "0": {
      "uid": 0,
      "key": ["keyword"],
      "keysecondary": [],
      "comment": "Entry title",
      "content": "Focused lore content.",
      "constant": false,
      "selective": true,
      "selectiveLogic": 0,
      "order": 100,
      "position": 1,
      "disable": false,
      "probability": 100,
      "useProbability": true,
      "depth": 4
    }
  }
}
```

Recommended entry categories: overview, player role, factions, locations, NPCs,
rumors, hard constraints, and current campaign state summary.

## IaC Wiring

For playable Git/IaC worlds:

- The committed world-pack folder is copied by Ansible into `/srv/apps/rp-stack/worldpacks/<slug>/`; this makes Light GUI discover `manifest.json`.
- Add a `runtime_source_files` entry in `inventories/local/group_vars/server.yml` so the SillyTavern lorebook is copied into `/srv/app-data/rp-stack/data/default-user/worlds/`.
- Use `force: false` by default to preserve user-owned runtime edits.
- Do not manually copy files into `/srv` or `/opt` from Windows.
- Use `abykovserv-iac-deploy` for commit, working-branch push, non-draft PR,
  green-CI merge, server apply, and verification. Direct pushes to `main` are
  prohibited.

Template:

```yaml
- src: "rp-stack/worldpacks/<slug>/sillytavern/<file>.json"
  path: "{{ rp_stack_data_dir }}/default-user/worlds/<file>.json"
  mode: "0640"
  force: false
```

Manual browser import is only a fallback when the JSON is already on the same
device that opened the browser. It is not the normal path for abykovserv.

## Legacy State Install

Only use legacy global state install when the user explicitly asks to install a
world into the old SillyTavern `/v1/chat/completions` flow.

For that legacy path, install on `192.168.1.88` with a SQLite-aware procedure:
back up `state/current.json` and `/srv/app-data/rp-stack/gateway/rp_gateway.db`,
then use a server-side helper or gateway endpoint that keeps SQLite state
versions and `state/current.json` consistent. Never tell the user to copy only
`state/current.json` if the database already exists.

## Character Cards

For each major NPC, include:

```text
id
display name
role
public face
private goal
attitude to player
speech style
hard constraints
secrets
starting relationship
```

## Prompt Rules

Every `gm-system.md` should say:

- preserve player agency;
- do not decide player thoughts, feelings, choices, or consent;
- obey Gateway-authorized outcomes and injected absolute rules;
- keep RP narration free of hidden checks and mechanical outcomes;
- keep lore consistent with established facts and newer verbatim history.

Existing revision-7 prompts may reference `<AUTHORITATIVE_WORLD_STATE>` because
that is an observed runtime layer. Candidate-revision-8-compatible prompt text
must not add or depend on that tag; use the Gateway-authority wording above.

Add the applicable mode contract:

- `rp`: no D20, skills, difficulty, score, success/failure labels, hidden checks, or mechanical `/check`; consequences follow from the world, state, resources, information, NPC goals, relationships, and prior events.

## Quick Reply Notes

Do not rewrite global Quick Reply presets while creating draft/source packs.
Document suggested buttons in `quick-replies/notes.md`, such as:

```text
Мир
Показать мир
Намерение
Слухи
Журнал
Отложить решение
```
