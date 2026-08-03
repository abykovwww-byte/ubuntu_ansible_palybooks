# Interactive Workspace Artifacts Contract

Use this contract only for deterministic training worlds that need a
party-scoped department workspace with folders and static or dynamic files.

## Capability gate

Add a detailed `manifest.training_workspace` contract to declare support. A
Showroom scenario independently enables it with
`interactive_workspace_enabled`; the run snapshots that flag. Contract
presence alone never activates the workspace.

When disabled, Gateway must not create a workspace snapshot, add file slots to
the narrator contract, expose workspace endpoints or accept file events. The
WorldPack must provide a coherent workspace-disabled path if the capability is
optional.

## Manifest and files

```json
"training_workspace": {
  "schema_version": "rp-training-workspace.v1",
  "folder_catalog": "artifacts/workspace/folders.json",
  "file_catalog": "artifacts/workspace/files/index.json",
  "interaction_policy": "rules/workspace-interactions.json"
}
```

Create:

```text
artifacts/workspace/
  folders.json
  files/index.json
  files/<blueprint>.json
rules/workspace-interactions.json
```

Use stable lowercase IDs. A display name is never a filesystem path or
authority key. Reject traversal, absolute paths, drive letters, control
characters and duplicate folder/file IDs.

## Authority split

- WorldPack owns folder/file blueprint IDs, placement, safe renderer, media
  family, lifecycle, visible slot schema, complete fallback and server-only
  interaction policy.
- Showroom scenario owns only capability activation and bindings from versioned
  training resources to stable folder IDs.
- Gateway owns validation, party/run snapshot, immutable file revision,
  authorization, event order and normalized deterministic evidence.
- The main narrator completion fills declared visible string slots only. It may
  not choose a path, renderer, MIME, file classification, lifecycle or score.
- The browser owns presentation and sends only file identity, revision,
  semantic event type and an idempotency key.

Never execute model-provided HTML, CSS, JavaScript, macros, binaries or remote
assets. Never expose phishing/safe classification, score mappings, answer keys,
real secrets or restricted source documents in public JSON, DOM attributes or
narrator prompts.

## Folder and file blueprints

`folders.json` defines a bounded tree with stable `folder_id`, optional
`parent_id`, order and player-visible label. Detect cycles and orphan parents.

Each file blueprint defines:

- stable `id` and positive `revision`;
- authored `folder_id`, display name template and safe extension;
- allowlisted renderer and media family;
- lifecycle: `party_start` or exact authored turn, `available_from_turn`,
  optional `available_until_turn` and deletion behavior;
- LLM slot names, required flags and length limits;
- complete fallback content;
- optional resource-binding key for a static uploaded document.

Keep file revisions immutable. History must restore the exact public snapshot
seen by the learner, not re-render a changed blueprint or resource.

## Static resources

Real documents live in a versioned training resource library, not in a public
WorldPack by default. Bind an exact resource revision to a stable folder ID.
Record content hash, detected MIME, size, processing status and classification.

Use only `public_training` resources in anonymous Showroom. Treat
`restricted_internal` as unsupported until participants have authenticated
authorization beyond a visitor cookie. Do not send uploaded documents to the
narrator by default. Encode assessable policy requirements as explicit
server-only rules instead of asking the model to interpret a real policy.

If conversion, antivirus scanning or text extraction is required, perform it
asynchronously during ingestion. Block scenario publication until mandatory
resources are ready; never run conversion on list, open or preview.

## Dynamic files

At party start or an authored turn, Gateway gives the existing main narrator
call an exact file contract. The response may contain only the expected
`file_key`, `blueprint_id` and declared visible slots. Gateway chooses every
structural and policy field.

Use authored fallback slots when output is missing, invalid or unavailable.
Opening a file never triggers a second LLM call. The workspace must remain
usable when the provider fails.

## Events and scoring

Allowed semantic events may include `file_opened`, `file_downloaded`,
`file_reported`, `link_opened` and `active_content_enabled`. Declare the exact
set per blueprint in server-only policy.

For every scored event define evidence, `score_rule_id`, `score_once` and, when
needed, `decision_result`. Point deltas and final rubric stay in
`rules/checks.md` and canonical state.

An authored phishing file may classify `file_opened` as failure without
inspecting any user content. A later report is a separate fact and never erases
the earlier unsafe event. Repeated event IDs return the saved result; reuse with
a different payload is rejected.

File validity follows its authored availability interval, not merely the
current turn. Do not weaken site artifact turn checks to reuse this lifecycle.
Normalize accepted evidence before RuleEngine while preserving separate
validation for sites and files.

## Validation

Reject:

- folder cycles, orphan parents, duplicate IDs or unsafe paths;
- unknown renderer, media family, extension, lifecycle or event type;
- missing/oversized required slots or incomplete fallback;
- file blueprint references to absent folders or schedules;
- dynamic structural fields left to the narrator;
- public phishing classification, policy or score data;
- restricted resources bound to anonymous Showroom;
- mutable history that resolves through the latest resource/blueprint revision;
- optional workspace without a coherent disabled path;
- any design requiring an LLM, filesystem scan or document conversion on open.

## Acceptance matrix

- parse catalogs, blueprints and policy; validate IDs, tree integrity,
  lifecycle, fallback, renderer/media allowlists and policy coverage;
- create a run and verify initial static folders/files pin exact revisions;
- materialize one dynamic file from the main narrator bundle and from fallback;
- reload history and verify the same immutable file revision;
- record an idempotent file open and prove zero LLM calls and zero schedule
  advancement;
- consume evidence with the next learner turn and apply each score rule once;
- prove public API/DOM omit phishing classification and restricted content;
- verify a later report does not erase earlier unsafe-open evidence;
- verify `interactive_workspace_enabled=false` suppresses prompt contracts,
  snapshots and endpoints;
- run all four links/workspace combinations and keep result, leaderboard,
  autotest and dataset metadata partitionable by both flags.
