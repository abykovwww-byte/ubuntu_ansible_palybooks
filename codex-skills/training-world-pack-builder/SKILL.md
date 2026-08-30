---
name: training-world-pack-builder
description: Build or update deterministic scored learning world packs for the standalone training-only tavern-awareness-showroom application, including independently optional interactive links and department workspaces with static or dynamic files, LLM-filled visible slots, and typed browser-event scoring. Use when Codex needs to create a simulation, scenario-based exercise, Awareness course, Showroom assessment world, role-based training, deterministic curriculum, scoring rubric, debrief, training site, phishing file, workspace, output-validator contract, or training landing-page interaction. For live deployment to abykovserv / 192.168.1.88, also use abykovserv-iac-deploy.
---

# Training World Pack Builder

Create reviewable, playable training world packs in the public
`tavern-awareness-showroom` application repository. This skill owns authored
learning design and pack artifacts; it does not deploy directly. Zero-window O2
removed the old training copies from
`ubuntu_ansible_palybooks/roles/apps/files/rp-stack`; they are not rollback
material. Fix failures forward through application/IaC PRs while preserving
legacy RP SQLite, state, backups, and authored data artifacts.

## Hard Boundaries

- Create `scenario_types: { recommended: "training", supported: ["training"] }`. The standalone application is training-only and must never expose or accept `rp`.
- Treat the standalone Training Gateway canonical party state as authority. Showroom creates isolated state from `state-seed.json`; never overwrite `state/current.json` for normal play.
- Use deterministic authored progression only: no dice, random outcomes, `/check`, or hidden model adjudication of correctness.
- Resolve only player actions explicitly stated in the current turn. Advance exactly one scheduled scenario turn after each response.
- Treat site actions as immutable sub-turn events: recording `link_opened`, submit, report, or close must not call an LLM or advance the authored schedule. Consume pending events atomically with the next committed learner turn.
- Give typed UI evidence precedence over contradictory prose for the same decision. Apply every authored `score_rule_id` at most once unless the policy explicitly permits repetition; a later safe action never erases earlier unsafe evidence.
- Let the browser send only artifact identity, semantic event type, and declared field IDs. Never transmit or persist field values, lengths, hashes, masks, clipboard data, or inferred credentials.
- Keep rubrics, score fields, validators, completion rules, and the current schedule in canonical state or explicit pack rules, not only in prose memory.
- Put every domain-specific turn, output constraint, detector, scoring effect, aggregate and fallback in the WorldPack `training_runtime` contract. Gateway may interpret the generic schema, snapshot it and apply it, but must not gain a campaign-ID branch, phishing regex, ОБЖ rule or other course-specific constant.
- Keep LLM narration enabled. `training/program.json` constrains the visible event and validates the result; it does not replace fresh narrator wording. Use the authored fallback for provider failure or a hard validation failure. A soft format/profile failure may receive exactly one bounded repair call through the training-specific repair limit.
- Training must never enqueue, load, inject, display, or reserve context for the RP-only `RP_STORY_MEMORY` layer. Keep its existing episodic chapters, raw history, retrieval, and 81920-token default history budget unchanged.
- Withhold hints, correctness, hidden scoring, remediation, and best-practice teaching until the authored debrief point. Do not make the simulation unwinnable or imply that every event is hostile.
- Treat the stored learner name, profession and responsibilities as mandatory scenario input when the user supplies them. Make authored work requests observably change when that profile changes; do not replace it with a generic department, invented backlog or random corporate project.
- Orient before asking for retrospective context. The first turn must provide enough situation, task ownership and a bounded decision for a new learner to act. A request for a weekly recap, backlog reconstruction or work plan is appropriate only after the scenario has established the work it refers to.
- Schedule links intentionally. A reusable site catalog is capacity, not a requirement to place a URL in every narrator response. Mark link-bearing turns explicitly and require a no-link value in the structured output on every other turn.
- Treat interactive links and the interactive workspace as independent optional capabilities. A Showroom scenario may enable neither, either one, or both. A catalog means support, while the scenario/run snapshot grants permission. Never infer enablement from catalog presence or let narrator/browser output activate a disabled capability.
- Author a complete capability-off path for every optional interaction surface. Disabling links or the workspace must leave the training coherent, assessable, and free of correctness cues; if that is impossible, declare the capability required instead of presenting an editable checkbox.
- Treat workspace actions as immutable sub-turn events. Opening, downloading, reporting, or enabling authored active content must not call an LLM or advance the schedule. File availability follows an authored lifecycle across turns; site events retain their narrower authored-surface ownership checks.
- Keep phishing/safe file classification, score mappings and answer keys server-only. Public file snapshots contain only player-visible content and neutral actions.
- Do not embed a gameplay model, credentials, real secrets, personal data, exploit payloads, or operationally harmful instructions in a pack.
- Keep the pack defensive, fictionalized, and safety-bounded for security, medical, legal, or other high-stakes topics. Escalate any need for real policy or regulated content to the user.
- Use `abykovserv-iac-deploy` for GitHub + Ansible deployment; never make durable `/srv` or `/opt` edits by hand.

The implemented standalone Gateway authority lives in `rp-gateway/app/services/training_runtime.py`,
`training_capabilities.py`, `training_artifacts.py`, and
`training_workspace.py`. Showroom persists both
scenario flags and immutable run snapshots in `rp-gateway/app/services/showroom.py`.
`worldpacks/awareness/` and `worldpacks/awareness-one-day/` are executable
examples containing both contracts, a party-start policy resource, a dynamic
turn file, hidden event policy, and capability-off-compatible chat paths.

## Intake

Ask only for missing information, at most three questions at once:

1. Learner role, subject domain, and measurable learning objective.
2. Scenario duration and schedule: number of turns, decision surfaces, and exact debrief point.
3. Assessment rubric: observable actions, score/state fields, pass conditions, and feedback style at debrief.

Ask separately whether the world supports interactive simulated links and an
interactive department workspace. These are independent capabilities. When
links are supported and the user does not choose another size, author ten
reusable site blueprints and read `references/site-artifacts-contract.md`.
When a workspace is supported, read
`references/workspace-artifacts-contract.md` and establish static folders,
resource classification, dynamic file schedule, file events, scoring and both
capability-off paths before writing artifacts.

Also establish audience level, permitted fictionalization, language, accessibility
or content constraints, and whether the user requests draft-only. If proceeding
with assumptions, record each in `manifest.json`; do not invent a mandatory
policy, score threshold, or safe procedure.

## Discover

Before editing, inspect the actual `tavern-awareness-showroom` repository and read:

- the existing training pack `worldpacks/awareness/` as the working example;
- `docs/decisions/007-light-gui-party-memory.md`, `009-long-context-memory-policy.md`, `010-party-scenario-types.md`, and the negative Training boundary in `016-rp-living-story-memory.md`;
- `references/training-contract.md` when authoring or reviewing the deterministic schedule, scoring, memory, and debrief contracts;
- `references/site-artifacts-contract.md` when the world contains links that open simulated sites;
- `references/workspace-artifacts-contract.md` when the world contains a department workspace or scored files;
- the state schema and existing manifests. Inspect
  `ubuntu_ansible_palybooks/inventories/local/group_vars/server.yml` only when
  preparing the exact deployment pin.

Verify paths with `rg --files`. Default source location:

```text
tavern-awareness-showroom/worldpacks/<slug>/
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
training/program.json
training/assessment.json
training/fallbacks.json
```

Every new deterministic training pack declares `manifest.training_runtime`
with schema `rp-training-runtime.v3`. `program.json` uses
`rp-training-program.v3` and owns ordered turns, one or more uniquely typed
`surfaces` per turn, turn-level question/fallback policy, role adapters and debrief.
`assessment.json` owns observable detectors, boolean rules, state effects and
bounded aggregates. `fallbacks.json` remains versioned metadata only; executable
turn fallbacks stay colocated with their turns in `program.json`. Existing v1/v2
packs remain valid and are not rewritten merely to adopt v3. Read
`references/training-contract.md` for the exact contract and ownership split.

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
Keep the schedule sparse enough to remain plausible for the scenario. On every
non-site turn, require the structured message's link field to say that there is
no link and forbid URLs elsewhere in the narration; never let the model infer a
portal link merely because the simulator capability exists.

For an interactive-workspace world, also create
`artifacts/workspace/folders.json`, `artifacts/workspace/files/index.json`,
versioned file blueprints, and server-only
`rules/workspace-interactions.json`. WorldPack folder and file IDs, renderer,
media family, lifecycle, phishing classification and score mapping are authored;
the narrator may fill only declared visible slots. Provide complete fallbacks
for files materialized at party start and on later turns. Real organization
documents are versioned Showroom resources bound to stable folder IDs, not
committed into a public WorldPack by default and not sent to the narrator unless
an explicit reviewed retrieval policy permits it.

For every Showroom-publishable training world, document whether each supported
capability is optional or required. Optional links and workspace must each have
a coherent disabled schedule/fallback. Do not create four copies of the
WorldPack; the Showroom scenario stores two independent booleans and the run
snapshots the chosen combination.

Set `manifest.player_role`; make `scenario_types.recommended` and the only
supported type `training`. Keep `rules/checks.md` as the human-readable mirror
of `training/assessment.json`; the executable contract is the JSON and neither
file may describe Gateway checks.

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

- current schedule/window, turn count, completion state, and remaining turns referenced by `training/program.json`;
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
- `opening-scene.md`: first scheduled window and a concrete decision surface, without an answer cue. Give the learner enough role-specific context to act; do not begin by asking them to invent prior work, a week summary or a plan for tasks the scenario has not introduced.

Use a complete, plausible mix of normal, ambiguous, inconvenient, and
assessment-relevant events. Never label threats or safe answers in the scene.
For structured artifacts such as email, chat, report, patient record, or ticket,
specify all visible fields and validate them against the authored template.
Every active turn must author a non-empty exact `header` and neutral `question`.
Gateway passes both values from the immutable WorldPack snapshot to the narrator
and normalizes the final text to those canonical boundaries; do not rely on
`instruction` or conversation history to make the model guess either boundary.
Author each `surfaces[].must_include` as short natural-language requirements mirroring
the machine-only `required_patterns`; raw regexes stay in the validator and are
not narrator instructions. Author a non-empty optional `variation_budget` list
on each turn when fresh wording is desired, naming only elements the model may
change (for example subject, body wording, time inside the window, task detail,
or tone). Omitting the field remains valid and grants no extra freedom.

The lorebook is a compatibility artifact. Create focused entries; it is not
the source of score, schedule, or correctness.

## Prompt Memory and Precedence

The Gateway prompt has separate layers:

1. Universal scenario-mode rules and immutable world prompts.
2. Party-scoped immutable episodic `memory_chapters` for older scenes.
3. Budgeted recent raw turns; retain all raw turns durably.
4. One sanitized `ACTIVE_TRAINING_TURN_CONTRACT` loaded from the party's immutable WorldPack runtime snapshot.
5. Relevant characters, permitted dynamic canonical state, `AUTHORITATIVE_OUTCOME`, then the current player action.

The RP-only living story-memory block is deliberately absent from this list. A
Training change must not activate its service job, API/UI fields, prompt block,
or token reserve.

State and outcome override memory. Do not store the rubric only in episodic
memory, repeat a raw range already covered by a chapter, or use human-facing
journal recaps as narrator memory. Design detailed chapter continuity but keep
the current schedule, score, and debrief gate compact and authoritative. Before
debrief, the active prompt contract may contain learner identity, profession,
the active surface, explicit visible state paths and enabled interaction
contracts; it must not serialize score resources, detector definitions,
answer keys or future turns.

The narrator returns only the visible narration. When an interaction contract
requires a narrative bundle, it returns one JSON object with the complete turn
inside `narrative_text`, without a preamble or Markdown fence. Gateway may
normalize one provider-added JSON fence before schema validation, but the
WorldPack must not depend on that tolerance and must never put domain rules in
the normalization layer.

## Validate

- Parse every JSON file.
- Run the same IaC preflight used before deployment:

```powershell
python scripts\validate-training-runtime.py --worldpacks worldpacks
```

- Verify `training_runtime` files cannot escape the pack root; turns are contiguous; all regexes compile; every detector reference, effect, aggregate, debrief score and fallback resource resolves to the state seed.
- Confirm only `training` appears in `scenario_types` and that it is recommended.
- Validate from the RP-stack source root:

```powershell
python scripts\validate-state.py --state worldpacks\<slug>\state-seed.json --schema state\schema.json
```

- Test deterministic paths: initial turn header; one explicit action updates only expected state; `/check` is rejected; no score/hint leaks before debrief; debrief output includes planned explanation; party and memory isolation hold.
- Inspect the serialized active prompt contract and assert it contains the
  exact authored header and question for the current turn while excluding the
  fallback, assessment, score resources and future turns.
- Assert that Training enqueues no `rp_story_memory` job, contains no `RP_STORY_MEMORY` prompt block, and retains the pre-RP-story context budget.
- Test at least two materially different player profiles and prove the opening task and later work requests change accordingly. Exercise the validation-failure fallback too; it must use the same stored profile rather than reverting to generic corporate copy.
- Assert the exact authored set of link-bearing turns. For every other turn, reject both a non-empty structured link field and any URL in free text; the presence of a site catalog must not make links ubiquitous.
- Exercise the four capability combinations when both contracts are supported: neither, links only, workspace only, and both. Reject enabled capabilities unsupported by the manifest and reject both flags for `rp`.
- Verify capability-off paths remain playable, do not materialize disabled snapshots, reject disabled event endpoints, and do not leak an answer cue through missing UI affordances.
- Test output templates and relevant validator rules. If generic Gateway validation cannot enforce a requested course contract, extend the versioned generic runtime schema/interpreter with tests; never add a world ID or subject-specific rule to Gateway. Declare the schema change before implementing it when it broadens the user's requested scope.
- For bundle surfaces, test raw JSON, a single provider-added fenced JSON
  object, malformed/multiple bundles and fallback materialization. Final turn
  metadata must describe the delivered response as validator-valid while
  preserving the original `fallback_reason`; audit events must distinguish
  provider failure from Gateway validation failure.
- Confirm `corporate_portal.characters` contains at most five unique IDs; every dynamic card uses `{employee_position}` and every portal character is used by the authored scenario.
- Confirm `showroom_result.metric` is `state_path`, its `state_path` resolves to a numeric canonical-state field, and the field is updated only by the authored deterministic scoring contract.
- Scan narrowly for API-key-looking strings and unsafe real data.
- For site artifacts, run the checks in `site-artifacts-contract.md`: duplicate
  IDs, unknown renderer/theme/slot/action, unsafe URL or markup, missing
  fallback, missing scheduled reference, missing score rule, and public policy
  leakage are hard failures.
- For workspace artifacts, run the checks in
  `workspace-artifacts-contract.md`: folder/file ID integrity, lifecycle,
  renderer/media allowlists, immutable revisions, resource classification,
  fallback completeness, hidden-policy separation and event-to-score coverage
  are hard failures.
- Run `pytest tests/test_training_capabilities.py` plus the Showroom capability
  test in `tests/test_gateway.py`; do not accept a manifest merely because its
  top-level schema string is present. Gateway must load and validate every
  referenced catalog, blueprint, resource and server-only policy file.
- Run `pytest tests/test_training_runtime.py`. Add a non-domain twin fixture
  (for example ОБЖ for an awareness change) proving that the same Gateway code
  loads different turns and scoring without subject-specific branches.
- Run the complete static, container and browser acceptance matrix in
  `site-artifacts-contract.md`. At minimum prove idempotent event recording,
  zero LLM/schedule advancement for sub-turn events, no field values in request
  or storage, atomic next-turn consumption, score-once behavior, history
  restoration, deterministic provider fallback, shared renderer MIME/CSP, and
  equivalent Light GUI/Showroom behavior.
- Prove a run snapshots both `interactive_links_enabled` and
  `interactive_workspace_enabled`; editing the scenario later must not alter
  the active run. Keep leaderboard, autotest and dataset results partitionable
  by the two flags.

## IaC and Delivery

Publish application changes in `tavern-awareness-showroom` first. After its PR
is merged, update only the exact `awareness_showroom_repo_version` pin in
`ubuntu_ansible_palybooks/inventories/local/group_vars/server.yml`. Then follow:

```text
standalone app validation on a codex/ branch -> app non-draft PR -> green CI -> app merge
-> IaC pin branch -> IaC non-draft PR -> green CI -> IaC merge
-> server pull-based Ansible apply -> runtime verification
```

Direct pushes to `main` are prohibited; do not leave merge-ready work on the
working branch.

After deployment, verify the manifest is listed by the standalone
`http://192.168.1.88:8011/api/worldpacks`, the Showroom run creates a training
party whose isolated state starts at turn one, and the RP endpoint on port 8010
does not list or resume it.
For interactive sites, run the focused Gateway artifact tests inside the rebuilt
container, then perform one synthetic end-to-end path through artifact open,
submit or report, and the following scoring turn. Inspect typed evidence and
canonical counters without publishing raw DB rows or any field values. Record
provider errors separately from artifact behavior; a successful deterministic
fallback does not make the provider error disappear.
For interactive workspaces, also verify initial static folders, one dynamic
file, immutable history restoration, an idempotent scored file event, absence
of phishing classification in public responses, and rejection of restricted
resources in anonymous Showroom. Opening a file must add no narrator request.
If SSH/sudo/network blocks the apply, report the committed/pushed state and
the exact remaining server-side action; do not claim it is live.

## Final Handoff

State whether the pack is draft-only, committed/pushed, or deployed and
visible. List the pack, score/schedule, debrief, validation evidence, and any
Gateway enforcement gap.
