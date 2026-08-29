# RP Stack Paths

Verify paths with `rg --files` before editing. Work from the Git/IaC repository
root in an isolated worktree; Windows is only the editing and offline validation
environment.

## Decision 043 Source

```text
roles/apps/files/rp-stack/worldpacks/day-watch-moscow-v2/world.json
roles/apps/files/rp-stack/worldpacks/day-watch-moscow-v2/scenario-presets/*.json
roles/apps/files/rp-stack/worldpacks/day-watch-moscow-v2/<referenced-assets>
```

Only `day-watch-moscow-v2` is supported by the current production loader.

## Executable Contract

```text
roles/apps/files/rp-stack/rp-gateway/app/rp/content.py
roles/apps/files/rp-stack/rp-gateway/app/rp/schema.py
roles/apps/files/rp-stack/rp-gateway/app/rp/turn_engine.py
roles/apps/files/rp-stack/rp-gateway/tests/test_rp_world_scenario.py
roles/apps/files/rp-stack/rp-gateway/tests/test_rp_turn_engine.py
```

`app/rp/content.py` is the sole executable schema and loader truth for
`world.json`, `scenario-presets/*.json`, and materialized snapshots. The
repository validator intentionally does not mirror this format.

## Offline Validation

From the repository root:

```powershell
$python = "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
& $python scripts\validate-repository.py
Push-Location roles\apps\files\rp-stack\rp-gateway
& $python -m pytest -q tests\test_rp_world_scenario.py tests\test_rp_turn_engine.py
Pop-Location
```

The focused tests are the format/materialization evidence. Repository validation
covers the remaining repository contracts and is not a substitute for them.

## Current Runtime

Runtime target:

```text
host: 192.168.1.88
ssh user: abykov
server checkout: /opt/ubuntu_ansible_palybooks
runtime app: /srv/apps/rp-stack
persistent data: /srv/app-data/rp-stack
```

Existing runtime paths include:

```text
/srv/apps/rp-stack/worldpacks/
/srv/app-data/rp-stack/gateway/rp_gateway.db
```

Do not create or edit `/opt` or `/srv` paths on Windows. Do not manually copy
the new definitions to the server.

The Decision 043 World/Scenario source is not currently consumed by Light GUI,
its API, or the deployed Gateway. Therefore server files, container health, and
the existing `/api/worldpacks` response cannot prove this slice works live.

## Later Deployment

Deployment is owned by `abykovserv-iac-deploy`. Use it only after a later task
integrates the new loader with the product surface and explicitly authorizes
cutover. Then verify the applied Git revision, containers, HTTP discovery,
actual party creation, persisted World/Scenario snapshots, and a real turn.
