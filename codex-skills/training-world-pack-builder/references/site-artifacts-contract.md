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

`link_opened` is policy-dependent. `credentials_submitted` is emitted when any
configured credential field is non-empty and submit is pressed; its contents
are neither checked nor transmitted. `reported` and `site_closed` remain
separate facts. A later safe action never erases an earlier unsafe event.

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
