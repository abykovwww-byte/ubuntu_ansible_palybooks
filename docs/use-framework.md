# USE Framework на abykovserv

## Состояние доставки

Приложение управляется элементом `use-framework` роли `apps`. IaC клонирует ровно commit `e8876a1c83af0a0ffe1d17d09e6cb99448d7326a`, создаёт server-only `.env`, собирает образ и запускает Compose. По решению владельца сервис публикуется как `0.0.0.0:8765`, включая LAN и Tailscale; nginx и DNS не создаются.

Данные хранятся отдельно от checkout:

| Путь | Назначение |
|---|---|
| `/srv/apps/use-framework` | закреплённый исходный код и Compose |
| `/srv/app-data/use-framework` | канонический YAML, snapshots, mapping-профили, отчёты и SQLite |
| `/srv/backups/use-framework` | архивы backup/restore |

GitHub-репозиторий закрытый. До первого apply значение `use_framework_github_token` должно быть задано только в `/etc/ansible/local-overrides.yml`. Токен в Git и в этот документ не добавляется. Для этого app включён изолированный режим `repo_version_is_commit`: clone выполняется без checkout, затем роль делает fetch/reset на полный закреплённый SHA. Поведение остальных branch/tag-приложений не меняется.

## Apply владельцем сервера

После push IaC:

```text
sudo systemctl start ansible-local-apply.service
sudo systemctl status ansible-local-apply.service --no-pager
sudo journalctl -u ansible-local-apply.service -n 200 --no-pager
```

Повторный запуск должен быть успешным и не пересобирать приложение без изменения Git revision, `.env` или принудительного флага роли.

## Live-приёмка

```text
docker ps --filter name=use-framework
curl --fail http://192.168.1.88:8765/health
curl --fail http://100.117.52.16:8765/health
curl --fail 'http://192.168.1.88:8765/api/graph?event=ns1'
docker exec use-framework nsgraph --root /data/canonical validate
docker exec use-framework /app/docker/backup.sh
```

Открыть `http://192.168.1.88:8765` из LAN и `http://100.117.52.16:8765` через Tailscale и проверить НС1: 16 вершин, 18 рёбер, 63 хоста, EDR `17 yes + 2 unknown`, `rd.bc` — `no + stale`.

## Обновление и rollback

Обновление выполняется заменой `use_framework_repo_version` на полный принятый SHA приложения. Перед изменением модели создать backup. Rollback кода — вернуть предыдущий SHA и повторить apply; rollback данных — остановить сервис, выполнить `/app/docker/restore.sh` для архива соответствующей revision и снова запустить apply. Производный SQLite при старте пересобирается из восстановленного YAML.
