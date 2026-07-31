# Interactive Site Artifacts Contract

Use this contract only for deterministic training worlds that need simulated
sites opened from authored emails or messenger messages.

## Manifest and files

Add:

```json
"training_artifacts": {
  "schema_version": "rp-training-artifacts.v1",
  "site_catalog": "artifacts/sites/index.json",
  "interaction_policy": "rules/site-interactions.json",
  "default_site_count": 10
}
```

Create ten blueprint files by default. The application owns renderer code; a
pack may reference only: `credential-form`, `otp-form`, `file-share`,
`document-approval`, `payment-review`, `tracking-form`, `meeting-join`,
`survey-form`, and `support-download`. Allowed themes are `office-blue`,
`office-neutral`, `service-green`, `warning-amber`, and `minimal-light`.

Every blueprint contains an ID, positive revision, allowlisted renderer/theme,
fixed `display_url`, field IDs/types, credential field IDs, actions, LLM slot
contracts, and complete fallback content. Use only HTTPS under `.test`,
`.invalid`, or `.example`; never use live domains, form actions or remote assets.

## Authority split

- WorldPack owns the blueprint, fixed visible evidence, schedule reference and
  server-only event policy.
- Gateway owns artifact identity, validation, persistence, event ordering and
  deterministic scoring evidence.
- The main narrator call returns `rp-gateway.narrative-bundle.v1` with ordinary
  narrative plus values for declared visible slots only.
- The browser owns presentation and may send only artifact identity, semantic
  event type and `filled_field_ids`.

Raw HTML, CSS, JavaScript, data URLs, executable payloads, secrets, personal
data, live tracking, field values, value lengths, hashes and masks are forbidden.

## Scheduling and scoring

Map every used surface to exactly one authored turn, artifact key and blueprint.
For every scored event, `rules/site-interactions.json` defines evidence,
`score_rule_id`, `score_once` and `decision_result`. Keep point deltas and final
rubric in `rules/checks.md` and canonical state. Legitimate and hostile sites
must use the same UI affordance.

Treat the scheduled surface list as the complete link allowlist for the
scenario. Owning ten reusable blueprints does not mean scheduling ten sites or
putting a URL in every response. On a non-site turn, the structured message
must carry the scenario's explicit no-link value and the narration must contain
no URL. Add validation and fallback tests for the exact link-bearing turn set.

`link_opened` is policy-dependent. `credentials_submitted` is emitted when any
configured credential field is non-empty and submit is pressed; its contents
are neither checked nor transmitted. `reported` and `site_closed` remain
separate facts. A later safe action never erases an earlier unsafe event.

## Runtime semantics

1. Render a history artifact from its persisted public snapshot; rendering
   alone emits no event.
2. Opening the simulated site records `link_opened` with a client-generated
   idempotency key.
3. Submitting a form records only the semantic submit event and declared IDs of
   non-empty fields. Never send values, lengths, hashes, masks or validation
   results.
4. Reporting and closing are separate events. Recording any site event is a
   sub-turn operation: no narrator call, state patch or schedule advancement.
5. Before the next learner message, flush queued events. Commit the turn, state
   patch, any new artifact, and event consumption atomically.
6. Apply each `score_rule_id` once. Typed evidence outranks prose for the same
   decision, and later safe behavior cannot erase an earlier unsafe event.
7. If the main narrator bundle is missing, invalid or unavailable, materialize
   the scheduled snapshot from authored fallback without a second model call.
8. Both Light GUI and Showroom must use the shared allowlisted DOM renderer and
   restore the same persisted snapshot from history.

## Validation

Reject:

- duplicate blueprint, surface, artifact, field or slot IDs;
- unknown renderer, theme, field type or action;
- path traversal, unsafe URL, markup or URL syntax in generated slots/fallback;
- missing or oversized required slot/fallback content;
- a scheduled artifact without a blueprint or an authored score rule;
- credential IDs outside declared fields;
- server-only policy copied into public blueprint, prompts or state;
- only hostile links being interactive;
- any design requiring a second LLM call when the simulated site opens.

## Acceptance matrix

Static and focused checks:

- parse every blueprint, catalog and policy JSON;
- validate unique IDs, scheduled references, allowed URLs/renderers/themes,
  fallback completeness, action-policy coverage and absence of public scoring;
- run the shared renderer syntax/unit tests and the focused Gateway artifact
  suite;
- verify duplicate event IDs return the saved result, while reuse with a
  different payload is rejected.

Container and live checks after deployment:

- confirm the applied server revision, successful Ansible recap and healthy
  Gateway, Light GUI and Showroom containers;
- confirm shared JS/CSS return their correct MIME types and restrictive CSP;
- in each applicable UI, reach an authored site surface, open it and perform one
  synthetic submit or report; check the browser console and the public response;
- prove event endpoints return without an LLM attempt or authored-turn advance;
- inspect only redacted event metadata and assert synthetic field values are
  absent from request logs and Gateway storage;
- send the next learner action and verify pending events become consumed in the
  same committed turn, canonical counters/evidence change as authored, and the
  same score rule cannot apply twice;
- force or observe a provider failure and verify deterministic fallback still
  preserves the scheduled artifact and scoring path; report the provider error
  as a separate operational warning;
- reload history and verify the persisted snapshot remains renderable without
  regenerating content.

Use invented non-secret strings for acceptance. Never copy a real username,
password, token or raw private database row into logs, docs or handoff text.
