# Training World Contract

Use this compact checklist while authoring or reviewing a training pack.

## Manifest

```json
"scenario_types": { "recommended": "training", "supported": ["training"] },
"showroom_result": {
  "metric": "state_path",
  "state_path": "player.resources.total-score"
},
"training_runtime": {
  "schema_version": "rp-training-runtime.v1",
  "program": "training/program.json",
  "assessment": "training/assessment.json",
  "fallbacks": "training/fallbacks.json"
}
```

The UI user chooses the party type. The Gateway rejects an unsupported
world/type combination; a pack must not auto-select it.

`showroom_result` binds the Showroom leaderboard to the authored numeric field
in canonical state. Define it while creating the world; the scenario editor
must not choose the metric or state path. Verify that `state_path` exists in
`state-seed.json` and is maintained by the deterministic scoring contract.

Interactive support and scenario activation are separate. A valid
`training_artifacts` contract means links are supported; a valid
`training_workspace` contract means the department workspace is supported.
The Showroom scenario stores independent `interactive_links_enabled` and
`interactive_workspace_enabled` flags, and each run snapshots them. Do not add
a second manifest boolean list that can drift from the detailed contracts.

## Authority and lifecycle

`training_runtime` is mandatory for new deterministic training packs.

- Gateway owns the versioned interpreter, state patching, party isolation,
  idempotency, one-call orchestration, validation execution, provider failure
  handling and persistence. It has no knowledge of the course subject.
- WorldPack `program.json` owns turn order, visible event facts, allowed output
  surface, role adaptation, permitted visible state, link policy, debrief and
  complete fallback text.
- WorldPack `assessment.json` owns text/UI detectors, boolean rules, score and
  counter effects, evidence labels and bounded aggregates.
- `training_artifacts` and `training_workspace` declare independent optional
  interaction support. The Showroom run snapshot activates neither, either or
  both; activation is never inferred from the runtime or catalogs.
- Light GUI and Showroom render Gateway-issued data and submit typed events.
  They never choose turns, scoring, correctness or capability activation.

At first runtime access, Gateway hashes and snapshots the combined program,
assessment and fallback files for the party. Existing parties and branches keep
that immutable snapshot after a WorldPack update; new parties receive the new
revision. A missing or newly invalid source file must not corrupt an existing
party snapshot.

## Program contract

`training/program.json` uses `rp-training-program.v1` and contains:

- `progression`: positive `total_turns` and state resource IDs for current
  window, remaining turns and completion status;
- ordered, contiguous `turns` starting at one;
- per turn `window`, exact `header`, narrator `instruction`, optional
  `visible_state_paths`, neutral `question`, and one `surface`;
- a surface type (`email` or `messenger`), count, required fields/regexes,
  forbidden regexes, link policy (`none` or `artifact`), role-adaptation gate,
  question gate and a complete fallback;
- optional regex-based `role_adapters` and a default role task;
- `debrief` with canonical score resource bindings, evidence resource IDs,
  instruction and fallback.

The Gateway gives the LLM only the active turn (or debrief), learner name and
description, explicitly allowlisted visible state and the enabled interaction
contract. It strips fallback text and never includes detectors, score counters,
future turns or answer keys before debrief. The LLM generates fresh wording;
the validator checks the authored facts. Validation or provider failure uses
the same turn's fallback without a repair call.

The sanitized active-turn contract includes the exact authored `header` and
`question`. Plain narration starts and ends with those values. An interactive
turn returns one JSON object and puts the complete visible turn inside
`narrative_text`; no analysis, preamble or Markdown fence is part of the
contract. Gateway may unwrap one provider-added JSON fence before schema
validation, but still rejects multiple or malformed bundles.

Fallback placeholders are allowlisted: `player.name`, `player.description`,
`role.task`, `artifact.url`, and `resource.<id>`. Resource placeholders are for
canonical state values and normally belong only in the debrief. Unknown
placeholders and path traversal are deployment failures.

## Assessment contract

`training/assessment.json` uses `rp-training-assessment.v1`. Its detectors are
generic primitives interpreted by Gateway:

- `text_regex` and `text_regex_count` for explicit learner wording, with
  optional `exclude_patterns` for negated or otherwise disqualifying wording;
- `interaction_event` for normalized, score-eligible site/workspace evidence;
- `profile_overlap` for an observable relation to the stored learner role;
- `expression` combining detector IDs with `all`, `any` and `not`.

Rules select exact turns or `"*"`, evaluate a detector expression and apply
only allowlisted state effects: `increment`, `set`, and `append_evidence`.
Aggregates recompute bounded sums from canonical resources after effects. Every
effect, aggregate component, debrief score and evidence binding must resolve to
`state-seed.json`. Scoring reads only the current learner response and pending
typed events; narrator output never becomes correctness evidence.

Adding a detector primitive or surface type is a versioned generic Gateway
schema change. Adding phishing, fire safety, patient care or any other domain
rule is a WorldPack change and must not introduce a campaign-ID branch in
Gateway.

## Runtime Contract

- No dice, skill checks, randomness, or `/check`-driven correctness.
- Advance exactly one authored turn from the current state schedule.
- Count only explicitly stated learner actions.
- State stores score/counters, schedule, completion conditions, and the debrief gate.
- Before debrief, never reveal correctness, answers, hidden scoring, hints, remediation, or best-practice commentary.
- During debrief, explain outcomes through observable actions and the authored rubric.
- Browser interaction events are immutable sub-turn evidence. They do not
  invoke a model or advance the schedule and are consumed atomically with the
  next committed learner turn.
- Typed UI evidence takes precedence over contradictory prose. Never transmit
  form values: credential exposure is inferred only from a configured field
  being non-empty when submit is pressed.
- A scheduled artifact must be present even when narrator output is invalid or
  the provider fails; use the authored fallback from the same turn instead of a
  second LLM request.
- A runtime turn makes at most one narrator request. Invalid content is not sent
  through a repair completion; validation falls back deterministically.
- A disabled capability contributes no narrator contract, public snapshot,
  event endpoint or scoring evidence. Its authored off-path must keep the
  training coherent and assessable.
- Workspace files use immutable revisions and authored availability intervals.
  Opening, downloading or reporting a file is sub-turn evidence and must not
  call a model or advance the schedule.

## Memory Contract

Older party turns become immutable chronological `memory_chapters`; the
Gateway retains all raw turns and includes the newest complete raw pairs within
the context budget. Canonical state and `AUTHORITATIVE_OUTCOME` override both
chapter memory and raw history. Do not rely on journal recaps or lorebook
entries as the authoritative schedule or score.

## Verification Scenarios

1. Create a party explicitly with `training` and verify the first header/window.
2. Send an explicit learner action and verify only deterministic fields change.
3. Attempt `/check` and verify rejection.
4. Verify a pre-debrief response contains neither a hint nor score/correctness.
5. Reach the authored completion gate and verify the debrief follows its rubric.
6. Create a second party and verify no state, history, or chapter leaks between them.
7. Open and submit an interactive site, verify zero extra LLM calls, no schedule
   advancement, no field values in requests/storage, idempotent event handling,
   and restoration from history.
8. Commit the next learner turn and verify typed events are consumed atomically,
   score rules apply once, canonical evidence names the UI facts, and later prose
   does not erase an earlier unsafe event.
9. Force invalid narrator output or a provider error on a scheduled artifact
   turn and verify the authored fallback still produces the correct safe snapshot.
10. When both interaction contracts exist, run all four links/workspace flag
    combinations and verify disabled surfaces cannot be activated by narrator,
    browser or crafted event input.
11. Edit the Showroom scenario after a run starts and verify the run retains its
    original two capability flags and leaderboard/dataset dimensions.
12. Open a scored phishing file and verify public responses omit its hidden
    classification, the event applies once, and a later report does not erase
    the earlier open evidence.
13. Replace the training subject with a materially different fixture (for
    example phishing to ОБЖ), then verify the same Gateway interpreter loads
    the new active prompt, fallback, resources and scoring with no domain code.
14. Start a party, edit its source WorldPack runtime files, and verify the active
    party and its branches retain the original contract hash while a new party
    receives the new revision.
15. Inspect the active prompt and verify the exact current header/question are
    present while fallback, scoring and future turns are absent; exercise raw,
    fenced and malformed interaction bundles.
16. Force validation fallback and verify the delivered fallback validates,
    turn metadata reports final validator success plus the original reason, and
    audit separates provider fallback from Gateway validation fallback.
