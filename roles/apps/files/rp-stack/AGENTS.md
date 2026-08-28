# RP Stack application instructions

- Preserve the boundary `browser -> Light GUI/Showroom -> Gateway -> SQLite/WorldPacks/providers`.
- Gateway is the authority for state, permissions, idempotency, scoring, training events, datasets, memory, and provider fallback.
- Browser-issued IDs, revisions, scores, URLs, and artifact facts are untrusted until Gateway validation.
- Add, change, or remove a Gateway contract through its production schema/loader and actually affected clients. Add automated coverage only for player-visible behavior, authorization, data loss or mixing, atomicity/isolation/idempotency/recovery, or a real provider/storage boundary; remove dedicated checks with the superseded mechanism. Update the Wiki only for changed external behavior or operator workflow.
- Run these offline legacy WorldPack/training checks only when that surface changes; they are not a general RP gate, and Awareness keeps its existing deterministic requirements:
  - `python3 scripts/validate-state.py --state worldpacks/<slug>/state-seed.json --schema state/schema.json` for every WorldPack state seed;
  - `python3 scripts/validate-training-runtime.py --worldpacks worldpacks`
  - `python3 scripts/test-state-workflow.py`
  - `python3 scripts/test-check-workflow.py`
- Provider canaries must be bounded, explicitly confirmed, and executed through `/api/admin/autotests`, which creates an isolated checkpoint branch.
- Do not infer provider success from fallback-visible narrative. Verify the run status, completed turns, fallback turns, and relevant Gateway evidence.
- Never launch the stack locally. After deployment, the authoritative test is `docker compose run --rm rp-gateway pytest` under `/srv/apps/rp-stack`.
