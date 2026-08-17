# USE Framework на abykovserv

## Состояние доставки

Приложение управляется элементом `use-framework` роли `apps`. IaC клонирует ровно commit `e7a1d1c5ee90d714ec6863aacc9206a4d7d679f7`, создаёт server-only `.env`, собирает образ и запускает Compose. По решению владельца сервис публикуется как `0.0.0.0:8765`, включая LAN и Tailscale; nginx и DNS не создаются.

Данные хранятся отдельно от checkout:

| Путь | Назначение |
|---|---|
| `/srv/apps/use-framework` | закреплённый исходный код и Compose |
| `/srv/app-data/use-framework` | канонический YAML, snapshots, mapping-профили, отчёты и SQLite |
| `/srv/app-data/use-framework/import/ns1-assets.xlsx` | приватная серверная копия сохранённого реестра НС1; не хранится в Git |
| `/srv/app-data/use-framework/import/ns1-ad-accounts.json` | приватный нормализованный AD-срез 4 учётных записей; не хранится в Git |
| `/srv/app-data/use-framework/import/ns1-ad-computers.json` | приватный нормализованный AD-срез 14 компьютеров; не хранится в Git |
| `/srv/backups/use-framework` | архивы backup/restore |

GitHub-репозиторий закрытый. До первого apply значение `use_framework_github_token` должно быть задано только в `/etc/ansible/local-overrides.yml`. Токен в Git и в этот документ не добавляется. Для этого app включён изолированный режим `repo_version_is_commit`: clone выполняется без checkout, затем роль делает fetch/reset на полный закреплённый SHA. Поведение остальных branch/tag-приложений не меняется.

Ansible preflight требует приватный `ns1-assets.xlsx` до пересборки контейнера. Application entry point импортирует его через минимальный production mapping: сохраняет только нормализованные host/EDR-поля и переносит прежний `example.test` snapshot в quarantine. Если приватный источник отсутствует, ложная тестовая фикстура не обслуживается. AD JSON импортируются отдельно статическими production-профилями с `preserve_free_fields: false`; они остаются inventory-only до появления точного подтверждённого binding. Токен операций записи генерируется локально на сервере в `/etc/ansible/use-framework-api-token`, передаётся только через runtime `.env` и не хранится в Git.

## Apply владельцем сервера

После merge в `main` прошедшего зелёный CI non-draft pull request с IaC:

```text
sudo systemctl start ansible-local-apply.service
sudo systemctl status ansible-local-apply.service --no-pager
sudo journalctl -u ansible-local-apply.service -n 200 --no-pager
```

Повторный запуск должен быть успешным и не пересобирать приложение без изменения Git revision, `.env` или принудительного флага роли.

После первого успешного apply этого revision выполнить read-only preview двух AD-срезов, проверить `instances: 4` и `instances: 14`, затем повторить те же команды с `--apply` и штатно перезапустить контейнер, чтобы API перечитал snapshots:

```text
docker exec use-framework nsgraph --root /data inventory import /data/import/ns1-ad-accounts.json --profile /app/inventory/mappings/ns1-ad-accounts-production.yaml --source-id ns1-ad-accounts --captured-at 2026-08-14 --freshness-days 30
docker exec use-framework nsgraph --root /data inventory import /data/import/ns1-ad-computers.json --profile /app/inventory/mappings/ns1-ad-computers-production.yaml --source-id ns1-ad-computers --captured-at 2026-08-14 --freshness-days 30
docker exec use-framework nsgraph --root /data inventory import /data/import/ns1-ad-accounts.json --profile /app/inventory/mappings/ns1-ad-accounts-production.yaml --source-id ns1-ad-accounts --captured-at 2026-08-14 --freshness-days 30 --apply
docker exec use-framework nsgraph --root /data inventory import /data/import/ns1-ad-computers.json --profile /app/inventory/mappings/ns1-ad-computers-production.yaml --source-id ns1-ad-computers --captured-at 2026-08-14 --freshness-days 30 --apply
docker restart use-framework
```

## Live-приёмка

```text
docker ps --filter name=use-framework
curl --fail http://192.168.1.88:8765/health
curl --fail http://100.117.52.16:8765/health
curl --fail 'http://192.168.1.88:8765/api/graph?event=ns1'
curl --fail 'http://192.168.1.88:8765/api/graph?event=ns1&view=scenario'
curl --fail 'http://192.168.1.88:8765/api/instances?type=host'
curl --fail 'http://192.168.1.88:8765/api/snapshots'
curl --fail 'http://192.168.1.88:8765/api/quality'
docker exec use-framework nsgraph --root /data/canonical validate
docker exec use-framework /app/docker/backup.sh
```

Открыть `http://192.168.1.88:8765` из LAN и `http://100.117.52.16:8765` через Tailscale и проверить НС1. Представление API по умолчанию `view=landscape`: 21 вершина, 19 рёбер, `scenario_projection=0`; неподтверждённое e1 доступно только в явном `view=scenario`, где сохраняются 34 вершины и 43 ребра. Общая repository health остаётся 35/43: inventory-only `host_ts1c_bc` с `event_scope: []` не входит ни в одно event-filtered представление. GUI-карточки и `/api/instances` обязаны использовать только latest snapshot каждого source, сохраняя старые snapshots как историю. Карточки показывают canonical attributes, identity и нормализованные instance fields с source/date; процессы `supplier-payment` и `payroll` подписаны как «Оплата поставщику» и «Выплата зарплаты».

Для текущих приватных источников ожидаются: 82 current host instances, `example.test=0`, source id `ns1-hosts`, `captured_at=2026-08-14`, `stale=0`; EDR `69 yes / 0 no / 13 unknown`, Sysmon `34 yes / 29 no / 19 unknown`, Security `40 yes / 23 no / 19 unknown`. `host_banking_app` должен иметь ровно одну привязку к `preo-sb-bc-01.bc.ptsecurity.com` / `10.0.57.33`; `dc3-bc-1capp-01` не объединяется с ним. `host_ts1c_bc` подтверждён как сервер тестирования 1С:ДиректБанк и остаётся inventory-only без выдуманного ребра. AD preview/apply даёт 4 account и 14 computer instances, также inventory-only. Assets заполняет карточку `sys_erp` только по exact match IR-30932; остальные найденные карточки не привязываются к вершинам без item-level identity/edge evidence.

## Обновление и rollback

Обновление выполняется заменой `use_framework_repo_version` на полный принятый SHA приложения. Перед изменением модели создать backup. Revision `e7a1d1c5ee90d714ec6863aacc9206a4d7d679f7` меняет только UI инвентаря: `host` и `ad_computer` отображаются одной логической карточкой при однозначном совпадении namespace и первого DNS-label; `bc.ptsecurity.com` остаётся отдельным namespace, коллизии не склеиваются, пустые поля скрыты, а непустой provenance раскрывается по запросу. AD, DNS, NetBox, EDR, collector и owner-поля показываются как отдельные source facets; исходные snapshots, API и каноническая модель не меняются, миграция не требуется. Revision сохраняет risk-based правила доставки из `e4e621903019af855cb0487191afad740470f375` и доказательный канон `caaa67346e456ff1bd042ff3e7cf80d9bf33f53b`: latest-per-source устраняет двойные current instances, production mappings сохраняют ограниченный набор EDR/AD-полей, а неподтверждённые связи остаются gaps.

Fail-closed миграции принимают только известные SHA-256 persistent-модели, сначала валидируют временную канонику и сохраняют первоначальные originals в `/data/canonical-migrations/<migration-id>/`; последующие принятые upgrade сохраняют существующий backup неизменным, а неизвестное локальное расхождение останавливает запуск без перезаписи. Bootstrap вычисляет `captured_at` по серверному XLSX и сохраняет snapshot history, но current API выбирает только latest snapshot каждого source. Rollback кода — вернуть предыдущий SHA и повторить apply; rollback данных — остановить сервис, выполнить `/app/docker/restore.sh` для архива соответствующей revision и снова запустить apply. Производный SQLite при старте пересобирается из восстановленного YAML.
