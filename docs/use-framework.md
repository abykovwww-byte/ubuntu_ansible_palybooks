# USE Framework на abykovserv

## Состояние доставки

Приложение управляется элементом `use-framework` роли `apps`. IaC клонирует ровно commit `db16315eab1e14062f2f974f49b341411767aa6d`, создаёт server-only `.env`, собирает образ и запускает Compose. Сервис публикуется только на `127.0.0.1:8765`; nginx, DNS и LAN/WAN listener не создаются.

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
curl --fail http://127.0.0.1:8765/health
curl --fail 'http://127.0.0.1:8765/api/graph?event=ns1'
docker exec use-framework nsgraph --root /data/canonical validate
docker exec use-framework /app/docker/backup.sh
```

Через SSH tunnel открыть `http://127.0.0.1:8765` и проверить НС1: 16 вершин, 18 рёбер, 63 хоста, EDR `17 yes + 2 unknown`, `rd.bc` — `no + stale`. Снаружи сервера порт `8765` отвечать не должен.

## Обновление и rollback

Обновление выполняется заменой `use_framework_repo_version` на полный принятый SHA приложения. Перед изменением модели создать backup. Rollback кода — вернуть предыдущий SHA и повторить apply; rollback данных — остановить сервис, выполнить `/app/docker/restore.sh` для архива соответствующей revision и снова запустить apply. Производный SQLite при старте пересобирается из восстановленного YAML.
