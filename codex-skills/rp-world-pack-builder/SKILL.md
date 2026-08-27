---
name: rp-world-pack-builder
description: Build or update SillyTavern/rp-gateway/Light GUI world packs for roleplay parties from natural-language requests. Use when Codex needs to create or modify campaign worlds, lorebooks/world info, character notes, player-role seeds, RP prompts, narrative consequence rules, Quick Reply guidance, or canonical rp-gateway state seeds. Route deterministic scored learning scenarios to training-world-pack-builder. For live deployment to abykovserv / 192.168.1.88, also use the abykovserv-iac-deploy skill.
---

# RP World Pack Builder

## Scope

Create or update reviewable world-pack source artifacts for the local
SillyTavern + `rp-gateway` + Light GUI stack, for `rp` parties.

This skill owns:

- world-pack design and files;
- `state-seed.json` and RP-stack validation;
- SillyTavern lorebook JSON generation;
- RP-specific IaC wiring, such as runtime lorebook copy entries.

This skill does not own the live server deployment procedure. For deploy/apply
steps on `192.168.1.88`, read and follow `abykovserv-iac-deploy`.
It also does not own pure Light GUI layout/frontend changes unless they alter
world-pack files, state seeds/schemas, lorebook contracts, or prompt/state
semantics.

## Non-Negotiables

- `rp-gateway` canonical state is the source of truth. Lorebooks and prompts are memory/context, not authority.
- Light GUI play uses isolated party state copied from the selected world pack's `state-seed.json`. Do not overwrite live `state/current.json` for normal Light GUI play.
- Light GUI narration is party-scoped. Revision 8 uses the narrower history-first prompt contract below; parties pinned to revisions `0..7` keep their compatibility paths. Do not rely on the SillyTavern summarization extension for Light GUI; treat SillyTavern lorebooks as legacy/debug compatibility artifacts.
- Revision 10 may add authored world time and cancelable events, but
  the model may estimate only elapsed time. Event conditions and consequences
  remain reviewed WorldPack data applied by Gateway.
- The runtime target is the Ubuntu server `192.168.1.88`, not Windows.
- Windows is only for Git/IaC editing and local validation. Never install worldpacks, lorebooks, state, or `/srv/...` runtime files on Windows.
- Do not manually copy generated files into `/opt` or `/srv` on the server as a permanent fix. Use the GitHub + Ansible route from `abykovserv-iac-deploy`.
- Do not hard-code a model into a world pack. Model selection belongs to the party/model profile.
- Do not infer or silently select the party scenario type. Light GUI users choose `rp` or `training` explicitly when creating a party.
- Keep world content separate from Gateway mechanics. World prompts may supplement the selected scenario contract but must not re-enable mechanics forbidden by it.
- Route a deterministic, scored, or debrief-driven learning world to `training-world-pack-builder`. Do not create it as a decorative RP pack.
- Keep secrets and provider keys out of all world-pack and IaC files.

## Long-Party Prompt Memory

The revision-7 and older compatibility paths keep four distinct layers:

1. Canonical state: compact current facts and mechanics; it is the only authority.
2. RP living story memory: a bounded, cumulative, party-scoped ledger of canon, rules and abilities, inventory, characters, active/resolved threads, unresolved hooks, current situation, and chronology. It exists only when `scenario_type == "rp"`, never mutates state, and must not be enqueued, injected, exposed, or budgeted for `training`.
3. Episodic compressed history: immutable, chronological, party-scoped `memory_chapters` made from older raw ranges. Preserve player actions, meaningful NPC reactions/dialogue, discoveries, possessions, tone, locations, and unresolved leads. It must not collapse into a state-like list of facts.
4. Recent raw turns: the newest verbatim dialogue needed for immediate continuity.

- Do not use a state summary as a substitute for narrative history.
- When raw turns are summarized, replace the covered raw range with the episodic history; never send both copies in the same narrator prompt.
- Make the episodic-history detail budget configurable. For long-context models, prefer rarer, richer compression over frequent terse summaries.
- Retain every original party turn in durable storage. When a raw turn leaves `effective_party_history_token_budget`, create the next immutable chapter; until chapter creation succeeds, keep the overflow raw turns in the narrator prompt.
- Bound the prompt by selecting detailed chapters and the newest complete raw turn pairs. Archived-turn retrieval is optional, query-specific, non-authoritative, and must never cross a party boundary.
- For RP revision 7, keep every complete non-excluded raw turn after effective `RP_STORY_MEMORY.to_turn_id` in the narrator prompt. This full uncovered raw tail is newer and more authoritative than story memory; do not trim it for a soft percentage target or duplicate it through chapters, retrieval, or fallback blocks.
- Preserve the revision-7 authority order: `AUTHORITATIVE_OUTCOME` and current action -> full uncovered raw tail -> effective `RP_STORY_MEMORY` -> archive. Gateway owns the mandatory `PROMPT_AUTHORITY_HIERARCHY` block; WorldPack prompts must not duplicate or reorder it.
- On revision-7 hard overflow, perform the bounded synchronous story-memory force-refresh, reload coverage, and rebuild before rejecting the turn. Reject before the narrator only if the mandatory prompt still does not fit after refresh.
- Treat revision-7 safe-fallback narrator prose as noncanonical: it is excluded from story memory, chapters, retrieval, and relationship canon, while the player input and stale/as-of scene marker remain available to the next prompt. Do not make fallback prose canonical in WorldPack content.
For the revision-8+ history-first authoring contract:

- Declare `rp_contract.revision: 10` for new RP packs. Raise an existing pack only in an explicit compatible update; do not blanket-migrate existing manifests or parties. `merchant-sviatoslav` is the first revision-10 activation target; other existing packs remain on their declared revisions.
- Keep the complete runtime `WORLD_SYSTEM_PROMPT\n<gm-system.md>` block at or below 5,000 characters and the complete `WORLD_AUTHORS_NOTE\n<authors-note.md>` block at or below 1,500 characters. The literal block name, newline, and authored content all count; do not rely on truncation.
- Keep the complete serialized `PARTY_LORE_CARDS` block at or below 4,000 characters, including its runtime header and instructions. Author compact independent cards because Gateway includes or omits whole cards and never cuts a card to make it fit.
- Preserve the union of a cache-anchored 50-to-57-unit recent window and every eligible unit newer than the safe story-memory coverage. Quantize the recent-window start down to an eight-unit boundary; do not put changing counters, IDs, revisions, or timestamps in narrator/world/absolute rules before RAW. An opening scene counts as its narrator response with the exact `[AUTO_START] Старт партии` player marker suppressed; legacy `turn_kind = null` counts as `narrative`; commands, corrections, and other non-game kinds do not count.
- Preserve the rev8 order: narrator rules, world rules, absolute rules, RAW history, then story memory, whole lore cards, corrections, relationship/world-event pressure, author note, and current player action. Do not move lore cards or author-note content ahead of RAW; they are intentionally volatile and would invalidate the provider prefix.
- Use exactly five independently covered story-memory sections: `situation`, `threads`, `characters`, `assets_and_rules`, and `chronology_and_hooks`. A normal update returns all five in one call; only a section that fails structural validation gets a targeted retry. Empty arrays and `current_situation=null` are valid and must not be retried for being empty. Safe coverage is the minimum coverage across all five sections, so a stale or failed section keeps its uncovered raw tail in the prompt.
- Do not make revision-8 WorldPack prompts depend on scene-state/boundary/reanchor blocks, state summaries or character-state retrieval, archive retrieval/fallback, `LONG_TERM_PARTY_MEMORY`, legacy episodic `memory_chapters`, or journal recaps. State may still drive Gateway outcomes and absolute rules, but these projections are not narrator prompt layers.

For the revision-10 world-clock contract:

- Revision 10 remains the cumulative world-clock boundary for compatible legacy
  packs. Raise an existing pack only in an explicit compatible update; do not
  blanket-migrate packs or parties. Declare `manifest.files.world_clock` only
  when the pack authors a calendar and cancelable events.
- Author `world-clock.json` with schema `rp-gateway.world-clock.v1`, an initial
  timezone-aware date, ISO-8601 `max_step`, typed markers, and at least one
  event. Conditions are only `date_gte`, `after_event`, or `after_confirmed`.
- Give every event at least one authored `superseded_by` marker so a confirmed
  player action can cancel or replace the scheduled outcome. Use only a bounded
  `state_equals` predicate on an allowed canonical path or explicit player
  confirmation; never ask the LLM to infer cancellation from free text.
- Consequences v1 are only a durable `world_fact` or enable/disable of an
  existing authored Lore Card by stable key. Do not author NPC movement,
  presence registries, arbitrary state patches, generated lore, or new runtime
  consequence types.
- Keep event summaries and durable facts short enough for the single 800-char
  `СОБЫТИЯ МИРА` projection. The nearest authored horizon follows relationship
  pressure and precedes the author note/current action; do not duplicate the
  schedule in world prompts.

Across revisions:

- For `training`, omit the RP story layer and its reserve while preserving its existing path. Treat provider cache telemetry as an observed value, not a promise.
- Keep human-facing journal recaps separate from narrator memory.
- Test RP activation, the negative `training` case, covered-turn exclusion, chronological coverage, party/branch isolation, archive retrieval isolation, secret exclusion, context budget, and a manual memory rebuild before deployment.

## Intake

For a new world pack, ask only missing mandatory answers, at most three concise
questions at a time:

1. World name.
2. World premise/source: original, real-world/history, or existing IP/fandom.
3. Starting player character role, status/power level, and constraints.
4. Intended scenario compatibility must be `rp`. For deterministic learning, use `training-world-pack-builder` instead.

Skip answers already provided. For existing IP/fandom worlds, ask whether the
user wants canon-faithful fan setup or an original inspired-by variant. If the
user says to proceed with assumptions, record assumptions in `manifest.json`.
Read `references/intake-questions.md` only when the premise is still too vague.

## Discover

Before writing files:

- Read `references/rp-stack-paths.md` and `references/world-pack-contract.md`.
- Verify actual repo paths with `rg --files`; adapt to discovered paths.
- Prefer existing examples and schemas over invented formats.

Default source location:

```text
ubuntu_ansible_palybooks/roles/apps/files/rp-stack/worldpacks/<slug>/
```

Use lowercase ASCII slugs with letters, digits, and hyphens.

## Build

Create the required artifact set from `references/world-pack-contract.md`:

```text
manifest.json
state-seed.json
campaign-bible.md
prompts/gm-system.md
prompts/authors-note.md
prompts/opening-scene.md
presets/<id>/gm-system.md          # revision 11 only
presets/<id>/authors-note.md       # revision 11 only
prompts/openings/<id>/opening-scene.md  # revision 11 only
prompts/openings/<id>/state-seed.json   # revision 11 only; exact filename
world-info/index.md
sillytavern/<slug-or-title>.json
characters/index.md
rules/checks.md
quick-replies/notes.md
setup-flow.md
relationships/model.json
lore-cards/<group>.json
world-clock.json                 # optional for a revision-10+ authored calendar
rp-supervisor.json               # optional RP narrator-drift observation/enforcement contract
```

`relationships/model.json` is required when `scenario_types.supported`
includes `rp` and optional otherwise. `lore-cards/` is optional, but when used
it must be declared as `manifest.files.lore_cards` and follow
`rp-gateway.worldpack-lore-cards.v1`.

World-pack requirements:

- Put the starting player role in `manifest.player_role`; Light GUI uses it as the default player character description.
- Put `scenario_types.recommended` and `scenario_types.supported` in `manifest.json`. This metadata filters incompatible combinations but never auto-selects the party type.
- For a new pack supporting `rp`, declare `"rp_contract": {"schema_version": "rp-core.v2", "revision": 11}` and author the required presets/openings contract below. This is the maximum contract understood by the pack, not a migration flag: ordinary parties are capped by Gateway's observed revision, and existing packs and parties remain pinned until an explicit compatible update. Do not blanket-migrate them or author a new RP pack against the legacy mechanical v1 contract.
- Revision 10 remains valid for existing compatible packs and does not require
  presets/openings; its declaration remains `"rp_contract": {"schema_version":
  "rp-core.v2", "revision": 10}`. `day-watch-moscow-v2` is the first activated
  revision-11 pack; inventory observes revision 11, while v1 packs and existing
  parties keep their declared/materialized revisions.
- A revision-11 manifest declares non-empty top-level `presets` and `openings`
  plus explicit `"presets_default"` and `"openings_default"`. Preset entries are
  exactly `id`, `title`, `world_system_prompt`, `world_authors_note`; opening
  entries are exactly `id`, `title`, `player_role`, `prompt`, `state_seed`. IDs
  match `^[a-z0-9][a-z0-9_-]{0,63}$`, are unique within their catalog, and are
  the only selection values accepted from clients.
- Every revision-11 opening seed path is exactly
  `prompts/openings/<id>/state-seed.json`. Keep the four legacy root files and
  declarations as byte-equal aliases of the explicit defaults:
  `prompts/gm-system.md`, `prompts/authors-note.md`,
  `prompts/opening-scene.md`, and `state-seed.json`. Root
  `manifest.player_role` equals the default opening role; never infer a default
  from array order.
- Keep every revision-11 opening `player_role` at or below 4,000 normalized
  characters. The draft becomes `PlayerCharacterCreate.description`, whose
  existing 4,000-character limit also governs this authored role.
- Preserve the state overlay order: selected full seed → Gateway metadata/world
  clock → PlayerCharacter (including role and known fact) → explicit starting
  patch → final state-schema validation. A full opening seed removes fragment
  merging; it does not bypass or reorder these overlays.
- In every revision-11 Lore Card whose key starts with `npc:`, `content` has two
  separate non-empty authored lines beginning exactly `Примета:` and
  `Манера речи:`. They are visible character guidance, not hidden storage.
- Treat each preset as a complete prompt pair, not fragments. Including the
  existing headings, `WORLD_SYSTEM_PROMPT` stays within 5,000 characters and
  `WORLD_AUTHORS_NOTE` within 1,500. The authors note must be a usable
  preset-specific brief with required scene forms/elements and explicit
  conflict-resolution prohibitions. Review that meaning as authored prose; do
  not expect a semantic validator, generated content, or silent truncation.
- Add `files.world_clock` only when the revision-10+ pack needs an authored
  calendar and cancelable events. Validate those events against the Lore Card
  library; the file remains optional and existing packs stay pinned.
- Add `files.rp_supervisor` only when the pack deliberately opts into
  `rp-gateway.rp-supervisor.v1`. Start with `mode: observe`: it scores exactly
  six behavior axes over 50 canonical turns every eight turns and never changes
  the narrator prompt. `mode: enforce` additionally requires authored corridors
  and both advisory directions for every rule. This file does not define scene
  location, select a provider/model, or require an RP revision bump.
- Reuse stable location and character IDs from `state-seed.json` throughout the pack. Revision 8 adds no scene manifest field and its narrator does not receive `scene_state`. When maintaining a revision-7 pack, an additional invariant narrative affiliation may use the existing bounded `rp_contract.stable_affiliations` compatibility field; never infer professions, goals, beliefs, emotions, or relationship-model roles into it.
- Do not copy `scene_claims`, `scene_delta`, or the private narrator bundle schema into `gm-system.md` or `authors-note.md`. They belong only to Gateway's revision-7 compatibility path; revisions 8+ request plain narrator text.
- Include focused lorebook entries, not one giant encyclopedia entry.
- For revision-8 Light GUI retrieval, prefer compact reviewed
  `lore-cards/*.json` over repeating the whole world/state in prompts. Use a
  stable ASCII key and non-empty exact keywords for every card. Each NPC card
  uses `npc:<character-id>`, canonical title, every Russian relationship alias,
  private goal, hard boundaries and hidden facts, with `always_on=false`.
  Hidden content is not a trigger: only title/keywords are scanned as whole
  matches over current input plus three complete prior units. Keep each card
  independently useful inside the 4,000-character whole Lore block.
- `scripts/author-worldpack-lore-cards.py` may prepare candidate cards through
  exact OpenRouter `deepseek/deepseek-v4-pro`, but this is author-time only.
  Review every fact against WorldPack source before commit. Never make runtime
  party creation call a model or write generated content back into a WorldPack.
- Separate confirmed facts, rumors, and unresolved mysteries.
- Give NPCs goals, constraints, secrets, and relationships rather than static descriptions.
- Type genuinely invariant `world_constraints` as `kind: absolute` with a
  stable `id`, `source`, and narrow `forbidden_claims` markers when deterministic
  response enforcement is possible. Treat untyped constraints as guidance.
- For every pack whose `scenario_types.supported` includes `rp`, declare the
  `rp-relationships.v2` model in `manifest.json` and author
  `relationships/model.json`. If the declaration is absent, Gateway silently
  leaves the relationship-pressure layer disabled; pack loading does not fail.
- Use
  `roles/apps/files/rp-stack/worldpacks/mechanist-new-world/relationships/model.json`
  as the current executable example. Do not copy that model into this skill.
- Keep the first relationship slice to the `loyalty` axis, `wound` and `role`
  badges, and `crack | ultimatum | plot | strike | favour` boundary events.
  In v2, declare every state character in `characters.<id>.aliases`, keep
  normalized alias forms unique across characters, declare positive clocks for
  every boundary event, and include a monotonic linear `trust_mapping`.
  Mark at least one concrete positive authored event that unambiguously depicts
  voluntary help with `"resolves": ["favour"]`. Do not put this marker on a
  generic positive event such as trust gain, shared risk, or an unrelated kept
  promise: due time alone and positive weight are not proof that the favour was
  delivered.
  Extraction returns `character_mention`, never `character_id`; evidence must
  be a verbatim normalized substring of the current player+narrative turn.
  Do not expose axis values, band labels, or active events in pack-authored
  client surfaces. The narrator invents plot tells; do not author prepared
  tells in the model.
- Include a playable opening scene and immediate hooks.
- Add explicit narrator "do not do" rules: preserve player agency, obey Gateway-authorized outcomes and typed absolute rules, and do not turn costs or setbacks into equivalent hidden victories.

Scenario prompt requirements:

- `rp`: prohibit D20, skills, difficulty, score, success/failure labels, hidden checks, and mechanical `/check`; continue from WorldPack constraints, established facts, Gateway-authorized outcomes, information, resources, NPC goals, relationships, and prior consequences.
- Do not freeze the player in the current location. An explicit non-negated first-person move to a named destination is valid intent; give locations stable IDs and useful authored names/aliases so Gateway and the narrator can resolve it to an existing location.
- For RP relationship seeds, ensure a positive boundary can produce the declared
  `favour` event and a concrete voluntary help scene before its WorldPack clock;
  never expose numeric weights, internal IDs, or due turns in prompts.
- Treat `prompts/gm-system.md` and `prompts/authors-note.md` as active Light GUI runtime inputs, not documentation-only files.

RP-stack wiring for playable worlds:

- Light GUI discovery comes from deployed `/srv/apps/rp-stack/worldpacks/<slug>/manifest.json`; Ansible gets it from the committed `roles/apps/files/rp-stack/worldpacks/<slug>/` source tree.
- SillyTavern does not read `world-info/index.md`. It needs the generated lorebook JSON installed under `/srv/app-data/rp-stack/data/default-user/worlds/`.
- Unless the user explicitly requested draft-only, add a `runtime_source_files` entry in `inventories/local/group_vars/server.yml` for the SillyTavern lorebook:

```yaml
- src: "rp-stack/worldpacks/<slug>/sillytavern/<file>.json"
  path: "{{ rp_stack_data_dir }}/default-user/worlds/<file>.json"
  mode: "0640"
  force: false
```

- If `scripts/install-worldpack.py` exists, you may reference it in `manifest.files.server_installer`, but do not run it on Windows and do not use it for normal Light GUI party state.
- Use legacy single-campaign state install only when the user explicitly asks for the old SillyTavern `/v1/chat/completions` flow. In that case, install on `192.168.1.88` with a SQLite-aware server-side path; never claim copying only `state/current.json` is enough.

## Validate

Before commit/deploy:

- Validate every `.json` file parses.
- Validate `scenario_types.recommended` is `rp` and `scenario_types.supported` contains only `rp`.
- Use the bundled runtime, not `python` from `PATH`:

```powershell
$python = "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
```

- Run the full repository gate from the repository root:

```powershell
powershell.exe -File scripts\ci.ps1
```

- During iteration, run the focused repository and relationship checks:

```powershell
& $python scripts\validate-repository.py
Push-Location roles\apps\files\rp-stack
& $python scripts\validate-relationships.py --worldpacks worldpacks
& $python scripts\validate-state.py --state worldpacks\<slug>\state-seed.json --schema state\schema.json
Pop-Location
```

- For every relationship model, confirm that `character_weights` keys exist in
  the state seed; every state character has at least one unique alias form;
  referenced `role` and `wound` IDs are declared; all five boundary clocks are
  positive; `trust_mapping` is monotonic; bands do not
  overlap and each defines exactly one of `min`/`max`; event weights are in
  `[-30, 15]`; `decay_turns` is `null` or a positive integer;
  at least one positive concrete-help event declares `resolves: ["favour"]`;
  `plot.discovery_chance_per_turn` is in `[0, 1]`; and the first slice declares
  only the `loyalty` axis.
- For revision-10+ packs with `files.world_clock`, validate the closed envelope,
  timezone/date and duration, unique IDs, acyclic `after_event` references,
  known markers, non-empty supersession for every event, allowed consequence
  types, and referenced authored Lore Card keys.
- For packs with `files.rp_supervisor`, validate the closed
  `rp-gateway.rp-supervisor.v1` envelope, the fixed 50/8 window and cadence,
  all six canonical rule IDs, observe/enforce rule shapes, two-advisory limit,
  three-cycle reassertion limit, and 30-day typed-result retention.
- For every revision-11 pack, validate both closed catalogs, explicit defaults,
  stable unique IDs, safe existing paths, exact opening seed convention,
  canonical byte-equal root aliases, root/default `player_role` equality, and
  both complete prompt-block budgets. Run state-schema validation for every
  nested file named `state-seed.json`, not only the root alias.
- Run relevant focused tests when Gateway/app code or IaC behavior changed.
- Run a narrow scan for API-key-looking strings.
- Do not treat Windows-side `/srv` or `\srv` paths as runtime validation.

## Deploy

If the user asks to create a playable world and does not say draft-only, continue
past artifact creation into the deployment chain. Use `abykovserv-iac-deploy`
for the authoritative procedure.

Expected route:

```text
local Git/IaC edit on a codex/ branch or in an isolated worktree -> validate
-> commit -> push the working branch -> open a non-draft PR
-> wait for green CI -> Codex merges the PR into main
-> SSH to abykov@192.168.1.88
-> run the established pull-based Ansible apply there
-> verify runtime files and HTTP endpoints
```

Direct pushes to `main` are prohibited; do not leave merge-ready work on the
working branch.

Deployment verification:

- `/srv/apps/rp-stack/worldpacks/<slug>/manifest.json` exists on `192.168.1.88`.
- `http://192.168.1.88:8010/api/worldpacks` lists the world with `status: playable`.
- `/srv/app-data/rp-stack/data/default-user/worlds/<file>.json` exists for SillyTavern.
- Containers and health checks are acceptable.

If SSH, sudo, network, or approval blocks deployment, stop at the blocker. Report
what has been committed/pushed and the exact server-side action still required.
Do not fall back to manual `/srv` copies or Windows-local installs.

## Present

In the final response:

- Say whether the world is draft-only, pushed, or deployed and visible.
- List the important files and validation results.
- If deployed, include commit hash, server apply status, Light GUI visibility, and SillyTavern lorebook verification.
- If not deployed, do not say it is visible; give the next required server action.

## Quality Bar

A usable pack must include:

- a playable starting situation;
- explicit supported and recommended scenario types;
- clear player role and initial agency;
- 3-7 factions or power centers;
- 5-12 important NPCs with goals, constraints, and relationships;
- 3-8 scene-producing locations;
- hard world constraints the narrator cannot violate;
- valid `state-seed.json`;
- `manifest.player_role`;
- focused SillyTavern lorebook JSON;
- first scene prompt and 3-5 immediate hooks;
- explicit narrator safety/agency rules.

## References

- `references/intake-questions.md`: question bank and minimum viable intake.
- `references/rp-stack-paths.md`: stack paths and validation commands.
- `references/world-pack-contract.md`: required layout and JSON/state conventions.
