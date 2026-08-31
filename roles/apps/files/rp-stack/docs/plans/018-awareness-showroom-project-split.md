# Plan 018: вынести Awareness и Showroom в training-only project

Date: 2026-08-27 · Owner decision: accepted · Delivery status: zero-window
C1/O2/N3 applied; shape/HTTP/browser/SQLite smoke passed, full training live
acceptance pending.

Rollback window: `0`. Владелец 2026-08-30 явно разрешил немедленное удаление
legacy Showroom/training source без cold standby. SQLite rows/tables, state и
backups этим разрешением не удаляются.

## Результат

После cutover существуют два независимо поставляемых приложения:

| Приложение | Git authority | UI | Runtime mode | Данные |
|---|---|---|---|---|
| RP Stack | `abykovwww-byte/ubuntu_ansible_palybooks` + встроенный RP source | Light GUI `:8010` | только `rp` | `/srv/app-data/rp-stack` |
| Awareness Showroom | `abykovwww-byte/tavern-awareness-showroom` | Showroom `:8011` | только `training` | `/srv/app-data/awareness-showroom` |

`ubuntu_ansible_palybooks` остаётся authority для server topology и хранит
полный commit pin отдельного public application repository. Ansible клонирует
его анонимно по HTTPS без GitHub token; код нового приложения живёт только в
его repository и не копируется из старого дерева.

## Владение компонентами

В новый project переходят:

- `rp-showcase-gui` и его admin/public flows;
- `worldpacks/awareness` и `worldpacks/awareness-one-day`;
- training-only Gateway с `ShowroomStore`, generic training runtime,
  artifacts, workspace, capability gates и required shared UI renderer;
- auth, party/state/history/provider plumbing в объёме, реально необходимом
  training run;
- собственные Compose, CI, SQLite, JSON state, covers, cookies, network и
  backup contract.

В RP Stack остаются:

- Light GUI, RP parties, RP WorldPacks и RP contracts;
- RP rule/narrative pipeline, relationships, world clock, character retrieval,
  RP story memory, Turn Trace и RP evals;
- server IaC, ports, local overrides, backup orchestration и commit pin нового
  project;
- существующая RP SQLite без переписывания: старые Showroom/training строки
  остаются нетронутыми, но production RP Gateway их не публикует и не исполняет.

В одной zero-window поставке с C1 из RP source удаляются Showroom UI, Awareness
WorldPacks и training-only runtime/API/tests. Таблицы не удаляются, старая БД,
state и backups не переписываются.

## Git migration

1. Через GitHub API зафиксировать exact source `main` SHA.
2. Из независимого clone выполнить history-preserving split:

   ```text
   git subtree split --prefix=roles/apps/files/rp-stack <SOURCE_BASE_SHA>
   ```

3. Создать public `abykovwww-byte/tavern-awareness-showroom`, опубликовать
   split history и записать `SOURCE_BASE_SHA`/`SUBTREE_HEAD` в provenance.
4. После bootstrap все изменения нового project идут только через
   `codex/* -> non-draft PR -> green CI -> merge`.
5. IaC ссылается только на полный merge commit SHA нового project. Floating
   branch/tag как deploy pin запрещён.

Bootstrap сначала переносит весь `rp-stack` subtree, чтобы не потерять историю
и скрытую зависимость. Затем отдельные PR включают process guards и удаляют
RP-only code. Это не создаёт общей библиотеки и не требует синхронизации fork.

## Runtime isolation

Training Gateway обязан fail closed:

- startup разрешён только при process mode `training`;
- create/start/message/resume/background jobs отклоняют persisted party или run
  чужого режима до provider call и до записи в state/history;
- публикуются только training-compatible WorldPacks;
- prompt-generated worlds и `/v1/chat/completions` отключены;
- Showroom создаёт только fixed `training` payload и не показывает выбор RP;
- invalid create payload возвращает `422`, persisted wrong-mode resource —
  `409`;
- регрессии доказывают отсутствие новых DB rows и provider calls при отказе.

RP Gateway получает симметричные guards для `rp`. `incident-50` остаётся только
в RP project. В training project разрешены только Awareness packs и будущие
WorldPack-owned deterministic training scenarios.

## Данные и identity

Новый project стартует с пустой SQLite. В Git-каталог нового project
переносятся только настройки опубликованных сценариев:

- title, description, status, sort order;
- WorldPack slug;
- model profile, сопоставленный по `(provider, base_url, model)`, а не legacy ID;
- leaderboard presentation flags;
- independent links/workspace capability flags;
- cover как проверяемый файл внутри Git-каталога либо явный `null`.

`SHOWROOM_CATALOG_PATH=/app/configs/showroom/scenarios.json` включает
fail-closed startup reconciliation. Стабильный catalog `key` создаёт
`scenario_catalog_<key>`; повторный startup обновляет только
фактически изменившиеся поля/обложку. Нулевое или неоднозначное
совпадение model tuple останавливает startup. Импорт не удаляет
необъявленные DB-сценарии и не изменяет run data.

Не переносятся visitors, runs, internal parties, turns, state versions,
training event history, sessions, users, provider keys, BYOK, feedback и
leaderboard rows. В UI после cutover `Мои прохождения` и рейтинг начинаются с
нуля, admin входит заново, scenario/run IDs меняются. Старые результаты доступны
только в legacy snapshot/backup старого project.

Cookie names разделены, потому что разные host ports не изолируют cookies:

- `awareness_gateway_session`;
- `awareness_showroom_visitor`.

## Delivery waves

### D0 — decision freeze

Обновить Decision 018, этот plan и Wiki. Никаких runtime-изменений и apply.

### N0/N1 — new repository bootstrap

Создать standalone public project из subtree history. Добавить `AGENTS.md`, README,
provenance, standalone Compose/env example, CI и migration documentation.
Baseline должен собираться до удаления code.

### N2 — training-only enforcement

Включить process/resource mode guards, отдельные paths/cookies/network и
training-only Showroom controls. Запустить focused Gateway/JS tests и full CI.

### N3 — RP prune

Это отдельный application PR до финализации IaC pin. Он удаляет RP-only UI,
WorldPacks, services, endpoints и tests из нового training repository. После
его green merge IaC закрепляет фактический merge SHA. Для каждого кандидата
нужно доказать отсутствие training consumer; косметическое переименование
сохраняемых классов и каталогов не требуется.

### I1 — shadow deploy

IaC клонирует новый public repository анонимно по HTTPS без GitHub token на
exact commit в
`/srv/apps/awareness-showroom`, persistent data — в
`/srv/app-data/awareness-showroom`, backup — в
`/srv/backups/awareness-showroom`. Showroom временно публикуется на `18011`,
старый `8011` продолжает обслуживать пользователей.

I1 применён: отдельные containers, loopback `:18011`, SQLite/state/covers
и backup paths подняты. Пустая витрина доказала топологию,
но не training acceptance.

### I2 — source-owned scenario catalog

Published training configs и обложки поставлены из отдельного application
repository. IaC пинит exact commit и включает catalog path; applied startup
показал пять сценариев на `:8011`. Это подтверждает каталог, но не заменяет
полное живое прохождение обоих курсов.

### O1 — freeze and drain

На старом Showroom запретить создание новых сценариев/runs, дождаться
завершения нужных активных runs и read-only сверить legacy published
config с Git-каталогом. Любой delta идёт обычным application PR; runtime
admin mutation не становится authority.

Владелец явно снял перенос истории, visitors/runs и ожидание старых активных
прохождений как блокеры C1. Поэтому drain и migration не выполняются: новый
project начинает с собственной БД, а C1 не выполняет destructive migration или
deletion старой RP SQLite. Logical before/after snapshot остаётся acceptance
gate.

### C1 — cutover

Переключить `8011` на новый project, сделать старый Gateway RP-only, убрать
старый Showroom из RP Compose. `8010` остаётся RP Light GUI. Никакого cold
standby, dual-write или proxy между Gateway.

C1 применён с exact application pin
`67244432659f6c25a268cbf788a8fa3af0f5b52f`: новый Showroom занял
LAN-only `192.168.1.88:8011`, старый RP Gateway запускается только с
`SCENARIO_TYPE=rp`, а `rp-showcase-gui` отсутствует. Apply
`83a90eda9a2465567028e7e58446378e0b10ccc2` завершился с `failed=0`:
оба проекта healthy, `:8010`/`:8011` отвечают `200`, browser console чистая,
listener `:18011` и старые RP training source paths отсутствуют. Обе SQLite
прошли integrity/FK smoke. Backup/test restore и сквозная training/RP приёмка
ещё не подтверждены.

### O2 — cleanup

Выполняется в той же поставке, что C1: удалить из RP source training/Showroom
code, старый service и неиспользуемые source declarations. Так как
`ansible.builtin.copy` не удаляет stale-файлы, роль удаляет их точные source
paths на сервере до
финального `docker compose up -d --build`. Legacy DB, state и backups
сохраняются согласно retention
policy: O2 не удаляет SQLite rows/tables, не запускает `delete_user_data` и не
выполняет destructive SQL migration.

Cleanup allowlist относительно `/srv/apps/rp-stack` ограничен 15 paths:

```text
rp-showcase-gui
worldpacks/awareness
worldpacks/awareness-one-day
ui-shared
scripts/validate-training-runtime.py
rp-gateway/app/services/showroom.py
rp-gateway/app/services/training_artifacts.py
rp-gateway/app/services/training_capabilities.py
rp-gateway/app/services/training_runtime.py
rp-gateway/app/services/training_workspace.py
rp-gateway/tests/test_showroom_portal.py
rp-gateway/tests/test_training_artifacts.py
rp-gateway/tests/test_training_capabilities.py
rp-gateway/tests/test_training_runtime.py
rp-gateway/tests/test_awareness_one_day.py
```

После source copy Ansible read-only проверяет эти пути, добавляет `rp-stack` в
coordinated handoff и сохраняет retry-marker. Предварительный build обоих
проектов выполняется без изменения примонтированных legacy WorldPacks. Затем
останавливаются оба владельца порта, выполняется `state: absent`, а финальный
`docker compose up -d --build` собирает RP из уже очищенного source. Поэтому
prebuild прогревает зависимости и проверяет общий build path, но не является
сборкой exact финального RP context; при ошибке финальной clean-сборки действует
только fix-forward/retry по сохранённому marker.

Два ранее скопированных authored data artifacts
`/srv/app-data/rp-stack/default-user/worlds/Awareness.json` и
`/srv/app-data/rp-stack/default-user/worlds/Awareness. One day.json` также
сохраняются. RP inventory больше не публикует их из source, а активный RP
Gateway/Compose их не использует; это сохранённые данные, не cold standby.

## Acceptance

После C1/O2 apply обязательны:

- оба Awareness курса пройдены через Showroom до authored debrief;
- хотя бы один реальный narrator provider call подтверждён отдельно от
  deterministic fallback;
- artifact и workspace sub-turn events не вызывают LLM и атомарно влияют на
  следующий committed turn;
- итог берётся из `manifest.showroom_result`, совпадает с canonical state и
  leaderboard;
- visitor isolation не раскрывает internal `party_id`;
- restart/resume сохраняет активный run;
- SQLite `integrity_check` и `foreign_key_check`, backup и test restore успешны;
- rejected wrong-mode requests не меняют DB и не вызывают provider;
- реальная RP party на `:8010` продолжает ход после cutover;
- `:8011` работает из нового commit pin, browser console чистая;
- `rp-stack-showcase-gui`, старые source paths и shadow listener `:18011`
  отсутствуют;
- read-only логический snapshot legacy training/showroom rows в старой RP SQLite
  (schema/table presence, counts и checksum выбранных строк) не меняется на всём
  C1 acceptance; штатные записи новой RP-партии и file-level mtime/hash не
  используются как критерий неизменности legacy данных;
- source merge, Ansible apply и live verification сообщаются как отдельные
  стадии.

C1 acceptance требует полные прохождения обоих курсов на одном exact
application revision с `fallback_turns == 0`. В committed turns и audit не
допускаются `provider_fallback`/`llm_safe_fallback`; каждый provider-required
ход должен иметь успешный completed provider call и committed response с
`validator_valid=true`. `repaired=true` допустим только после успешного provider
repair при `fallback=false`. Более мягкая агрегатная fallback-метрика может быть
только post-acceptance SLO и не заменяет этот gate.

Rollback window равен нулю. После apply оперативного возврата на legacy
Showroom нет: ошибка устраняется новым application/IaC PR и повторным apply.
Standalone DB не сливается со старой RP SQLite; обе базы, state и backups
сохраняются как отдельные контуры данных.

## Сознательно не входит

- общий Python package или runtime router;
- submodule, subtree sync bot или shared mutable SQLite;
- миграция истории прохождений и identity;
- изменение доменных training contracts Awareness;
- новый provider, telemetry stack или dependency;
- удаление legacy таблиц.
