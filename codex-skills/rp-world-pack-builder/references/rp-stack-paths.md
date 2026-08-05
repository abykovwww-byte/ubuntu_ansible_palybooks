# RP Stack Paths

## Local repository

```text
$env:USERPROFILE/Documents/Tavern/ubuntu_ansible_palybooks
roles/apps/files/rp-stack/
roles/apps/files/rp-stack/worldpacks/<world-slug>/
roles/apps/files/rp-stack/state/schema.json
roles/apps/files/rp-stack/rp-gateway/
roles/apps/files/rp-stack/rp-light-gui/
inventories/local/group_vars/server.yml
```

## Server

```text
host: 192.168.1.88
ssh user: abykov
checkout: /opt/ubuntu_ansible_palybooks
runtime: /srv/apps/rp-stack
persistent Gateway data: /srv/app-data/rp-stack/gateway
party state: /srv/apps/rp-stack/state/parties
```

## URLs

```text
Light GUI: http://192.168.1.88:8010
Gateway: internal http://rp-gateway:8088
```

## Validation

```powershell
Set-Location "$env:USERPROFILE/Documents/Tavern/ubuntu_ansible_palybooks/roles/apps/files/rp-stack"
python scripts/validate-state.py --state worldpacks/<slug>/state-seed.json --schema state/schema.json
```

```bash
cd /srv/apps/rp-stack
docker compose run --rm rp-gateway pytest
docker inspect --format='{{.State.Health.Status}}' rp-stack-gateway
docker inspect --format='{{.State.Health.Status}}' rp-stack-light-gui
curl -fsS http://192.168.1.88:8010/api/worldpacks
```
