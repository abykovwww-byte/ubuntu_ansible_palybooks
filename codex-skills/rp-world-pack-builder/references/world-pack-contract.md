# World Pack Contract

Use a lowercase ASCII slug under:

```text
roles/apps/files/rp-stack/worldpacks/<world-slug>/
```

## Required files

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

## Manifest

```json
{
  "id": "world-slug",
  "title": "World Title",
  "language": "ru",
  "mode": "original campaign",
  "tone": ["political intrigue"],
  "status": "playable",
  "premise": "Short player-facing premise.",
  "player_role": "Starting player role.",
  "created_for": "rp-gateway-light-gui",
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
    "characters": "characters/index.md",
    "checks": "rules/checks.md",
    "quick_replies": "quick-replies/notes.md",
    "setup_flow": "setup-flow.md"
  }
}
```

The recommended scenario type must be in the supported list. Light GUI reads
the manifest and Gateway copies `state-seed.json` into isolated party state.

## State and context

The seed must match `state/schema.json`. Put compact current facts and mechanics
in canonical state. Put reviewable setting context in `world-info/index.md`.
Neither world information nor memory may override canonical state or a Gateway
outcome.

Each major NPC should include an id, display name, role, public face, private
goal, attitude to player, speech style, hard constraints, secrets, and starting
relationship.

## Prompt rules

Every system prompt must preserve player agency, obey authoritative state and
Gateway outcomes, apply mechanics only in compatible scenario types, and keep
lore consistent with confirmed state.
