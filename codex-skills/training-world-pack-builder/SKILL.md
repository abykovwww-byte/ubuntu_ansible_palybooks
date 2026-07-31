---
name: training-world-pack-builder
description: Build or update deterministic scored learning world packs for the SillyTavern/rp-gateway/Light GUI stack, including interactive simulated or phishing sites with LLM-filled visible slots and typed browser-event scoring. Use when Codex needs to create a simulation, scenario-based exercise, awareness course, assessment world, role-based training, deterministic curriculum, scoring rubric, debrief, output-validator contract, or training landing-page interaction. For live deployment to abykovserv / 192.168.1.88, also use abykovserv-iac-deploy.
---

# Training World Pack Builder

Create reviewable, playable training world packs for the local RP Stack. This
skill owns authored learning design and pack artifacts; it does not change
Gateway mechanics or deploy directly.

## Hard Boundaries

- Create `scenario_types: { recommended: "training", supported: ["training"] }`. The party creator still selects `training`; a world pack must never silently select or change it.
- Treat Gateway canonical party state as authority. Light GUI creates isolated state from `state-seed.json`; never overwrite `state/current.json` for normal play.
- Use deterministic authored progression only: no dice, random outcomes, `/check`, or hidden model adjudication of correctness.
- Resolve only player actions explicitly stated in the current turn. Advance exactly one scheduled scenario turn after each response.
- Treat site actions as immutable sub-turn events: recording `link_opened`, submit, report, or close must not call an LLM or advance the authored schedule. Consume pending events atomically with the next committed learner turn.
- Give typed UI evidence precedence over contradictory prose for the same decision. Apply every authored `score_rule_id` at most once unless the policy explicitly permits repetition; a later safe action never erases earlier unsafe evidence.
- Let the browser send only artifact identity, semantic event type, and declared field IDs. Never transmit or persist field values, lengths, hashes, masks, clipboard data, or inferred credentials.
- Keep rubrics, score fields, validators, completion rules, and the current schedule in canonical state or explicit pack rules, not only in prose memory.
- Withhold hints, correctness, hidden scoring, remediation, and best-practice teaching until the authored debrief point. Do not make the simulation unwinnable or imply that every event is hostile.
- Do not embed a gameplay model, credentials, real secrets, personal data, exploit payloads, or operationally harmful instructions in a pack.
- Keep the pack defensive, fictionalized, and safety-bounded for security, medical, legal, or other high-stakes topics. Escalate any need for real policy or regulated content to the user.
- Use `abykovserv-iac-deploy` for GitHub + Ansible deployment; never make durable `/srv` or `/opt` edits by hand.

## Intake

Ask only for missing information, at most three questions at once:

1. Learner role, subject domain, and measurable learning objective.
2. Scenario duration and schedule: number of turns, decision surfaces, and exact debrief point.
3. Assessment rubric: observable actions, score/state fields, pass conditions, and feedback style at debrief.

Also ask whether the world needs interactive simulated sites. When enabled and
the user does not choose another size, author ten reusable site blueprints and
read `references/site-artifacts-contract.md` before writing them.

Also establish audience level, permitted fictionalization, language, accessibility
or content constraints, and whether the user requests draft-only. If proceeding
with assumptions, record each in `manifest.json`; do not invent a mandatory
policy, score threshold, or safe procedure.

## Discover

Before editing, inspect the actual nested IaC repo and read:

- the existing training pack `worldpacks/awareness/` as the working example;
- `docs/decisions/007-light-gui-party-memory.md`, `009-long-context-memory-policy.md`, and `010-party-scenario-types.md`;
- `references/training-contract.md` when authoring or reviewing the deterministic schedule, scoring, memory, and debrief contracts;
- `references/site-artifacts-contract.md` when the world contains links that open simulated sites;
- the state schema, existing manifests, and `inventories/local/group_vars/server.yml`.

Verify paths with `rg --files`. Default source location:

```text
ubuntu_ansible_palybooks/roles/apps/files/rp-stack/worldpacks/<slug>/
```

Use lowercase ASCII slugs with letters, digits, and hyphens. Preserve unrelated
uncommitted work.

## Build

Create the normal world-pack contract:

```text
manifest.json
state-seed.json
campaign-bible.md
prompts/gm-system.md
prompts/authors-note.md
prompts/opening-scene.md
world-info/index.md
sillytavern/<slug>.json
characters/index.md
rules/checks.md
quick-replies/notes.md
setup-flow.md
```

For an interactive-site world, also create `artifacts/sites/index.json`, ten
allowlisted blueprint JSON files, and server-only
`rules/site-interactions.json`. Put fixed URLs, field semantics and interaction
classification in authored files; the narrator may fill only visible prose
slots. Include a realistic mix of legitimate, ambiguous and hostile sites so
interactivity is not an answer cue.

For every scheduled site, author one artifact key, one blueprint revision, the
allowed event types, complete fallback slot content, and score-rule mappings.
The main narrator response must return narrative and declared visible slots in
one bundle. Opening a site never triggers a second model call; invalid or failed
narration must still materialize the scheduled site from authored fallback.

Set `manifest.player_role`; make `scenario_types.recommended` and the only
supported type `training`. Keep `rules/checks.md` as the deterministic
resolution-and-scoring contract; it must not describe Gateway checks.

For corporate showroom training worlds, add `manifest.corporate_portal` with
one to five authored characters who actually participate in the scenario.
Classify every card as `static` or `dynamic`. A static card has `position`; a
dynamic card has `position_template` containing `{employee_position}` so the
Gateway materializes its job title when the showroom party is created. Include
only player-visible directory data (`display_name`, city, birthday, phone,
messenger, email); never expose secrets, hidden scoring, or answer keys. Keep
the portal presentational: schedules, score, progression, and correctness stay
in canonical state and the deterministic training contract.

For every training world that can be published in Showroom, add
`manifest.showroom_result`. Set `metric` to `state_path` and `state_path` to the
canonical numeric result authored in `state-seed.json`, for example
`player.resources.total-score`. The world owns this binding; never leave the
result source or state path for the Showroom scenario editor to choose. Keep the
human-facing result label and whether a leaderboard is enabled as scenario
presentation settings.

Put the following in `state-seed.json` under schema-valid fields:

- current schedule/window, turn count, completion state, and remaining turns;
- named score counters for observable learner actions;
- constraints, validated facts, and data the narrator needs to produce the next exact surface;
- no secret answer key exposed as player-facing state.

Use `campaign-bible.md` for the authored turn map. Each turn specifies its
window/header, neutral context, required artefacts or dialogue format, eligible
player actions, deterministic consequences, fields to update, and next-turn
transition. Describe the debrief separately: it alone may reveal scoring,
correctness, explanations, remediation, and completion result.

Write active runtime prompts:

- `gm-system.md`: one authored turn at a time; exact templates; state authority; no hints or assessment before debrief; no random mechanics; preserve player agency.
- `authors-note.md`: voice, realism, pacing, and output presentation; it may not override the Gateway training contract.
- `opening-scene.md`: first scheduled window and a concrete decision surface, without an answer cue.

Use a complete, plausible mix of normal, ambiguous, inconvenient, and
assessment-relevant events. Never label threats or safe answers in the scene.
For structured artifacts such as email, chat, report, patient record, or ticket,
specify all visible fields and validate them against the authored template.

The lorebook is a compatibility artifact. Create focused entries; it is not
the source of score, schedule, or correctness.

## Prompt Memory and Precedence

The Gateway prompt has separate layers:

1. Scenario contract and immutable world prompts.
2. Party-scoped immutable episodic `memory_chapters` for older scenes.
3. Budgeted recent raw turns; retain all raw turns durably.
4. Relevant characters, dynamic canonical state, `AUTHORITATIVE_OUTCOME`, then the current player action.

State and outcome override memory. Do not store the rubric only in episodic
memory, repeat a raw range already covered by a chapter, or use human-facing
journal recaps as narrator memory. Design detailed chapter continuity but keep
the current schedule, score, and debrief gate compact and authoritative.

## Validate

- Parse every JSON file.
- Confirm only `training` appears in `scenario_types` and that it is recommended.
- Validate from the RP-stack source root:

```powershell
python scripts\validate-state.py --state worldpacks\<slug>\state-seed.json --schema state\schema.json
```

- Test deterministic paths: initial turn header; one explicit action updates only expected state; `/check` is rejected; no score/hint leaks before debrief; debrief output includes planned explanation; party and memory isolation hold.
- Test output templates and relevant validator rules. If generic Gateway validation cannot enforce a requested course contract, declare the gap and add code/tests only with user approval.
- Confirm `corporate_portal.characters` contains at most five unique IDs; every dynamic card uses `{employee_position}` and every portal character is used by the authored scenario.
- Confirm `showroom_result.metric` is `state_path`, its `state_path` resolves to a numeric canonical-state field, and the field is updated only by the authored deterministic scoring contract.
- Scan narrowly for API-key-looking strings and unsafe real data.
- For site artifacts, run the checks in `site-artifacts-contract.md`: duplicate
  IDs, unknown renderer/theme/slot/action, unsafe URL or markup, missing
  fallback, missing scheduled reference, missing score rule, and public policy
  leakage are hard failures.
- Run the complete static, container and browser acceptance matrix in
  `site-artifacts-contract.md`. At minimum prove idempotent event recording,
  zero LLM/schedule advancement for sub-turn events, no field values in request
  or storage, atomic next-turn consumption, score-once behavior, history
  restoration, deterministic provider fallback, shared renderer MIME/CSP, and
  equivalent Light GUI/Showroom behavior.

## IaC and Delivery

For a playable pack, add the SillyTavern lorebook to `runtime_source_files` in
`inventories/local/group_vars/server.yml` with `force: false`. Then follow:

```text
local validation -> commit -> push origin/main -> server pull-based Ansible apply -> runtime verification
```

After deployment, verify the manifest is listed by `/api/worldpacks`, the
party can be created explicitly as `training`, its isolated state starts at
turn one, and the lorebook exists under the managed SillyTavern worlds path.
For interactive sites, run the focused Gateway artifact tests inside the rebuilt
container, then perform one synthetic end-to-end path through artifact open,
submit or report, and the following scoring turn. Inspect typed evidence and
canonical counters without publishing raw DB rows or any field values. Record
provider errors separately from artifact behavior; a successful deterministic
fallback does not make the provider error disappear.
If SSH/sudo/network blocks the apply, report the committed/pushed state and
the exact remaining server-side action; do not claim it is live.

## Final Handoff

State whether the pack is draft-only, committed/pushed, or deployed and
visible. List the pack, score/schedule, debrief, validation evidence, and any
Gateway enforcement gap.
