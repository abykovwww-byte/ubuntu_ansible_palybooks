# RP Stack Paths

Verify these with `rg --files` before editing.

## Local Editing Repo

Windows is only the Git/IaC editing and validation environment:

```text
$env:USERPROFILE/Documents/Tavern/ubuntu_ansible_palybooks
```

Important paths:

```text
roles/apps/files/rp-stack/
roles/apps/files/rp-stack/worldpacks/<world-slug>/
roles/apps/files/rp-stack/state/schema.json
roles/apps/files/rp-stack/rp-gateway/
roles/apps/files/rp-stack/rp-light-gui/
inventories/local/group_vars/server.yml
```

## Runtime Server

Runtime target:

```text
host: 192.168.1.88
ssh user: abykov
server checkout: /opt/ubuntu_ansible_palybooks
runtime app: /srv/apps/rp-stack
persistent data: /srv/app-data/rp-stack
```

World/runtime paths on the server:

```text
/srv/apps/rp-stack/worldpacks/
/srv/app-data/rp-stack/state/parties/
/srv/app-data/rp-stack/data/default-user/worlds/
/srv/app-data/rp-stack/data/default-user/QuickReplies/
/srv/app-data/rp-stack/gateway/rp_gateway.db
```

Do not create or edit these runtime paths on Windows.

## URLs

```text
Light GUI:    http://192.168.1.88:8010
SillyTavern:  http://192.168.1.88:8000
Gateway from SillyTavern container: http://rp-gateway:8088/v1
```

Light GUI API:

```text
GET  /api/worldpacks
POST /api/worldpacks/prompt
GET  /api/model-profiles
GET  /api/parties
POST /api/parties
DELETE /api/parties/{party_id}
GET  /api/parties/{party_id}/state
GET  /api/parties/{party_id}/history
GET  /api/parties/{party_id}/supervisor
POST /api/parties/{party_id}/messages
POST /api/parties/{party_id}/checks
POST /api/parties/{party_id}/world/instruct
```

## Validation

Local state validation from the RP-stack source root:

```powershell
Set-Location "$env:USERPROFILE/Documents/Tavern/ubuntu_ansible_palybooks/roles/apps/files/rp-stack"
python scripts/validate-state.py --state worldpacks/<slug>/state-seed.json --schema state/schema.json
```

Gateway tests when relevant:

```powershell
Set-Location "$env:USERPROFILE/Documents/Tavern/ubuntu_ansible_palybooks/roles/apps/files/rp-stack/rp-gateway"
python -m pytest tests
```

Server verification after deploy:

```bash
cd /srv/apps/rp-stack
docker compose ps
docker inspect --format='{{.State.Health.Status}}' rp-stack-gateway
docker inspect --format='{{.State.Health.Status}}' rp-stack-light-gui
docker inspect --format='{{.State.Health.Status}}' rp-stack-sillytavern
curl -fsS http://192.168.1.88:8010/api/worldpacks
curl -fsS http://192.168.1.88:8010/api/model-profiles
```

## Deploy

Deployment is owned by the `abykovserv-iac-deploy` skill. The route is
GitHub + server-side Ansible apply on `192.168.1.88`, not manual Windows copies
and not Windows-local `/srv` writes.
