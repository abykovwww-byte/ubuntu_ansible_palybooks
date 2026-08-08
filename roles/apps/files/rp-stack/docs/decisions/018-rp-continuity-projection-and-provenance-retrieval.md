# Decision 018: RP Continuity Projection with Provenance-Backed Retrieval

Date: 2026-08-08

## Status

Proposed.

This decision describes a planned RP-only evolution of the current living story
memory. It is not implemented runtime behavior yet. Until the implementation is
validated and enabled, Decision 016 and the current `RPStoryMemoryUpdater`
remain authoritative for production behavior.

If accepted and implemented, this decision supersedes the full-replacement
update contract and default four-turn cadence from Decision 016. It does not
change the existing authority boundary: canonical state and
`AUTHORITATIVE_OUTCOME` remain above every memory or retrieval layer.

## Context

RP Stack already has the main primitives required for durable long-session
continuity:

- party-scoped committed raw turns in SQLite;
- versioned canonical state and deterministic `AUTHORITATIVE_OUTCOME`;
- immutable episodic memory chapters;
- RP-only cumulative story-memory snapshots;
- durable post-turn service jobs with retry and restart recovery;
- party-scoped lore cards and archived-turn retrieval;
- checkpoints, branches, Prompt Inspector and isolated eval runs.

The current RP story-memory updater is intentionally bounded and fail-open, but
its v1 contract has structural weaknesses for very long campaigns:

1. The service model returns a complete replacement JSON document. An omitted
   older detail can disappear even when no later event invalidated it.
2. Entries are mostly free-form strings without stable IDs or mandatory links
   to the raw turns from which they were derived.
3. The narrator cannot deterministically request the original evidence for a
   selected memory item.
4. The default four-turn cadence can leave fresh continuity outside the latest
   snapshot while the next player action is already being prepared.
5. Approximate archived retrieval is driven mainly by the current player text.
   Implicit references, renamed entities and old promises can be missed.
6. A large token-budgeted raw tail protects continuity, but it spends context on
   many scenes that are not relevant to the current action.
7. Repeated full-document LLM rewriting can create semantic drift: an uncertain
   interpretation may become a stronger statement in later revisions and then
   influence subsequent narration.

The intended user experience is different: after a committed RP turn, a service
model should update continuity while the player reads and writes the next turn.
The narrator should receive a compact current projection, a small recent raw
window, and original historical context only when it is relevant.

## Decision

Introduce an RP-only `RP Continuity Projection v2` as a derived, provenance-
backed view over committed raw turns and canonical state.

The design is:

```text
Committed raw turn log
        +
Canonical state and outcome metadata
        |
        v
Background continuity extractor (service model)
        |
        v
Strict typed delta
        |
        v
Deterministic projector
        |
        v
Revisioned continuity snapshot
        |
        +--------------------+
        |                    |
        v                    v
Exact source lookup     Approximate archive retrieval
        |                    |
        +----------+---------+
                   v
             Context builder
                   |
                   v
              Narrator LLM
```

The continuity projection is a materialized view and continuity aid. It is not
canonical state, it cannot resolve game mechanics, and it cannot override a
later explicit committed event.

### Authority model

The prompt and conflict-resolution order remains:

1. universal scenario and safety rules;
2. authored WorldPack/GM canon and canonical Gateway state;
3. `AUTHORITATIVE_OUTCOME` for the current turn;
4. later explicit committed raw events;
5. active explicit continuity items backed by provenance;
6. inferred continuity items with confidence;
7. episodic summaries, lore retrieval hints and approximate search results.

When a lower layer conflicts with a higher layer, the lower layer is ignored or
marked stale. The service model never promotes its own inference into canonical
state.

### Source event and commit boundary

Continuity processing uses only the final committed turn:

- the committed player message;
- the final visible narrator response after repair or safe fallback;
- the committed state version;
- the stored `Outcome` and state-patch metadata when available.

It must not use an initial provider response that was rejected by the validator
or replaced before commit.

Player text is normally an attempt, plan or claim. It becomes an explicit
continuity event only when the event is the speech act itself, such as a promise
or disclosure, or when the narrator/outcome confirms the result. A statement
such as "I kill the dragon" is not recorded as a completed fact merely because
it appeared in the player message.

### Continuity snapshot

A snapshot is bounded, party-scoped and revisioned. The exact schema is fixed by
JSON Schema/Pydantic before implementation, but the logical shape is:

```json
{
  "schema_version": "rp-gateway.rp-continuity.v2",
  "revision": 18,
  "covered_through_turn_id": 184,
  "state_version": 185,
  "current_situation": {
    "text": "The party is inside the archive after the doors were sealed.",
    "source_turn_ids": [184]
  },
  "items": {
    "character:mira:location": {
      "kind": "character_state",
      "subject_id": "character:mira",
      "predicate": "location",
      "value": "location:archive",
      "status": "active",
      "epistemic_type": "explicit",
      "confidence": 1.0,
      "source_turn_ids": [184],
      "last_updated_turn_id": 184
    }
  }
}
```

The projection can contain dynamic world facts, character state, relationships,
possessions, promises, obligations, active and resolved threads, unresolved
hooks, rules learned during play, current situation and a bounded causal
chronology.

Each item has a stable branch-local ID. Later updates supersede or resolve the
same item rather than appending a contradictory duplicate.

The service extractor may emit only:

- `explicit`: directly supported by committed narration, outcome or canonical
  state;
- `inferred`: a cautious interpretation, always with confidence below `1.0`.

Hidden authored motives, NPC secrets and future plans are not inferred from
behavior. They remain in WorldPack or canonical hidden state and are exposed to
the narrator only through existing visibility rules.

### Typed delta instead of full replacement

The service model returns a strict delta, not a replacement snapshot and not an
arbitrary JSON Patch over storage paths.

```json
{
  "schema_version": "rp-gateway.rp-continuity-delta.v1",
  "base_revision": 17,
  "source_turn_ids": [184],
  "operations": [
    {
      "op": "upsert_item",
      "item": {
        "item_id": "character:mira:location",
        "kind": "character_state",
        "subject_id": "character:mira",
        "predicate": "location",
        "value": "location:archive",
        "epistemic_type": "explicit",
        "confidence": 1.0,
        "source_turn_ids": [184],
        "evidence": [
          {
            "turn_id": 184,
            "message_role": "assistant",
            "quote": "Mira entered the archive and closed the door."
          }
        ]
      }
    }
  ]
}
```

Allowed operations are typed, for example:

- `upsert_item`;
- `supersede_item`;
- `resolve_item`;
- `remove_inference`;
- `set_current_situation`;
- `append_chronology_entry`.

A deterministic projector owned by Gateway:

- validates the schema and allowed operation types;
- checks `base_revision` and source coverage;
- verifies that every referenced turn belongs to the current campaign/branch;
- verifies supplied evidence quotes against the committed message when quotes
  are present;
- applies operations transactionally and idempotently;
- refuses deletion by omission;
- preserves previous revisions and the delta ledger;
- records conflicts instead of silently overwriting incompatible explicit
  facts;
- creates the next bounded snapshot.

The service model never writes canonical state and never writes database rows
directly.

### Provenance and exact source retrieval

Every active continuity item must contain at least one `source_turn_id`. Inferred
items must also identify the text or events that support the inference.

When a selected item becomes relevant to the current action, Gateway can perform
an exact party-scoped lookup by turn ID. This is deterministic provenance
retrieval, not vector search.

```text
continuity item
    -> source_turn_ids
    -> committed turns table
    -> original player/narrator text
```

The original raw turn remains the source history. Continuity JSON stores only
references and bounded evidence excerpts, not a second full copy of the
transcript.

### Recent and uncovered raw history

For RP v2 the default immediate working history becomes:

```text
last 10 committed turn pairs
UNION
all turns newer than the latest continuity snapshot coverage
```

The union is deduplicated by turn ID and remains token-budgeted. The number ten
is configurable, but snapshot coverage is mandatory.

This rule prevents an asynchronous race from hiding a fresh event. If the
continuity updater is delayed or fails, every uncovered committed turn remains
in the next narrator prompt even when it falls outside the configured recent
window.

Training and novel history policies are unchanged by this decision.

### Background execution and job priority

After every committed RP turn, Gateway enqueues or coalesces one
`rp_continuity` service job. The job runs outside the gameplay response path.
Failure never rolls back or invalidates the already committed turn.

The current local service model has one parallel slot. Running continuity and
memory-chapter model calls concurrently would normally add contention rather
than reduce latency. The default implementation therefore uses explicit job
priority:

1. `rp_continuity`;
2. episodic `memory` when a chapter is actually eligible;
3. lower-priority maintenance jobs.

One pending/running job per campaign is coalesced so a burst of player turns does
not create an unbounded queue. An updater invocation processes all uncovered
turns that fit its service-model budget, then continues in bounded batches if
required.

The narrator never waits for the job. Staleness is observable through coverage,
revision and job status, while uncovered raw turns provide the correctness
fallback.

### Context selection and prompt order

A deterministic context planner selects continuity items before prompt
assembly. It uses the current player text, `Outcome.target`, active characters,
current location, active threads, promises, obligations and directly linked
entities. An extra LLM planner is not added to every turn unless later evals
show a material recall improvement that justifies the latency and cost.

The RP prompt order becomes:

```text
1. Scenario/system rules
2. World system prompt
3. Author's note
4. Relevant RP_CONTINUITY_STATE v2 items
5. Relevant episodic chapters
6. Relevant lore cards
7. Recent raw turn pairs
8. Uncovered raw turn pairs
9. Exact provenance source turns
10. Approximate archived-turn retrieval
11. Relevant character block
12. Canonical state summary
13. AUTHORITATIVE_OUTCOME
14. Current player action
```

The current player action remains the final message. Duplicate turn IDs are
removed across recent, uncovered, exact and approximate retrieval blocks.

The context builder does not inject the complete continuity snapshot when only a
small entity/thread slice is relevant. Prompt Inspector shows selected item IDs,
source turn IDs, retrieval reason, coverage and staleness.

### Approximate archived retrieval

Exact provenance is the primary mechanism for explaining a known continuity
item. Approximate retrieval handles implicit references for which no item was
selected yet, such as "I do what we agreed then."

The first implementation improves the existing local retrieval without adding a
new database service:

1. check whether SQLite FTS5 is available in the pinned Gateway runtime;
2. use party-scoped FTS/BM25 when available;
3. combine it with the existing exact-term, conservative-stem, character
   n-gram and recency signals;
4. build queries from current action plus selected entities, active threads,
   promises and obligations;
5. preserve explainable component scores in Prompt Inspector;
6. fetch full raw text from `turns` only after candidate IDs are selected.

Embeddings and a vector database are deferred. They may be added only if a
representative eval set shows that the explainable local hybrid retriever misses
material continuity. Any future vector index must be party/branch scoped, store
IDs rather than authoritative text, and preserve exact raw lookup.

### Lore-card boundary

Authored lore and dynamic party continuity remain separate:

- WorldPack/lore cards contain stable authored background and reusable world
  knowledge;
- RP continuity contains party-specific events and changing state derived from
  committed play.

The service model does not silently create, modify or archive authored lore
cards. The context planner can select both layers automatically. Manual lore
inclusion remains an administrative/debug override, not the normal runtime
path.

### Persistence and branch behavior

Add new append-only storage rather than mutating v1 rows in place during rollout:

```text
rp_continuity_deltas
rp_continuity_snapshots
```

The existing `turns` table remains the raw source log. `service_jobs` gains the
`rp_continuity` job type.

Checkpoint forks copy the newest snapshot fully covered by the checkpoint,
reset the target branch revision to one, and remap every `source_turn_id` and
coverage boundary through the existing source-to-branch turn map. Deltas outside
the checkpoint are not copied.

All reads and writes remain scoped by `state_campaign_id`. Cross-party retrieval
is forbidden even when text or entity IDs are identical.

### Failure handling

The continuity path is fail-open for gameplay and fail-closed for invalid
projection data:

- provider timeout, malformed JSON or exhausted retry keeps the previous
  snapshot;
- invalid delta operations are rejected and audited;
- a stale snapshot never hides uncovered raw turns;
- a base-revision conflict causes regeneration from the latest snapshot rather
  than blind application;
- an inferred item cannot override a conflicting explicit item;
- continuity failure does not trigger narrator repair or a second narrator call;
- raw history, canonical state and the completed player response remain intact.

## Rollout and migration

Implementation uses feature flags and four phases.

### Phase 1: storage and shadow generation

- add schemas, delta ledger, snapshots and projector;
- enqueue v2 jobs for RP parties;
- generate and validate v2 without injecting it into narrator prompts;
- expose coverage, provenance and conflicts in admin/Prompt Inspector;
- keep v1 story memory fully active.

### Phase 2: shadow context comparison

- build the proposed v2 context alongside the actual v1 context;
- record only bounded audit metadata, never secret-bearing prompts;
- compare selected entities, source coverage, prompt size and retrieval misses;
- run offline long-session fixtures and isolated provider canaries.

### Phase 3: opt-in narrator injection

- enable v2 per test party or feature flag;
- use recent plus uncovered raw and provenance retrieval;
- keep v1 available as rollback;
- verify branch, restart, provider-failure and stale-job behavior.

### Phase 4: default and retirement

- make v2 the RP default only after acceptance criteria pass;
- stop injecting and updating v1 for new RP turns;
- retain v1 snapshots for audit and rollback;
- document any later data cleanup as a separate decision.

## Codex implementation workflow

The implementation task must follow `AGENTS.md`,
`docs/repository-work-standard.md` and the `rp-stack-wiki` skill. It must use a
`codex/` branch or isolated worktree and keep local, committed, pushed,
Ansible-applied and live-verified states separate.

The main Codex agent first launches parallel read-only subagents:

1. **Persistence and branch mapping**: inspect `state_store.py`, migrations,
   checkpoints and fork remapping.
2. **Prompt and retrieval**: inspect `main.py`, `narrative.py`,
   `character_retrieval.py`, context budgeting and Prompt Inspector.
3. **Jobs and race conditions**: inspect `adjudicator.py`, service-job recovery,
   retry/coalescing and local-model slot constraints.
4. **API and Light GUI**: inspect existing story-memory views, admin endpoints
   and source-turn display components.
5. **Tests and documentation**: map Gateway tests, eval fixtures, Wiki and ADR
   impact.

The orchestrator then fixes the shared contracts before write agents start:

- snapshot and delta schemas;
- typed operations and projector precedence;
- storage interfaces;
- context-selection result model;
- feature flags and rollout states;
- branch-copy contract;
- audit and Prompt Inspector fields.

After that, parallel write work is allowed only in non-overlapping ownership
areas:

- continuity schemas/extractor/projector;
- storage and retrieval primitives;
- isolated test fixtures/evals;
- isolated Light GUI components.

Shared integration files including `adjudicator.py`, `main.py`, `narrative.py`,
`config.py`, common schemas, migration ordering and Wiki navigation remain owned
by the main agent. Parallel reviewers then cover data consistency, async races,
retrieval/provenance, party isolation, hidden-data leakage and test gaps.

Do not invent project subagent configuration in `.codex/config.toml`; the current
repository file enables hooks only. Use the Codex host's available subagent
workflow and degrade to parallel read/review plus sequential shared writes when
write isolation is uncertain.

## Validation and acceptance criteria

The change is not complete until all of the following are demonstrated:

- every active v2 item has valid party-scoped `source_turn_ids`;
- the projector is deterministic and idempotent;
- omission from a delta cannot delete an existing item;
- explicit/inferred precedence and supersession are covered by tests;
- source evidence cannot reference another party or a turn after branch
  checkpoint coverage;
- branch forks remap provenance correctly;
- stale or failed jobs leave all uncovered raw turns in the next prompt;
- no continuity service failure blocks or rolls back a committed turn;
- no extra narrator call is introduced by continuity processing;
- Prompt Inspector explains selected continuity and archive sources;
- existing `training` and `novel` behavior and token budgets remain unchanged;
- the full offline RP Stack gate passes;
- a synthetic long campaign of at least 200 turns covers possessions, injuries,
  NPC knowledge, promises, scene changes, contradictory facts and ambiguous
  emotions;
- an isolated provider canary proves source party state/history are unchanged;
- Wiki pages and diagrams accurately distinguish current v1 runtime from planned
  or enabled v2 behavior.

## Alternatives considered

### Keep the v1 full-replacement story memory

Rejected as the long-term target because it cannot provide item-level
provenance and can lose older details by omission. It remains the rollout
fallback.

### Let the service model patch canonical state

Rejected. It would make an interpretive background model an authority over game
facts and would create a self-reinforcing error path.

### Store raw text only in a vector database

Rejected. A vector index is approximate and should never be the sole copy of
source history. SQLite committed turns remain the primary record.

### Add embeddings and a separate vector service immediately

Deferred. Exact provenance plus SQLite FTS/local hybrid retrieval is simpler,
more explainable and probably sufficient for the first measurable iteration.

### Send the complete campaign transcript on every narrator call

Rejected because prompt cost and attention noise grow with campaign length, and
it does not solve provenance or conflict handling.

### Use an LLM context planner on every turn

Deferred. Deterministic entity/thread selection is cheaper and lower latency.
An LLM planner requires evidence from evals before entering the critical path.

## Consequences

Positive consequences:

- long RP campaigns gain a compact current continuity model without discarding
  the original evidence;
- the narrator can recover the exact historical context of selected facts;
- service-model mistakes are inspectable, reversible and less likely to become
  self-confirming canon;
- prompt growth becomes bounded by relevant state, recent raw and selected
  sources rather than total campaign length;
- the design reuses Gateway, SQLite, service jobs, branches and Prompt Inspector
  instead of introducing another application or authority;
- dynamic party continuity no longer depends on manually promoting events into
  lore cards.

Costs and risks:

- the delta schema, projector, conflict rules and branch remapping add
  implementation complexity;
- a poor context selector can still miss relevant history, so eval quality is
  central;
- every-turn service processing increases local-model work, although coalescing
  and priority limit queue growth;
- item-level provenance increases snapshot and database size;
- inferred psychological state remains inherently uncertain and must stay
  visibly non-authoritative;
- v1 and v2 coexist during rollout, temporarily increasing code and UI surface.

## Related decisions

- [Decision 006: Light GUI Party Flow](006-light-gui-party-flow.md)
- [Decision 009: Long-Context Memory Policy](009-long-context-memory-policy.md)
- [Decision 010: Party Scenario Types](010-party-scenario-types.md)
- [Decision 016: RP-only Living Story Memory](016-rp-living-story-memory.md)
- [Decision 017: WorldPack-owned Training Runtime](017-worldpack-owned-training-runtime.md)
