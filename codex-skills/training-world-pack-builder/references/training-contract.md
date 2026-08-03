# Training World Contract

Use this compact checklist while authoring or reviewing a training pack.

## Manifest

```json
"scenario_types": { "recommended": "training", "supported": ["training"] },
"showroom_result": {
  "metric": "state_path",
  "state_path": "player.resources.total-score"
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
