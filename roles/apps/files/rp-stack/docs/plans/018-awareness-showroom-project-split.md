# Plan 018: вынести Awareness и Showroom в training-only project

Date: 2026-08-27 · Owner decision: accepted · Runtime status: I1 shadow applied, not cut over.

## Результат

После cutover существуют два независимо поставляемых приложения:

| Приложение | Git authority | UI | Runtime mode | Данные |
|---|---|---|---|---|
| RP Stack | `abykovwww-byte/ubuntu_ansible_palybooks` + встроенный RP source | Light GUI `:8010` | только `rp` | `/srv/app-data/rp-stack` |
| Awareness Showroom | `abykovwww-byte/tavern-awareness-showroom` | Showroom `:8011` | только `training` | `/srv/app-data/awareness-showroom` |

`ubuntu_ansible_palybooks` остаётся authority для server topology и хранит
commit pin отдельного public application repository. Код нового приложения живёт
только в его repository. Ansible не копирует его source из старого дерева.

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
- исторические Showroom/training строки старой SQLite как read-only legacy data
  на rollback window.

После подтверждённого cutover из активного RP source удаляются Showroom UI,
Awareness WorldPacks и training-only runtime/API/tests. Таблицы не удаляются и
история не переписывается.

## Git migration

1. Через GitHub API зафиксировать exact source `main` SHA.
2. Из независимого clone выполнить history-preserving split:

   ```text
   git subtree split --prefix=roles/apps/files/rp-stack <SOURCE_BASE_SHA>
   ```

3. Создать `abykovwww-byte/tavern-awareness-showroom`, опубликовать
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

Создать отдельный project из subtree history. Добавить `AGENTS.md`, README,
provenance, standalone Compose/env example, CI и migration documentation.
Baseline должен собираться до удаления code.

### N2 — training-only enforcement

Включить process/resource mode guards, отдельные paths/cookies/network и
training-only Showroom controls. Запустить focused Gateway/JS tests и full CI.

### N3 — RP prune

Удалить RP-only UI, WorldPacks, services, endpoints and tests. Сначала доказать
отсутствие training consumer для каждого кандидата. Не переименовывать
сохраняемые классы и каталоги только ради косметики.

### I1 — shadow deploy

IaC клонирует новый public repository на exact commit в
`/srv/apps/awareness-showroom`, persistent data — в
`/srv/app-data/awareness-showroom`, backup — в
`/srv/backups/awareness-showroom`. Showroom временно публикуется на `18011`,
старый `8011` продолжает обслуживать пользователей.

I1 применён: отдельные containers, loopback `:18011`, SQLite/state/covers
и backup paths подняты. Пустая витрина доказала топологию,
но не training acceptance.

### I2 — source-owned scenario catalog

Текущие published training configs и обложки публикуются отдельным PR
нового application repository. IaC после его merge пинит exact commit и
включает catalog path. До apply и живого прохождения это только
source/delivery contract.

### O1 — freeze and drain

На старом Showroom запретить создание новых сценариев/runs, дождаться
завершения нужных активных runs и read-only сверить legacy published
config с Git-каталогом. Любой delta идёт обычным application PR; runtime
admin mutation не становится authority.

### C1 — cutover

Переключить `8011` на новый project, сделать старый Gateway RP-only, убрать
старый Showroom из активного Compose. `8010` остаётся RP Light GUI. Никакого
dual-write или proxy между Gateway.

### O2 — cleanup

После rollback window удалить из активного RP source training/Showroom code и
data mounts. Legacy DB/backup сохраняются согласно существующей retention
policy; destructive SQL migration не выполняется.

## Acceptance

До cutover обязательны:

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
- source merge, Ansible apply и live verification сообщаются как отдельные
  стадии.

Rollback до окончания окна: вернуть IaC pin/topology на предыдущий RP Stack
revision и снова опубликовать старый Showroom на `8011`. Новую БД не сливать со
старой; после решения о rollback она остаётся отдельным forensic snapshot.

## Сознательно не входит

- общий Python package или runtime router;
- submodule, subtree sync bot или shared mutable SQLite;
- миграция истории прохождений и identity;
- изменение доменных training contracts Awareness;
- новый provider, telemetry stack или dependency;
- удаление legacy таблиц.
