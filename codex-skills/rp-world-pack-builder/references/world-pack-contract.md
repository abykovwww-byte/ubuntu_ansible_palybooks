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
```

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
    "recommended": "novel",
    "supported": ["novel", "rp"]
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

`scenario_types.recommended` and every item in `scenario_types.supported` must
be one of `rp`, `novel`, or `training`. The recommended value must also be in
the supported list. The user still chooses the party type manually; this
metadata does not auto-select it.

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

When a Light GUI party is created, the gateway copies this seed into isolated
party state under `/srv/app-data/rp-stack/state/parties/<party_id>/current.json`
and rewrites `meta.campaign_id`. Do not overwrite global `state/current.json`
for normal Light GUI worlds.

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
- Use `abykovserv-iac-deploy` for commit/push/server apply and verification.

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
- obey `<AUTHORITATIVE_WORLD_STATE>` and gateway outcomes;
- keep mechanics conditional on the selected party type;
- keep lore consistent with canonical state.

Add the applicable mode contract:

- `rp`: D20 and Gateway outcomes are authoritative; failed checks cannot become hidden success.
- `novel`: no dice, skills, checks, difficulty, result labels, or action menus; prioritize prose, relationships, pacing, and consent.
- `training`: no randomness or `/check`; advance one authored turn, enforce templates and scoring, and withhold hints or assessment until the scheduled debrief.

For `training`, `rules/checks.md` remains the required contract filename but
contains deterministic resolution and scoring rules rather than check commands.

## Quick Reply Notes

Do not rewrite global Quick Reply presets while creating draft/source packs.
Document suggested buttons in `quick-replies/notes.md`, such as:

```text
Мир
Показать мир
Проверка
Слухи
Журнал
Отложить решение
```
