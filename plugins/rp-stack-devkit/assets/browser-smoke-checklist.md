# RP Stack browser smoke checklist

Use the Codex Browser skill against the deployed Light GUI. Do not launch a local server.

Record the deployed server revision and exact time, then verify:

1. Login completes and the authenticated party list is visible.
2. A selected party shows the expected WorldPack, scenario mode, history, and state version from Gateway responses.
3. Sending one safe test turn creates exactly one new committed turn; a repeated client request does not duplicate it.
4. Provider-visible prose is not counted as success until the corresponding API response, run status, and fallback evidence agree.
5. Training artifacts render only server-issued exact URLs; typed interactions are reflected only after Gateway acceptance.
6. Showroom remains isolated from authenticated party state and uses its own run identity.
7. Browser console and failed network requests contain no new application error.

For a UI-free change, mark browser smoke `not required` with the reason. Never claim browser verification from HTTP-only evidence.
