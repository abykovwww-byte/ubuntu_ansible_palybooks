# USE Framework на abykovserv

## Состояние доставки

Приложение управляется элементом `use-framework` роли `apps`. IaC клонирует ровно commit `5e82b8389f2331e7b8383e8728cc7c215fb35b43`, создаёт server-only `.env`, собирает образ и запускает Compose. По решению владельца сервис публикуется как `0.0.0.0:8765`, включая LAN и Tailscale; nginx и DNS не создаются.

Данные хранятся отдельно от checkout:

| Путь | Назначение |
|---|---|
| `/srv/apps/use-framework` | закреплённый исходный код и Compose |
| `/srv/app-data/use-framework` | канонический YAML, snapshots, mapping-профили, отчёты и SQLite |
| `/srv/app-data/use-framework/import/ns1-assets.xlsx` | приватная серверная копия сохранённого реестра НС1; не хранится в Git |
| `/srv/backups/use-framework` | архивы backup/restore |

GitHub-репозиторий закрытый. До первого apply значение `use_framework_github_token` должно быть задано только в `/etc/ansible/local-overrides.yml`. Токен в Git и в этот документ не добавляется. Для этого app включён изолированный режим `repo_version_is_commit`: clone выполняется без checkout, затем роль делает fetch/reset на полный закреплённый SHA. Поведение остальных branch/tag-приложений не меняется.

Ansible preflight требует приватный `ns1-assets.xlsx` до пересборки контейнера. Application entry point импортирует его через минимальный production mapping, не сохраняющий свободные пользовательские поля, и переносит прежний `example.test` snapshot в quarantine. Если приватный источник отсутствует, ложная тестовая фикстура не обслуживается. Токен операций записи генерируется локально на сервере в `/etc/ansible/use-framework-api-token`, передаётся только через runtime `.env` и не хранится в Git.

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
curl --fail 'http://192.168.1.88:8765/api/graph?event=ns1&view=scenario'
docker exec use-framework nsgraph --root /data/canonical validate
docker exec use-framework /app/docker/backup.sh
```

Открыть `http://192.168.1.88:8765` из LAN и `http://100.117.52.16:8765` через Tailscale и проверить НС1. Представление по умолчанию `view=landscape`: 21 вершина, 20 рёбер, `scenario_projection=0`. Явное `view=scenario`: 35 вершин, 43 ребра, в том числе 13 вершин и 23 ребра `scenario_projection`. Дополнительно проверить: 63 хоста, `example.test=0`, source id `ns1-hosts`, `captured_at` равен UTC-дате серверного XLSX и `stale=0`; весь реестр EDR `33 yes / 5 no / 25 unknown`, точный селектор терминальных серверов — `17 yes + 2 unknown`, `rd.bc` — `EDR no`, `Sysmon/Security yes`, `stale=false`.

## Обновление и rollback

Обновление выполняется заменой `use_framework_repo_version` на полный принятый SHA приложения. Перед изменением модели создать backup. Revision `5e82b8389f2331e7b8383e8728cc7c215fb35b43` добавляет типизированную аттестацию источников, потолок статуса доказательств и разделение фактического ландшафта со сценарными проекциями, а также принимает точный исторический SHA управляемого `hosts.yaml`, оставшийся на production от revision `1544c3a`. Представление ландшафта остаётся режимом API по умолчанию, а сгенерированные сценарные пути доступны только через явный `view=scenario` и формируют отдельные заявки качества. Fail-closed миграции принимают только известные SHA-256 persistent-модели, сначала валидируют временную канонику и сохраняют первоначальные originals в `/data/canonical-migrations/<migration-id>/`; последующие принятые upgrade сохраняют существующий backup неизменным, а неизвестное локальное расхождение останавливает запуск без перезаписи. Bootstrap вычисляет `captured_at` по серверному XLSX и архивирует legacy snapshots вне активного набора. Rollback кода — вернуть предыдущий SHA и повторить apply; rollback данных — остановить сервис, выполнить `/app/docker/restore.sh` для архива соответствующей revision и снова запустить apply. Производный SQLite при старте пересобирается из восстановленного YAML.
