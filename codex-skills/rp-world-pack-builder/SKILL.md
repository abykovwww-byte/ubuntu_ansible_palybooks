---
name: rp-world-pack-builder
description: Build or update SillyTavern/rp-gateway/Light GUI world packs for roleplay and collaborative-novel parties from natural-language requests. Use when Codex needs to create or modify campaign worlds, lorebooks/world info, character notes, player-role seeds, RP/novel prompts, D20 resolution rules, Quick Reply guidance, or canonical rp-gateway state seeds. Route deterministic scored learning scenarios to training-world-pack-builder. For live deployment to abykovserv / 192.168.1.88, also use the abykovserv-iac-deploy skill.
---

# RP World Pack Builder

## Scope

Create or update reviewable world-pack source artifacts for the local
SillyTavern + `rp-gateway` + Light GUI stack, for `rp` and `novel` parties.

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
- Light GUI narration uses party-scoped `WorldPack + PlayerCharacter + ModelProfile + State + TurnHistory + gateway memory/journal summaries`. Do not rely on the SillyTavern summarization extension for Light GUI; treat SillyTavern lorebooks as legacy/debug compatibility artifacts.
- The runtime target is the Ubuntu server `192.168.1.88`, not Windows.
- Windows is only for Git/IaC editing and local validation. Never install worldpacks, lorebooks, state, or `/srv/...` runtime files on Windows.
- Do not manually copy generated files into `/opt` or `/srv` on the server as a permanent fix. Use the GitHub + Ansible route from `abykovserv-iac-deploy`.
- Do not hard-code a model into a world pack. Model selection belongs to the party/model profile.
- Do not infer or silently select the party scenario type. Light GUI users choose `rp`, `novel`, or `training` explicitly when creating a party.
- Keep world content separate from Gateway mechanics. World prompts may supplement the selected scenario contract but must not re-enable mechanics forbidden by it.
- Route a deterministic, scored, or debrief-driven learning world to `training-world-pack-builder`. Do not create it as a decorative RP pack.
- Keep secrets and provider keys out of all world-pack and IaC files.

## Long-Party Prompt Memory

When changing Light GUI/Gateway memory or prompt assembly, keep four distinct layers:

1. Canonical state: compact current facts and mechanics; it is the only authority.
2. RP living story memory: a bounded, cumulative, party-scoped ledger of canon, rules and abilities, inventory, characters, active/resolved threads, unresolved hooks, current situation, and chronology. It exists only when `scenario_type == "rp"`, never mutates state, and must not be enqueued, injected, exposed, or budgeted for `novel` or `training`.
3. Episodic compressed history: immutable, chronological, party-scoped `memory_chapters` made from older raw ranges. Preserve player actions, meaningful NPC reactions/dialogue, discoveries, possessions, tone, locations, and unresolved leads. It must not collapse into a state-like list of facts.
4. Recent raw turns: the newest verbatim dialogue needed for immediate continuity.

- Do not use a state summary as a substitute for narrative history.
- When raw turns are summarized, replace the covered raw range with the episodic history; never send both copies in the same narrator prompt.
- Make the episodic-history detail budget configurable. For long-context models, prefer rarer, richer compression over frequent terse summaries.
- Retain every original party turn in durable storage. When a raw turn leaves `effective_party_history_token_budget`, create the next immutable chapter; until chapter creation succeeds, keep the overflow raw turns in the narrator prompt.
- Bound the prompt by selecting detailed chapters and the newest complete raw turn pairs. Archived-turn retrieval is optional, query-specific, non-authoritative, and must never cross a party boundary.
- Preserve RP runtime order: scenario contract -> world system -> author's note -> `RP_STORY_MEMORY` -> episodic chapters -> lore/fallback/raw/retrieval -> relevant characters -> dynamic state -> `AUTHORITATIVE_OUTCOME` -> current player action. For `novel` and `training`, omit the RP story layer and its reserve while preserving the existing path. Treat provider cache telemetry as an observed value, not a promise.
- Keep human-facing journal recaps separate from narrator memory.
- Test RP activation, negative `training` and `novel` cases, covered-turn exclusion, chronological coverage, party/branch isolation, archive retrieval isolation, secret exclusion, context budget, and a manual memory rebuild before deployment.

## Intake

For a new world pack, ask only missing mandatory answers, at most three concise
questions at a time:

1. World name.
2. World premise/source: original, real-world/history, or existing IP/fandom.
3. Starting player character role, status/power level, and constraints.
4. Intended scenario compatibility: `rp`, `novel`, or their combination; identify one recommended type. For deterministic learning, use `training-world-pack-builder` instead.

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
world-info/index.md
sillytavern/<slug-or-title>.json
characters/index.md
rules/checks.md
quick-replies/notes.md
setup-flow.md
```

World-pack requirements:

- Put the starting player role in `manifest.player_role`; Light GUI uses it as the default player character description.
- Put `scenario_types.recommended` and `scenario_types.supported` in `manifest.json`. This metadata filters incompatible combinations but never auto-selects the party type.
- Include focused lorebook entries, not one giant encyclopedia entry.
- Separate confirmed facts, rumors, and unresolved mysteries.
- Give NPCs goals, constraints, secrets, and relationships rather than static descriptions.
- Include a playable opening scene and immediate hooks.
- Add explicit narrator "do not do" rules: preserve player agency, obey state, do not turn failed checks into hidden successes.

Scenario prompt requirements:

- `rp`: permit D20, skills, difficulty, modifiers, blockers, and fixed Gateway outcomes. Failed or blocked checks cannot become equivalent hidden success.
- `novel`: prohibit dice, skills, checks, difficulty, result labels, and game menus. Prioritize collaborative prose, character voice, relationships, pacing, continuity, and consent while preserving player agency.
- For packs supporting both types, phrase mechanical rules conditionally. Never write an unconditional check rule that conflicts with `novel`.
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
- Validate `scenario_types.recommended` is `rp` or `novel` and is included in `scenario_types.supported`.
- Validate the state seed from the RP-stack root:

```powershell
python scripts\validate-state.py --state worldpacks\<slug>\state-seed.json --schema state\schema.json
```

- Run relevant tests when gateway/app code or IaC behavior changed.
- Run a narrow scan for API-key-looking strings.
- Do not treat Windows-side `/srv` or `\srv` paths as runtime validation.

## Deploy

If the user asks to create a playable world and does not say draft-only, continue
past artifact creation into the deployment chain. Use `abykovserv-iac-deploy`
for the authoritative procedure.

Expected route:

```text
local Git/IaC edit -> validate -> commit -> push origin/main
-> SSH to abykov@192.168.1.88
-> run the established pull-based Ansible apply there
-> verify runtime files and HTTP endpoints
```

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
