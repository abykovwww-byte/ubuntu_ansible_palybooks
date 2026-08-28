# RP Stack application instructions

- Preserve the boundary `browser -> Light GUI/Showroom -> Gateway -> SQLite/WorldPacks/providers`.
- Gateway is the authority for state, permissions, idempotency, scoring, training events, datasets, memory, and provider fallback.
- Browser-issued IDs, revisions, scores, URLs, and artifact facts are untrusted until Gateway validation.
- Any new Gateway contract needs schema validation, focused pytest coverage, client compatibility checks, and Wiki updates.
- Scoped exception: implementation slices of Decision 043 use its focused,
  stage-specific gates. Validate the new World/Scenario contract through its
  production loader/schema; update clients and Wiki at their explicit cutover
  steps rather than on every internal slice. Do not run or extend legacy
  WorldPack/training workflows merely to certify the RP rebuild. This exception
  does not change Awareness requirements.
- Run offline contract validation from this directory before provider or browser checks:
  - `python3 scripts/validate-state.py --state worldpacks/<slug>/state-seed.json --schema state/schema.json` for every WorldPack state seed;
  - `python3 scripts/validate-training-runtime.py --worldpacks worldpacks`
  - `python3 scripts/test-state-workflow.py`
  - `python3 scripts/test-check-workflow.py`
- Provider canaries must be bounded, explicitly confirmed, and executed through `/api/admin/autotests`, which creates an isolated checkpoint branch.
- Do not infer provider success from fallback-visible narrative. Verify the run status, completed turns, fallback turns, and relevant Gateway evidence.
- Never launch the stack locally. After deployment, the authoritative test is `docker compose run --rm rp-gateway pytest` under `/srv/apps/rp-stack`.
