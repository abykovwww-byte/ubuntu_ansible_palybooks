# RP Stack application instructions

- Preserve the RP boundary `browser -> RP Light GUI -> RP Gateway -> RP SQLite/WorldPacks/providers`. Showroom and Training use the separate `tavern-awareness-showroom` application, Gateway, WorldPacks, and SQLite database.
- RP Gateway is the authority for RP state, permissions, idempotency, memory, and provider fallback. It must reject `training` and must not serve Showroom routes or training artifacts.
- Browser-issued IDs, revisions, scores, URLs, and artifact facts are untrusted until Gateway validation.
- Add, change, or remove a Gateway contract through its production schema/loader and actually affected clients. Add automated coverage only for player-visible behavior, authorization, data loss or mixing, atomicity/isolation/idempotency/recovery, or a real provider/storage boundary; remove dedicated checks with the superseded mechanism. Update the Wiki only for changed external behavior or operator workflow.
- Run these offline legacy WorldPack/training checks only while validating the retained rollback source or the explicit O2 deletion; they are not an active RP application path or a general RP gate:
  - `python3 scripts/validate-state.py --state worldpacks/<slug>/state-seed.json --schema state/schema.json` for every WorldPack state seed;
  - `python3 scripts/validate-training-runtime.py --worldpacks worldpacks`
  - `python3 scripts/test-state-workflow.py`
  - `python3 scripts/test-check-workflow.py`
- RP provider canaries must be bounded, explicitly confirmed, and executed through `/api/admin/autotests`, which creates an isolated checkpoint branch. Training provider acceptance belongs to the standalone application.
- Do not infer provider success from fallback-visible narrative. Verify the run status, completed turns, fallback turns, and relevant Gateway evidence.
- Never launch the stack locally. After deployment, the authoritative test is `docker compose run --rm rp-gateway pytest` under `/srv/apps/rp-stack`.
