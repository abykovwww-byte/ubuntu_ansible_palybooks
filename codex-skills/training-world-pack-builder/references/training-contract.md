# Training World Contract

Use this compact checklist while authoring or reviewing a training pack.

## Manifest

```json
"scenario_types": { "recommended": "training", "supported": ["training"] }
```

The UI user chooses the party type. The Gateway rejects an unsupported
world/type combination; a pack must not auto-select it.

## Runtime Contract

- No dice, skill checks, randomness, or `/check`-driven correctness.
- Advance exactly one authored turn from the current state schedule.
- Count only explicitly stated learner actions.
- State stores score/counters, schedule, completion conditions, and the debrief gate.
- Before debrief, never reveal correctness, answers, hidden scoring, hints, remediation, or best-practice commentary.
- During debrief, explain outcomes through observable actions and the authored rubric.

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
