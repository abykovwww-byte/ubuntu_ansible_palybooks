# Plan 029: завершение Decision 043 и clean RP cutover

**Дата:** 2026-09-02

**Статус:** принят к исполнению. Шаг 0 этого плана является только
документацией и evidence; production configuration, images, SQLite и
`RP_REBUILD_ENABLED` им не меняются.

## Цель и граница authority

Довести принятый [Decision 043](../decisions/043-rp-stack-rebuild.md) до одного
чистого RP-движка, одной новой SQLite и доказуемой human-приёмки, затем удалить
исполняемый legacy RP source. Этот документ фиксирует порядок оставшейся работы,
но не создаёт второй архитектурный authority: семантика World / Scenario /
Party принадлежит Decision 043, typed Lore и явная correction —
[Decision 042](../decisions/042-rp-explicit-gm-and-typed-lore-drafts.md), а
граница Training/Showroom —
[Plan 018](018-awareness-showroom-project-split.md).

Шаги являются воротами. Source change, commit, push, merge, Ansible apply,
activation и live verification всегда сообщаются как разные состояния. Green CI
не означает apply, healthy container не означает игровую приёмку, а успешный
provider HTTP response не означает принятую сцену.

## Baseline шага 0

| Контур | Зафиксированное состояние | Как проверяется |
| --- | --- | --- |
| GitHub source | `origin/main @ 66f0808ccfd13b9f5868436a8656001e8f3076f0` | `git fetch origin --prune`, затем `git rev-parse origin/main` |
| Последний applied IaC | `2ad61019fcad7693ce620d1f158bcb3353b6eb1b` | успешный `ansible-local-apply.service` journal плюс parity Git/host/container файлов; merge SHA отдельно не считается apply proof |
| Production Gateway | image `sha256:9321777d9db87da6ac5b2b23c4c085a5d28a51199a90b2ec16d922b4b85295c4`, healthy, restart count `0` | read-only Docker inspect |
| Activation | `RP_REBUILD_ENABLED=false` | effective container environment; clean RP production выключен |
| Clean DB | `/srv/app-data/rp-stack/rp_engine.db` отсутствует | read-only host probe; новая production SQLite ещё не создавалась |
| Standalone Showroom | public repository pin `3804d483452e6082eb2079790cf10d3dcc02107f` | IaC pin; C1/O2 уже переключили `:8011`, но полная training acceptance остаётся внешним gate Plan 018 |
| Clean focused tests | `92 passed`; около `9.70s` pytest / `15.48s` wall на локальном baseline | повторный focused run на том же source/dependency boundary |
| Clean implementation size | 8 clean runtime-файлов, `4 542 / 5 000 LOC` | allowlisted physical LOC, без tests/generated/vendor |
| Консервативный verification budget | `28 273 / 5 000 LOC`, debt `23 273` | измерение всего verification-кандидата после удаления legacy, а не только `app/rp/**` |
| Full Gateway suite | applied image `66.29s`; GitHub PR 126 `95.02s` | оба значения выше gate `≤60s`; переносить время между средами нельзя |
| OpenRouter availability | публичный catalog доступен; реальный Narrator probe получил `402` | каталог и paid execution — разные ворота; нужен доступный баланс владельца |

Актуальные публичные цены, поддержка structured output/reasoning и
provider-endpoints вынесены в
[evidence 043](../decisions/evidence/043-model-pricing-2026-09-02.md). Это
временной снимок, а не runtime route proof.

## Приоритеты

1. **P0 — provider gate.** Сначала восстановить возможность реального paid
   canary и доказать exact route без NVIDIA/fallback.
2. **P0 — закрыть контракт §4–§5.** Реализовать `PlayerCorrection`, typed player
   Lore, Scenario Lore и `authoring_kind` до человеческой приёмки.
3. **P1 — budgets.** Удалить legacy из verification candidate и уложить
   фактический allowlist в `≤5 000 LOC`, full suite — в `≤60s` в каждой
   заявленной среде.
4. **P1 — human acceptance.** Blind A/B, первые 20 ходов и длинная Party должны
   предъявить качество и причинные цепочки, а не только работу очередей.
5. **P2 — cutover.** Только после всех ворот собрать/apply candidate, включить
   clean RP, провести live proof и удалить исполняемый legacy source.
6. **После cutover — прополка.** Убирать только доказанно мёртвые остатки; данные
   и forensic history не уничтожать.

## Двенадцать обязательных принципов

1. **Один новый движок и одна clean SQLite.** Нет contract revisions,
   compatibility adapters, миграции или автоматической перепривязки старых
   партий.
2. **World / Scenario / Party имеют разных владельцев.** World содержит канон,
   Scenario — конкретный старт и роль игрока, Party — неизменяемые snapshots,
   RAW, derived state и решения.
3. **Snapshots неизменяемы.** Party хранит source IDs, versions и hashes;
   изменение source после создания не меняет существующую Party.
4. **Три модельные роли.** Narrator пишет только новую RAW-сцену;
   Administrator создаёт proposals; atomic service role создаёт Relationship,
   Lore и Memory candidates. Correction и Lore используют ту же atomic service
   role, но отдельные typed operations и обработчики.
5. **Runner сохраняет четыре инварианта.** Atomic status-predicate claim;
   attempts растёт только после фактического отказа; startup/shutdown/cancel/
   await принадлежат runner; роли и handlers не смешиваются.
6. **Значение определяет модель.** Regex/substrings не являются truth predicate
   для смысла, alias, участника, направления или evidence. Код проверяет schema,
   IDs, routing, ownership, persistence и projection.
7. **Не повторять неизменённый rejected semantic output.** Новый retry допустим
   только с исправляющим context/prompt/schema/model input.
8. **Narrator fail-closed.** Один call, без fallback, repair и template;
   отклонённый ответ ничего не коммитит. Пользовательский текст сохраняется для
   явного same-key retry после runtime failure.
9. **NVIDIA недостижим.** Не catalog choice, fallback, retry target и не
   наследование из `LLM_PROVIDER`. Historical rows/logs остаются читаемыми;
   retired active bindings fail-closed до provider call.
10. **Замена заканчивается удалением source и проверкой.** Legacy mutable data,
    SQLite, state и backups сохраняются; functional isolation не выдаётся за
    destructive cleanup.
11. **Документация следует внешнему контракту.** Wiki/skills меняются при
    изменении пользовательского, авторского или операционного интерфейса, а не
    при внутренней перестановке.
12. **Состояния поставки не смешиваются.** План, source, merge, apply,
    activation и observed live result фиксируются независимо.

## Порядок исполнения

### Шаг 0 — зафиксировать план и evidence

Один docs-only PR:

- сохраняет этот plan;
- дополняет Decision 043 пятью незакрытыми clean gaps;
- сохраняет датированный OpenRouter pricing/endpoint snapshot;
- исправляет порядок acceptance harness для `run28`/`run10`.

Этот PR не меняет inventory, runtime configuration, Wiki, skill, image или
сервер. После merge Ansible не запускается.

### Шаг 1 — закрыть provider gate

Внешнее условие: владелец пополняет OpenRouter balance. После этого отдельный
provider-canary должен для каждого кандидата сохранить:

- exact model ID и фактический returned model;
- outbound `provider.order` и `allow_fallbacks:false`;
- отсутствие NVIDIA в catalog endpoints и фактическом route;
- strict structured output, `reasoning_tokens`, latency, usage/cache metrics;
- четыре исхода typed Lore Decision 042, включая `no_candidate`;
- цену по фактическим usage, а не только по справочнику.

Запрещены `openrouter/auto`, `openrouter/free`, `*-latest`, `:free`, `nvidia/*`,
скрытый fallback и унаследованный retry target. Текущего
`provider.ignore=["nvidia"]` недостаточно: exact allowlist должен быть частью
исполняемого payload и trace.

Начальный short-list:

- atomic service role: `deepseek/deepseek-v4-flash-0731`, с
  `openai/gpt-oss-120b` как сравнительным кандидатом;
- Administrator: `deepseek/deepseek-v4-pro`; дорогая эскалация
  `anthropic/claude-sonnet-4.6` только при низкой доле accepted proposals;
- Narrator: `openai/gpt-5.6-luna-pro` остаётся неизменным до исходного blind
  A/B.

Stop condition: нет баланса, exact route/NVIDIA proof или кандидат не проходит
strict schema — следующие функциональные/acceptance шаги не маскируют этот
дефект другой моделью.

**Результат 2026-09-02:** balance gate открыт, а bounded canary сохранён в
[043-provider-canary-2026-09-02.md](../decisions/evidence/043-provider-canary-2026-09-02.md).
Оба DeepSeek Flash и Qwen отклонены для atomic роли по semantic/strict gates;
`gpt-oss-120b` не допускает требуемый reasoning-off. Четыре typed Lore исхода
4/4 прошла existing local `gemma-4-26b-a4b-it-rp-q4`, поэтому функциональный
срез использует её без cloud fallback. Administrator V4 Pro прошёл
`no_proposal`/`suggest` на exact `alibaba/fp8` только с фактическим budget 2 048,
но остаётся сравнительным кандидатом до human acceptance. Narrator Luna не
менялся и остаётся blind A/B anchor. Это закрывает pre-flight для начала шага 2,
но не является API/runner/storage/UI или activation proof.

### Шаг 2 — заморозить внешний контракт и file map

Перед кодом составить consumer map `endpoint → handler → storage → prompt/UI →
tests/docs` и пометить каждый элемент `retain`, `replace` или `delete`. Минимум:

| Поверхность | Целевой владелец |
| --- | --- |
| World manifest, characters, world Lore | World source → immutable World snapshot |
| Scenario preset/free seed, player role, Scenario Lore | Scenario source/input → immutable Scenario snapshot |
| RAW, turns, revisions, derived Lore/Relationships/Memory | Party clean SQLite |
| Narrator profile/provider/base URL/model/settings/BYOK | immutable Party binding |
| Role enablement/status/error/kill switch | clean role supervisor/API/Light GUI |
| Correction/Lore drafts and decisions | owner-scoped typed Party operations |

Compatibility endpoints не добавлять. Существующие публичные Party paths и
схемы сохраняются, кроме явно принятого расширения ниже.

### Шаг 3 — один disabled функциональный PR §4–§5

PR строится за `RP_REBUILD_ENABLED=false`, без production activation. Он закрывает
пять gaps как один связный внешний contract.

#### 3.1 Единая роль игрока

`ScenarioSnapshot.player_role` — единственный источник роли игрока для preset и
free Scenario. V2 `gm-system.md`/`authors-note.md` перестают ссылаться на
удаляемый `PlayerCharacter`; отдельная runtime-сущность или compatibility
projection не создаётся.

#### 3.2 Scenario Lore

`local_overrides` становится закрытой typed формой с `lore_cards`; неизвестные
ключи fail-closed. Materialization переносит карточки в неизменяемый Scenario
snapshot. Clean Lore API и Narrator prompt различают ровно три origin:
`world`, `scenario`, `runtime`.

#### 3.3 Typed player Lore

Сохраняются публичные paths Decision 042:

- `POST /api/parties/{party_id}/lore-cards/draft`;
- `POST /api/parties/{party_id}/lore-cards`;
- существующий `GET /api/parties/{party_id}/lore-cards`.

Draft запускается только по явному действию владельца Party, видит ровно один
полный committed turn и использует существующую async service/runner. Вид
операции известен до model call. Ответ — flat typed draft или `no_candidate`;
create требует отдельного подтверждения. Audit сохраняет `authoring_kind`.

#### 3.4 `PlayerCorrection`

Целевой clean API:

- `POST /api/parties/{party_id}/player-corrections/draft`;
- `GET /api/parties/{party_id}/player-corrections`;
- `POST /api/parties/{party_id}/player-corrections/{proposal_id}/decision`.

Только владелец инициирует draft/decision. Draft строится atomic service role до
мутации, ранжирует только переданный полный catalog и получает RAW hints в
широкой плюс точной границе. Результат — flat draft или `no_target`.

Narrator RAW и стабильная Party memory неизменяемы. До `accept` gameplay state
не меняется. Proposal несёт owner/version/hash/idempotency; stale decision даёт
`409`. Принятие создаёт monotonic correction revision/overlay, который входит
ровно в следующий prompt; reject не меняет prompt/state. Повторное решение и
exact duplicate идемпотентны.

#### 3.5 Storage и jobs

Создаётся только fresh clean schema; migration старых Party не пишется.
Candidate DB архивируются как evidence, но не становятся production state.
Добавляются лишь необходимые immutable records и минимальные новые operation
types в существующую role-job/service модель. Отдельная queue/service или общий
workflow framework не создаются. Administrator остаётся отдельной ролью.

#### 3.6 Документация и skill

Wiki обновляется только потому, что появляются внешние API/authoring semantics.
`rp-world-pack-builder` меняется только если меняется доступный автору WorldPack
contract; после изменения canonical skill source выполняются Apply/Check sync в
отдельно зафиксированном состоянии.

Focused tests обязаны предъявить happy path и fail-closed boundary каждого из
пяти gaps, owner/version/idempotency, restart/recovery, no mutation before
accept и prompt projection after accept. Full suite остаётся отдельным gate.

**Результат source-кандидата 2026-09-02:** пять gaps закрыты в одном disabled
срезе без новой queue/service/dependency. Focused World/Scenario, engine,
mechanics, provider и integration набор дал `63 passed`; полный локальный
repository CI — `674 passed`, включая все Gateway и Light GUI проверки.
Канонический `rp-world-pack-builder` синхронизирован `Apply`, затем `Check`
подтвердил отсутствие drift. Консервативный промежуточный budget равен
`28 962 / 5 000 LOC`, debt `23 962`; mixed local Gateway suite занял `198.5s`.
Это функциональное доказательство шага 3, но не cutover budget/time gate:
удаление legacy и RP-only замер принадлежат шагу 4. Production apply, включение
`RP_REBUILD_ENABLED` и live-приёмка не выполнялись.

### Шаг 4 — clean-only removal и бюджеты

После зелёного шага 3 удалить из исполняемого candidate:

- contract revisions и compatibility branches;
- Adjudicator, scene-state/D20/check/Intent/RuleEngine;
- старые validator/repair/fallback/automatic-GM paths;
- legacy memory/supervisor/worlds/migrations, их mocks и evaluator;
- ordinary legacy RP handlers, когда consumer map доказывает clean replacement.

Сохранить:

- shared auth/security, model catalog, provider/BYOK foundations;
- принятые Party HTTP/Light GUI contracts;
- immutable World/Scenario/Party snapshots и hashes;
- Narrator, atomic service role, Administrator и их runner invariants;
- legacy SQLite/state/backups как неисполняемые data/forensic artifacts.

Затем удалить `RP_REBUILD_ENABLED` и обе ветки выбора: candidate содержит один
движок и один World `day-watch-moscow-v2`. Не использовать xdist, новые
зависимости или новый test service ради time gate.

Измерить и опубликовать:

- точный production allowlist `≤5 000` физических LOC;
- полный Gateway suite `≤60s` отдельно локально и на GitHub runner;
- все исключения из LOC с обоснованием;
- глобальный CI отдельно от Gateway time gate.

Stop condition: debt не закрыт или suite выше бюджета — candidate не идёт в
human acceptance/cutover. Разрешены удаление мёртвых тестов вместе с удалённой
поверхностью и сокращение дублирования; запрещено ослаблять assertions или
подменять full suite focused набором.

**Результат source-кандидата 2026-09-02.** PR #131 на head
`0bc9177d8e79c7b188d4ce56c5bc09d37734153c` содержит один clean-only движок и
один World `day-watch-moscow-v2`. Точный allowlist равен
`4 966 / 5 000 LOC`, debt `0`; full suite дал `97 passed in 6.40s`
локально и `97 passed in 5.07s` на GitHub runner. Browser,
repository, Gateway и Ansible checks green. Методика, исключения и
runner invariants зафиксированы в
[evidence](../decisions/evidence/043-clean-only-budget-2026-09-02.md). На момент
этой записи merge, apply, activation и live-проверка ещё не выполнялись.

### Шаг 5 — собрать механический candidate

Собрать exact candidate image из merged source, без bind mount `/app`. На
изолированных data/state, loopback ports и новой SQLite доказать:

- preset и free Scenario;
- opening, normal turn, provider failure и явный same-key retry;
- concurrent exact duplicate → один provider call и один commit;
- три role loops, их status/error/kill switch, restart/shutdown recovery;
- Administrator accept/reject;
- typed Lore и PlayerCorrection до и после решения;
- три Lore origin в API и prompt;
- integrity/FK, image/source hashes и отсутствие production mutation.

Это mechanical/artifact proof, ещё не human quality proof.

### Шаг 6 — human acceptance в исправленном порядке

#### 6.1 Blind A/B

Сравнить legacy anchor и candidate на одной Narrator model
`openai/gpt-5.6-luna-pro`. Кроме заранее известного различия `session_id`, все
доступные sampling/settings anchors восстанавливаются из trace по
`narrator_trace_id`; недоказанные значения не угадываются. Смена Narrator model
до завершения A/B запрещена.

#### 6.2 Первые 20 ходов и контрастный старт

Владелец вручную проходит первые 20 ходов основной Party и короткий start с
другим preset/free Scenario. Проверяются роль игрока, канон, agency, отсутствие
мета-инструкций, prose quality, ошибочные Relationships/Lore и usability ручных
correction/Lore decisions.

#### 6.3 Длинная Party и причинные цепочки

Следующая длинная Party должна достигнуть 65+ committed ходов. Probes нельзя
запускать после финальной сцены. Для Relationship, runtime Lore,
`PlayerCorrection`, player Lore и Administrator порядок один:

1. создать/получить candidate в ещё продолжающейся Party;
2. дождаться deterministic apply или выполнить явное accept/reject;
3. провести следующий Narrator turn и доказать вход revision/card/cause в prompt;
4. провести отдельный последующий turn и доказать видимое непротиворечивое
   следствие в сцене.

`run28` остаётся mechanical evidence, но его false `kept_agreement`,
out-of-span/duplicate Lore и шаблонная сцена v62 не проходят semantic gate.
`run10` доказывает исправленные atomic outputs, но не последующую сцену, потому
что probes были post-final. Нельзя склеивать эти два evidence в ложную полную
цепочку.

Administrator cadence наблюдается на первой human Party и не настраивается
заранее без данных. Для каждого model output сохраняются input anchors, exact
route, raw/parsed result, decision и последующее влияние.

### Шаг 7 — production cutover

Только после шагов 1–6:

1. создать backup и test restore нужных data/state;
2. зафиксировать pre-cutover logical snapshot;
3. merge exact activation source и дождаться успешного Ansible local pull;
4. проверить applied SHA методом journal + Git/host/container parity;
5. запустить один clean Gateway/image/SQLite без legacy branches;
6. проверить `:8010`, API, browser console, реальную Party и provider traces;
7. сверить post-cutover logical snapshot и отсутствие destructive изменений
   legacy data;
8. зафиксировать rollback boundary, не называя preserved data исполняемым
   standby.

Plan 018 C1/O2 уже передал Showroom на `:8011`; этот план не возвращает training
code в RP и не переигрывает завершённый source cutover. Незакрытая полная
training acceptance остаётся внешним gate и сообщается отдельно.

### Шаг 8 — evidence, Wiki и прополка

После observed live proof:

- обновить Decision 043 итоговыми source/merge/apply/activation/runtime facts;
- обновить Wiki и Mermaid только по фактической финальной архитектуре;
- синхронизировать изменённые canonical skills и проверить drift;
- удалить доказанно неиспользуемые source declarations/tests/docs;
- сохранить legacy SQLite, state, logs и backups согласно retention policy;
- перечислить сознательно оставленные долги с владельцем и gate.

Прополка — отдельный bounded PR. Нельзя удалять уникальные, активные,
referenced, dirty или uncommitted artifacts по имени либо возрасту.

## Предполагаемая раскладка функционального среза

Это consumer map для шага 3, а не обязательство создать каждый файл. Новая
абстракция для единственного use case запрещена.

| Задача | Предпочтительная существующая поверхность |
| --- | --- |
| Scenario Lore и player role materialization | `app/rp/content.py`, schemas, существующие WorldPack presets |
| Lore origins и immutable records | `app/rp/storage.py`, `app/rp/turn_engine.py` |
| Atomic Lore/Correction call | существующие service-model handlers/runner |
| Prompt projection | `app/rp/narrator.py` |
| Public API/owner/version checks | существующие clean branches в `app/main.py` |
| Light GUI actions/status | существующий Party UI без нового frontend framework |
| Regression contract | focused clean RP tests плюс existing full suite |

## Финальные критерии готовности

Decision 043 считается завершённым только если одновременно выполнено всё:

- один исполняемый RP engine, одна fresh schema и один World;
- legacy RP source отсутствует из runtime candidate, legacy data сохранены;
- exact provider routes не могут попасть на NVIDIA/fallback/free/latest;
- Narrator остаётся fail-closed и bound к immutable Party settings/BYOK;
- World/Scenario/Party snapshots и три Lore origin наблюдаемы;
- typed player Lore и `PlayerCorrection` проходят owner/version/idempotency и
  causal later-scene proof;
- Relationships, Lore и Memory не используют regex/substrings как semantic truth;
- mechanical candidate, blind A/B, 20-turn и 65+ Party gates приняты человеком;
- production allowlist `≤5 000 LOC`, Gateway full suite `≤60s` в каждой
  заявленной среде;
- source, merge, apply, activation и live proof имеют отдельное evidence;
- backup/test restore и SQLite integrity/FK подтверждены;
- Wiki/skills соответствуют фактически применённому внешнему контракту.

## Явно не делаем

- не добавляем новый сервис, универсальную queue/workflow-платформу, ORM или
  dependency;
- не мигрируем старые Party и не создаём compatibility adapter;
- не делаем auto-accept, automatic correction или скрытый replay текста;
- не используем regex/substrings для смысловой валидации;
- не меняем Narrator model до исходного A/B;
- не удаляем legacy DB/state/backups и historical provider/log rows;
- не возвращаем Awareness/Showroom в RP repository/runtime;
- не считаем catalog price, HTTP 200, green CI или healthy container
  доказательством полной игровой готовности;
- не рефакторим соседний shared code без доказанной необходимости consumer map.

## Defaults при отсутствии новых решений владельца

- все функциональные PR остаются disabled до полного cutover gate;
- explicit confirmation требуется для Lore/Correction/Administrator decisions;
- stale owner/version/hash всегда fail-closed;
- no-candidate/no-target — нормальный typed исход, не ошибка и не повод повторить
  тот же semantic output;
- первая human Party определяет Administrator cadence;
- exact model anchors берутся из датированного evidence и повторно проверяются
  перед apply;
- любое расхождение source, journal, host и container останавливает live verdict.
