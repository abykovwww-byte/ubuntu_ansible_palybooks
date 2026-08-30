# RP Stack

RP Stack is the LAN-only roleplay application managed by this IaC repository.

```text
Browser
  -> Light GUI on http://192.168.1.88:8010
  -> party-scoped /api proxy
  -> RP Gateway
  -> provider APIs or the optional local model runner
```

Gateway owns RP authentication, world packs, player characters, model profiles,
party state, history, memory chapters, and deterministic RP checks. It starts in
`SCENARIO_TYPE=rp`, exposes only RP WorldPacks and parties, and rejects training
creation or resumption. The browser stores only its active session and party
preference.

Showroom and Awareness training run as the separate training-only
`tavern-awareness-showroom` application on `http://192.168.1.88:8011`, with its
own Gateway, WorldPacks, database, backup, and provider path. This repository
pins and deploys that application but is not its application-source authority.
The owner selected a zero-length rollback window: the legacy Showroom,
Awareness WorldPacks, and training runtime are removed from `rp-stack/` in the
same cutover delivery. Legacy SQLite rows, state, and backups remain preserved
and quarantined from the RP runtime.

**Service model / Служебная модель** is the single administrator-selected LLM
for the whole RP Stack. It serves long-term memory, world-state change drafts,
and character generation for every current and future party. Party narrator
models remain independent. User BYOK credentials are scoped to exactly one
party and are never used by the service model.

Active cloud narrator routes are Gemini and OpenRouter. The service model uses
only an explicitly selected local or OpenRouter route and never changes provider
when the local runner is unavailable. Retired provider/profile/log rows remain
readable for history, but cannot be selected for new or continuing runtime work.

Revision-8 RP WorldPacks may declare reviewed `lore-cards/*.json`. Gateway copies
them into a new party without a model call, retrieves them only by whole
title/keyword matches from the current-plus-three-turn scan, and records the
exact raised IDs in turn metadata. A player-triggered Lore Card draft uses one
bounded stack-key OpenRouter call and is persisted only after explicit confirm.

Candidate revision-9 RP adds a separate confirmed GM correction path. Bounded
local Gemma calls may classify and draft only an edit of an existing target;
Gateway alone commits it without advancing the scene and keeps a typed overlay
until the affected OpenRouter memory section absorbs authority `user`. This
source capability is not an activation or live-runtime claim.

Candidate revision-10 RP may declare an authored `world-clock.json`. Exact local
Gemma estimates only elapsed time from the last committed turn; Gateway applies
cancelable authored events atomically as durable world facts or existing Lore
Card toggles. The narrator and Light GUI receive a bounded one-shot event plus
nearest horizon. Observed revision remains 8 until a separate activation and
live verification slice.

## Runtime paths

```text
/srv/apps/rp-stack
/srv/apps/rp-stack/worldpacks
/srv/apps/rp-stack/state
/srv/app-data/rp-stack/gateway/rp_gateway.db
/srv/backups/rp-stack
```

## Operations

```bash
cd /srv/apps/rp-stack
docker compose up -d --build --remove-orphans
docker compose ps
docker compose logs --tail=100 rp-gateway rp-light-gui
docker compose run --rm rp-gateway pytest
bash scripts/backup.sh
```

Provider keys are configured only in `/etc/ansible/local-overrides.yml` on the
server. Never store them in Git or enter them in the browser.

## Deployment

Commit on a `codex/` branch or in an isolated worktree, push only that branch,
open a non-draft PR, and merge it into `main` after CI is green. Direct pushes
to `main` are prohibited. Then apply the server checkout through:

```bash
sudo systemctl start ansible-local-apply.service
```

Do not maintain long-lived manual edits under `/srv/apps/rp-stack`.
