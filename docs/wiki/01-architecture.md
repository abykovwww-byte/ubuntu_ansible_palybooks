# Архитектура и границы

[← Главная](README.md) · [Далее: интерфейсы →](02-interfaces.md)

## Архитектурная идея

RP Stack отделяет «текст, который придумала модель» от «того, что действительно произошло». Gateway — единственный компонент, который может связать пользователя, мир, персонажа, модель, состояние и историю в одну партию.

```text
Party = WorldPack
      + PlayerCharacter
      + ModelProfile
      + ScenarioType
      + CanonicalState
      + TurnHistory
      + TrainingRuntimeSnapshot (если WorldPack объявил training_runtime)
      + RPStoryMemory (только scenario_type=rp)
```

Браузер хранит cookie сессии и последнее выбранное представление. Он не является владельцем state, prompt history или provider key.

## Контейнеры и сети

Следующая схема описывает живой runtime до C1 apply. В source переключение уже
подготовлено, но apply и live verification ещё не выполнены.

```mermaid
flowchart TB
    subgraph Clients["Клиенты"]
        LG["Light GUI\nавторизованный UI"]
        SG["Showroom\nанонимная витрина"]
    end

    subgraph RPNet["Docker network: rp-stack"]
        G["FastAPI RP Gateway"]
    end

    subgraph LLMNet["Docker network: rp-llm, internal"]
        LL["llama.cpp Vulkan\nGemma 4 26B A4B Q4"]
    end

    LG -->|"nginx /api proxy"| G
    SG -->|"nginx /api proxy"| G
    G --> LL
    G --> Cloud["Cloud LLM providers"]
    G --> DB[("/data/rp_gateway.db")]
    G --> State["/state/parties"]
    G --> Packs["/worldpacks, read-only"]
```

Gateway не публикует host port. Снаружи доступны только nginx-контейнеры Light GUI и Showroom. Local LLM находится в отдельной internal-сети и не принимает запросы с LAN.

### Подготовленная C1-граница

Source C1 закрепляет standalone application exact commit
`67244432659f6c25a268cbf788a8fa3af0f5b52f`, целевой LAN-only адрес
`192.168.1.88:8011`, RP-only старый Gateway и RP Compose без старого Showroom.
Rollback window равен `0`. Эта схема ещё не является live: до Ansible apply
применённый shadow остаётся на loopback `:18011`, а старый Showroom — на
`:8011`.

```mermaid
flowchart LR
    U["RP player"] -->|":8010"| L["Light GUI"]
    L --> R["RP-only Gateway"]
    R --> RDB[("RP SQLite + state")]

    V["Training visitor"] -->|":8011"| S["Awareness Showroom"]
    S --> T["Training-only Gateway"]
    T --> TDB[("Awareness SQLite + state")]

    I["ubuntu_ansible_palybooks\nIaC"] --> R
    I -->|"exact public HTTPS commit"| T
```

Порт не является security boundary для cookie, поэтому новый project использует
собственные `awareness_gateway_session` и `awareness_showroom_visitor`. На
новой стороне Git-каталог создаёт только конфигурацию сценариев и covers;
run/history/auth identity начинается заново. Владелец явно снял перенос старой
истории как блокер, но старая RP SQLite остаётся нетронутой. Между Gateway нет
runtime-вызовов, общей БД или dual-write.

### Срез 6 Decision 043: clean RP за cutover-флагом

Clean RP использует тот же Gateway-процесс и HTTP-префикс, но отдельные
`RPTurnEngine`, SQLite и role handlers. World владеет общим каноном и статикой,
Scenario — ролью игрока, стартом, активными NPC, отношениями и настройками опыта;
loader материализует два immutable snapshot с SHA-256 до создания Party.
`RP_REBUILD_ENABLED` выбирает clean ordinary RP, не смешивая записи двух схем:

```mermaid
flowchart LR
    UI["Light GUI / API client"] --> API["Gateway main.py"]
    API --> Flag{"RP_REBUILD_ENABLED?"}
    Flag -->|"ordinary RP: true"| Engine["RPTurnEngine"]
    Engine --> Clean[("rp_engine.db")]
    Engine --> Narrator["exact Narrator route"]
    Engine --> Runner["atomic + Administrator runner"]

    Flag -->|"ordinary RP: false"| Adj["legacy RP Adjudicator"]
    Adj --> Legacy[("rp_gateway.db")]

    subgraph Source["World / Scenario source"]
        World["world.json"] --> Loader["production loader/schema"]
        Scenario["scenario-presets/*.json"] --> Loader
        Loader --> Snap["WorldSnapshot + ScenarioSnapshot\nотдельные SHA-256"]
    end
    Snap --> Engine
```

В clean-контур перенесён только `day-watch-moscow-v2`. Он не читает и не пишет
legacy Party/state/turn tables; обычные legacy RP endpoints при включённом флаге
возвращают `410`. После C1 RP source не содержит Training/Showroom exception:
standalone project обслуживает training-only Gateway и Showroom на целевом
`192.168.1.88:8011`. Нового RP-контейнера, порта или dual-write нет. В inventory
RP-флаг пока `false`, а C1 apply не выполнен: source интегрирован, но activation,
браузерный UX и live proof ещё не подтверждены.

## Ответственность компонентов

| Компонент | Отвечает за | Не отвечает за |
|---|---|---|
| Light GUI | RP-чат, создание партии, GM-инструменты, Prompt Inspector, admin-only Turn Trace Workbench и RP-админка | Правила, state, ключи провайдеров, долговременную память и training UI |
| Showroom | Витрина, анонимный запуск, минимальный чат, portal, training artifacts, рейтинг | Прямой доступ к party ID, внутреннюю turn trace, скрытый scoring, администрирование без Gateway role |
| Gateway | Auth, party scope, state, history, универсальные интерпретаторы правил, LLM routing, диагностическую trace read model, snapshots и события artifacts, branches, datasets | Предметную программу обучения, верстку интерфейсов и ручное хранение секретов в браузере |
| WorldPack | Неизменяемый замысел мира, seed, prompts, executable training program/assessment/fallbacks, site/workspace blueprints и interaction policy | Выбор модели, party owner, runtime state конкретного прохождения |
| Narrator LLM | Финальная сцена, диалог и разрешённые текстовые поля artifact | Истину state, HTML/CSS/JS, scoring, права, выбор режима |
| Service model | Эпизодические главы, RP-only living story memory, world-state drafts, генерация персонажей | Обычное ведение партии, изменение canonical state и использование party BYOK |
| Ansible | Доставка source/config/Compose на сервер | Игровое состояние и данные пользователей |

## Слои данных

```mermaid
flowchart LR
    WP["WorldPack source\nGit, read-only runtime"] --> Seed["state-seed.json"]
    WP --> Runtime["training program + assessment + fallbacks"]
    Runtime --> RuntimeSnap["Immutable party runtime snapshot"]
    Seed --> PS["Изолированный Party State"]
    RuntimeSnap --> PS
    PS --> SV["State versions"]
    PS --> Turns["Raw turns"]
    Turns --> Mem["Memory chapters"]
    Turns --> Story["RP-only story-memory snapshots"]
    Turns --> Journal["Legacy journal records"]
    Turns --> Dataset["Review overlay / JSONL"]
    PS --> Branch["Checkpoint branch"]
    WP --> Blueprint["Training site blueprints"]
    Blueprint --> Artifact["Party-scoped artifact snapshots"]
    Artifact --> Events["Typed interaction events"]
    Events --> PS
```

- **WorldPack** — шаблон. Он не изменяется во время партии.
- **Training runtime snapshot** — хешированный executable-контракт программы, оценки и fallback; обновление source влияет только на новые партии.
- **Party state** — текущие подтверждённые факты конкретного прохождения.
- **State versions** — история версий для rollback и audit.
- **Raw turns** — первичный журнал сообщений и фактических LLM-вызовов.
- **Memory chapters** — сжатые эпизоды для narrator prompt, но не замена raw history.
- **RP story memory** — кумулятивный реестр длинной RP-кампании; не создаётся для `training` и не является authority. Архивные агрегаты выведенного режима не исполняют новые memory jobs.
- **Legacy journal** — сохранённые записи прежних версий; текущий runtime их не генерирует.
- **Dataset labels** — отдельная кураторская разметка; она не переписывает игру.
- **Training artifact snapshot** — валидированный экземпляр шаблона с публичным текстом narrator; произвольный HTML модели не исполняется.
- **Interaction event** — типизированное действие `opened`, `submitted` или `reported`, которое Gateway привязывает к партии и учитывает детерминированно.

## Диагностический RP Turn Trace Workbench

Workbench не добавляет контейнер или второй state store. Корень трассы —
`(state_campaign_id, request_id)`, поэтому запрос остаётся видимым даже без
committed turn. Gateway объединяет существующие authoritative stores с
диагностическими событиями исполнения, точечными before/after для in-place
проекций, model attempts и пользовательскими аннотациями.

```mermaid
flowchart LR
    R["turn_requests\nrequest_id"] --> E["turn_trace_events"]
    A["Авторитетные RP stores\nturns · state · memory · relationships"] --> V["Trace read model"]
    E --> V
    M["turn_state_mutations"] --> V
    S["service_call_log"] --> V
    N["turn_phase_annotations"] --> V
    V --> L["Light GUI\nтолько admin/operator"]
    X["Showroom"] -.->|"нет доступа"| V
```

`turn_trace_events`, `turn_state_mutations`, `service_call_log` и аннотации не
читаются игровым runtime как вход. Ошибка диагностической записи не должна
изменить outcome, prompt, state patch, scoring или fallback. Workbench понимает
фактическую `rp_contract_revision`, а для legacy-партий —
`rp_contract_version`; незнакомые фазы остаются видимыми как generic nodes.
Admin-only gate не позволяет обычному владельцу партии увидеть скрытые RP
outcome, service prompts или операторские аннотации. Standalone training data и
assessment-инструкции в RP Workbench не загружаются.

## Почему каждый Gateway — authority своего режима

До LLM-вызова RP Gateway определяет владельца и партию, загружает RP state и
историю именно этой party/branch, интерпретирует реплику, выполняет RP Rule
Engine, получает outcome и собирает ограниченный narrator prompt. После вызова
он валидирует ответ, применяет patch и сохраняет turn, request status и audit.
Training snapshot, scoring и artifact/workspace contracts этот процесс не
загружает.

Standalone Training Gateway отдельно загружает immutable training-runtime
snapshot и неиспользованные типизированные события artifacts/workspace,
интерпретирует WorldPack assessment, получает deterministic outcome/state patch
и собирает sanitized `ACTIVE_TRAINING_TURN_CONTRACT`. Он делает один основной
narrator-вызов; один bounded provider repair допустим только для soft validation
failure. Ошибка provider или hard violation переводит ход в authored fallback
текущего WorldPack. Поэтому модель может заново сформулировать сцену, но не
заменить событие или score другим.

## Независимые training capabilities

> Статус: runtime реализован в IaC; применение ревизии и live-проверка фиксируются отдельно.

Showroom-сценарий имеет две независимые training-only настройки:
интерактивные ссылки и интерактивный рабочий диск. Наличие site/workspace
контракта в WorldPack будет означать поддержку, но не автоматическое включение.
Gateway проверит выбор и сохранит обе галки в immutable snapshot каждого run.

```mermaid
flowchart LR
    WP["Training WorldPack\nsite/workspace contracts"] --> Gate["Gateway capability gate"]
    Admin["Showroom admin\nlinks + workspace"] --> Gate
    Gate --> Run["Run snapshot\n00 / 10 / 01 / 11"]
    Run -->|"links"| Site["TrainingArtifactService"]
    Run -->|"workspace"| Disk["TrainingWorkspaceService"]
    Site --> Rules["Normalized typed evidence"]
    Disk --> Rules
    Rules --> Score["WorldPack assessment\nчерез универсальный runtime"]
```

`TrainingWorkspaceService` работает как логический модуль Gateway, а не
новый синхронный контейнер. Открытие папки, файла или сайта останется
SQLite/JSON-операцией без LLM. Отдельный фоновый worker допустим только для
предварительной проверки и конвертации загружаемых документов.

## Основные границы совместимости

Живой Compose до C1 apply содержит `rp-gateway`, `rp-light-gui`,
`rp-showcase-gui` и опциональный `rp-local-llm`. SillyTavern-контейнера в нём
нет.

Подготовленный zero-window C1/O2 source удаляет `rp-showcase-gui`, Awareness
WorldPacks и training runtime из RP checkout/Compose и поставляет Awareness
Showroom отдельным commit-pinned application через IaC. RP Gateway скрывает
старые training-партии и принимает только `rp`. Legacy training/Showroom строки
БД, state и backups остаются сохранёнными, но исполняемого training source в RP
контуре больше нет.

При этом сохранены:

- `/v1/chat/completions` для OpenAI-compatible интеграций;
- SillyTavern lorebook JSON внутри некоторых WorldPacks;
- legacy single-campaign endpoints `/api/state`, `/api/world/*`.

Новые функции должны использовать party-scoped `/api/parties/{party_id}/...` или showroom-scoped `/api/showroom/...`.

## Связанные решения

- [Decision 006 — party flow](../../roles/apps/files/rp-stack/docs/decisions/006-light-gui-party-flow.md)
- [Decision 010 — scenario types](../../roles/apps/files/rp-stack/docs/decisions/010-party-scenario-types.md)
- [Decision 015 — training interaction capabilities](../../roles/apps/files/rp-stack/docs/decisions/015-training-scenario-interaction-capabilities.md)
- [Decision 017 — WorldPack-owned training runtime](../../roles/apps/files/rp-stack/docs/decisions/017-worldpack-owned-training-runtime.md)
- [Decision 018 — separate training and RP gateways](../../roles/apps/files/rp-stack/docs/decisions/018-separate-training-and-rp-gateways.md)
- [Plan 018 — Awareness Showroom project split](../../roles/apps/files/rp-stack/docs/plans/018-awareness-showroom-project-split.md)
- [Decision 027 — request-centric Turn Trace Workbench](../../roles/apps/files/rp-stack/docs/decisions/027-turn-trace-workbench.md)
- [Compose](../../roles/apps/templates/rp-stack.compose.yml.j2)
