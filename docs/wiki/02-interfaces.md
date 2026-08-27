# Интерфейсы

[← Архитектура](01-architecture.md) · [Главная](README.md) · [Далее: жизненный цикл хода →](03-turn-lifecycle.md)

## RP contract revision

`PartySummary` совместимо хранит целое поле `rp_contract_revision` (`0..11`).
Gateway source поддерживает revision `11`, а activation inventory настроен на
observed `11` вместе с первым revision-11 WorldPack. Для declarations
`0..10` обычная новая RP-партия получает `min(WorldPack declared, observed)`.
Declared revision `11` требует observed `>=11` и иначе fail-closed, без
downgrade до `10`. Запрос создания
manual branch или autotest может явно передать candidate-ревизию в диапазоне
`0..11`; она хранится только у ветки и не меняет source party. Существующие поля и
endpoint сохраняются. Пустое поле в админской форме сохраняет revision исходной
партии. Existing party не повышается автоматически при изменении observed.

## Light GUI

Light GUI — основной интерфейс владельца партии. Это статические HTML/CSS/JavaScript, которые nginx отдаёт на `:8010` и проксирует `/api` в Gateway.

Основные возможности:

- вход через Gateway-сессию;
- список и создание партий;
- явный выбор WorldPack, персонажа, `scenario_type`, provider и narrator model;
- party-scoped параметры narrator во вкладке «Управление» для поддерживаемых OpenRouter Luna, Luna Pro и DeepSeek V4 Flash;
- создание private generated WorldPack из короткого ручного prompt или загруженного `.md`: браузер читает Markdown как UTF-8-текст, показывает имя/размер/фрагмент и передаёт содержимое Gateway без HTML-rendering;
- основной чат с восстановлением pending-запроса после refresh;
- открытие валидированных training-сайтов и отправка накопленных событий перед следующим сообщением;
- сценарно-зависимое редактирование персонажей вручную и генерация служебной моделью: в `rp` обычная форма показывает только имя, статус, локацию и текущую цель, не изменяя скрытые расширенные поля; в `training` сохраняется полный редактор;
- выбор party-scoped BYOK-ключа;
- GM preview/apply/discard, state и rollback; отдельной RP-формы проверки нет;
- Prompt Inspector, реальный размер последнего prompt, память и lore cards; для `rp` панель памяти отдельно показывает living story snapshot, его revision и покрытие, revisions 2..8 готовят legacy correction со следующим ходом, а rev9 открывает отдельный GM draft/confirm; для rev8+ под narrator response показываются Lore Cards, реально попавшие в его prompt, и явная кнопка draft из завершённого хода; для `training` этого UI нет;
- request-centric Turn Trace Workbench с main/background lanes, narrator/service attempts, state mutation diff и аннотациями;
- checkpoints, branches и история LLM-autotest;
- 👍/👎 для полной пары «реплика игрока → ответ модели»;
- admin-раздел: пользователи, глобальная служебная модель, видимость миров, Showroom, автотесты и dataset review.

Light GUI не вызывает provider API напрямую. Даже если ключ введён пользователем, он сохраняется Gateway для конкретной партии и не возвращается в браузер целиком.

### Authored presets и opening seeds в revision 11

Для revision-11 WorldPack в режиме `rp` `GET /api/worldpacks` возвращает отдельные top-level
списки `presets[]` (`id`, `title`) и `openings[]` (`id`, `title`,
`player_role`) вместе с `presets_default` и `openings_default`. Light GUI
показывает два selector до создания партии и отправляет только выбранные IDs:

```text
POST /api/player-characters/draft   {worldpack_id, ..., opening_id?}
POST /api/player-characters         {..., opening_id?}
POST /api/parties                   {..., preset_id?, opening_id?}
```

Omitted ID означает объявленный default, а не первый элемент массива.
Неизвестное или похожее на путь значение отклоняется; браузер не передаёт пути
WorldPack. `PlayerCharacterSummary` возвращает resolved `opening_id`.
`PartySummary` может вернуть выбранные `preset_id`, `opening_id` и audit-only
SHA-256, но не полные materialized prompt texts или state seed.

Для паков revisions `0..10` новые поля отсутствуют из ответа и форма сохраняет
прежний UX. Activation merge `80ab6d3` применён 27 августа 2026 года:
авторизованный Light GUI показал все три preset и четыре opening варианта,
передал выбранные `strategic` и `inquisition-observer`, а созданная ordinary
party сохранила оба ID и revision `11`. Это уровень `подключено`; отдельного
зарегистрированного causal probe и endurance-доказательства для revision 11 нет.

### Lore Cards в revision 8

History API возвращает для хода `metadata.prompt_assembly.lore_card_ids` и
читаемые `activated_lore_cards`. UI строит chips строго в порядке ID из metadata,
поэтому не показывает карточку, которую Gateway отбросил из финального prompt.

Кнопка «Сделать Lore Card из хода» вызывает party-scoped
`POST /api/parties/{party_id}/lore-cards/draft` с ID сохранённого complete turn.
Gateway возвращает только draft и заполняет им существующую видимую форму.
Пока игрок не проверил текст и не нажал «Подтвердить Lore Card», create endpoint
не вызывается и карточки в party storage нет. Свободный текст чата не запускает
draft classifier. Provider key и request остаются на стороне Gateway.

### Обращение к мастеру в revision 9

Composer rev9 RP party показывает отдельную кнопку «Мастеру». Она отправляет
`channel=gm` и обходит автоматический classifier. Обычная отправка использует
`channel=auto`: если local `gm_intent` не уверен или недоступен, API возвращает
`status=route_required`, UI показывает «Мастеру / В сцену», а turn/state не
меняются.

GM route возвращает `status=gm_draft` и strict `gm_patch_draft` с exact target,
`before` и `after`. UI показывает diff; confirm/reject отправляется отдельно в
`POST /api/parties/{party_id}/gm-corrections/decide`. Reject ничего не пишет.
Confirm создаёт одну внеигровую запись в истории: она отображается центральной
заметкой мастера, не получает 👍/👎, Lore Card draft или фиктивный narrator
response. Сцена и игровое время остаются прежними.

Кнопки «Исправить / Отозвать» в панели story memory для rev9 сразу открывают тот
же GM draft с exact target slot. Revisions 2..8 сохраняют прежний pending payload
со следующим игровым ходом. Rev9 legacy `story_memory_corrections[]` отклоняет,
чтобы один и тот же correction не мог пройти двумя разными путями.

### Параметры наратора

В «Управлении» параметры сохраняются вместе с выбранной моделью одной кнопкой и
действуют со следующего narrator-ответа. Значение «Авто» означает, что поле не
передаётся и модель использует свой default. Для Luna и Luna Pro доступны глубина
рассуждений и бюджет ответа; для DeepSeek V4 Flash дополнительно доступны
temperature и Top P. Короткое описание рядом с каждым полем объясняет влияние на
скорость, стоимость, вариативность и риск оборванного ответа. Неподдерживаемая
модель не показывает фиктивные disabled-настройки.

Настройки не меняют service model, memory jobs, relationship extraction,
auto-player или правила WorldPack. Gateway повторно проверяет диапазоны и
возможности выбранной модели; браузер не является authority. Смена модели через
старый payload без `narrator_settings` сохраняет параметры при повторном выборе
того же profile и сбрасывает их при переходе на другой profile.

Обычный `POST /api/parties/{party_id}/messages` сохраняет прежний контракт и
дополнительно принимает необязательный `story_memory_corrections[]` только для RP
revisions 2..8: `{field, fact_id, action: retract|replace, replacement_text?}`.
Gateway отклоняет неизвестную или уже неактивную цель до provider/state/turn;
authority и provenance не являются полями публичного payload.

При создании мира ручное поле ограничено 6000 символами. Для `.md` действует отдельный предел: 1 МиБ на клиенте и 200 000 символов в Gateway. Такой файл становится стабильным world system prompt, поэтому для очень крупного мира пользователь должен выбрать narrator model с достаточным context window.

### Read-only context и prompt diagnostics для branch

Candidate revision-7 follow-up добавляет один необязательный query-параметр к
существующим диагностическим endpoint:

```text
GET  /api/parties/{party_id}/context?branch_id={branch_id}
POST /api/parties/{party_id}/prompt/preview?branch_id={branch_id}
     {"content":"...", "source":"current|last"}
```

`branch_id` выбирает existing isolated branch только внутри той же party и owner
scope. Gateway сам разрешает её state store, source-party runtime settings и
persisted branch revision; raw `state_campaign_id` клиент не передаёт. При указанном
параметре ответ также содержит `branch_id`. Без параметра прежние source-party
path, preview body и response shape сохраняются без изменений. Неизвестная,
чужая или принадлежащая другой партии ветка возвращает `404`.

Оба endpoint остаются read-only: они не вызывают provider, не создают turn,
snapshot или branch и не меняют source/branch state. Wiring и excluded-turn
hardening merged в PR59/PR61, applied и live-проверены: excluded latest turn
`party_ad201794ce31` вернул один и тот же content-free `prompt_assembly` из turn
metadata, gateway trace, Prompt Inspector `source=last` и recorded context.
Registry-row Decision 030 имеет уровень `подключено`. Отдельный UI-control этим
контрактом не вводится.

### Turn Trace Workbench

Workbench доступен в Light GUI только пользователю с существующей ролью Gateway
`admin` (оператору). Обычный владелец партии не получает trace API. Ветка
выбирается явным `branch_id`; Gateway сам разрешает его в изолированный
`state_campaign_id`.

Замороженный API:

```text
GET  /api/turn-traces/parties
GET  /api/turn-traces/parties/{party_id}/branches
GET  /api/parties/{party_id}/turn-traces?branch_id&limit&before
GET  /api/parties/{party_id}/turn-traces/{request_id}?branch_id
POST /api/parties/{party_id}/turn-traces/{request_id}/annotations?branch_id
     {"annotation_id":"...","phase_key":"...","body":"..."}
```

Первые два endpoint возвращают партии и ветки для административной загрузки
страницы. Список возвращает
bounded summaries и непрозрачный cursor; тяжёлые prompt, response и
before/after загружаются detail-запросом. Экран показывает только реально
исполненные фазы, отличает main/background работу, repair/fallback и request без
committed turn. Для RP используется `rp_contract_revision`, если она есть, иначе
legacy `rp_contract_version`; неизвестная фаза отображается без клиентского enum.
В сравнении первый столбец всегда называет исполнителя и человеческое назначение
фазы, сохраняя `alignment_key` как вторичную техническую метку. Структурированные
данные раскрываются по JSON-сегментам, а сравнение показывает leaf-oriented
изменения по путям без повторного вывода неизменившейся части полного state/payload.
Единственная мутация из Workbench — идемпотентная аннотация существующей фазы;
она попадает в audit, но не меняет игру.

Обычный владелец партии и Showroom не получают страницу, ссылку или trace
endpoint: пользовательская session, visitor cookie и `run_id` не дают права на
внутренние prompt, ответы моделей, state diff или server-only training policy.

## Showroom

Showroom — отдельная витрина на `:8011` для прохождения опубликованных сценариев без регистрации.

```mermaid
sequenceDiagram
    participant Visitor as Посетитель
    participant UI as Showroom
    participant GW as Gateway
    participant Party as Внутренняя Party

    Visitor->>UI: Открывает сценарий
    UI->>GW: POST /api/showroom/scenarios/{id}/runs
    GW-->>Visitor: HttpOnly visitor cookie
    GW->>Party: Создаёт character + state + history
    GW-->>UI: run_id без party_id
    Visitor->>UI: Отправляет действие
    UI->>GW: POST /api/showroom/runs/{run_id}/messages
    GW->>Party: Обычный party turn pipeline
    Party-->>GW: Ответ и state version
    GW-->>UI: Публичное представление + artifact snapshot
    Visitor->>UI: Открывает / отправляет форму / сообщает о сайте
    UI->>GW: POST /api/showroom/runs/{run_id}/artifact-events
    GW->>Party: Сохраняет typed event без LLM и продвижения хода
    Visitor->>UI: Открывает / скачивает / сообщает о файле
    UI->>GW: POST /api/showroom/runs/{run_id}/workspace-events
    GW->>Party: Сохраняет workspace event без LLM и продвижения хода
```

`ShowroomScenario` и `WorldPack` — разные сущности. Scenario добавляет публичное название, описание, режим, модель, обложку, порядок и leaderboard policy к ссылке на WorldPack. Несколько сценариев могут использовать один мир.

Посетитель получает случайную HttpOnly-cookie. Gateway связывает её с `ShowroomRun`, а run — с внутренней Party. Публичному клиенту не выдаются raw party ID, скрытый score, rubric или answer key.

Для training-миров Showroom умеет:

- показывать immutable snapshot корпоративного портала до пяти персонажей;
- материализовать динамическую должность из описания сотрудника;
- отображать структурированные письма и чаты безопасными text nodes;
- открывать валидированные учебные сайты общим с Light GUI DOM-renderer;
- собирать opt-in leaderboard по numeric state path или числу ходов;
- хранить обратную связь 👍/👎 с проверкой владельца visitor cookie.

Обычные ссылки и вложения в structured content остаются неинтерактивными. Исключение — ссылка на валидированный training artifact: в письме и в блоке `СООБЩЕНИЕ` UI превращает только точное совпадение с выданным Gateway `display_url` в кнопку открытия сайта. Gateway отдаёт только публичный snapshot, UI собирает страницу из фиксированного blueprint без `innerHTML`, внешних ресурсов и исполняемого кода. Введённые значения не отправляются; Gateway получает только факт отправки формы.

После POST хода Showroom перечитывает canonical history из Gateway с
`cache: no-store`. Браузерный или промежуточный cache не может вернуть старую
историю и убрать pending-placeholder до появления уже сохранённой реплики.

## Административный контур

Админка живёт в Light GUI и использует те же Gateway session/role проверки. Она управляет:

- пользователями и их статусами;
- сменой паролей;
- глобальной служебной моделью;
- public/private видимостью WorldPacks;
- Showroom-сценариями и обложками;
- party-scoped LLM-vs-LLM autotests;
- review статусами партий и ходов;
- JSONL-экспортом одобренных samples.

Администратор не получает raw API key через публичный API: ответ содержит метаданные и последние четыре символа.

### Галки training-сценария

> Статус: UI и Gateway API реализованы в IaC; live-статус зависит от применённой ревизии.

После выбора `Тип сценария = Training` редактор Showroom должен показывать две
независимые галки:

```text
[ ] Подключить интерактивные ссылки
[ ] Подключить интерактивный диск
```

Для `rp` они скрыты или заблокированы и всегда сохраняются как
`false`. Gateway, а не браузер, проверяет поддержку выбранным WorldPack. Один
сценарий хранит одну комбинацию; четыре копии мира и автоматические четыре
карточки не создаются. Для сравнения режимов администратор может опубликовать
несколько сценариев, ссылающихся на один WorldPack.

При старте Gateway копирует `interactive_links_enabled` и
`interactive_workspace_enabled` в run. Последующее редактирование сценария не
меняет уже начатую тренировку. Рабочая папка появляется в пользовательском UI
только при включённом snapshot-флаге; ссылки аналогично получают интерактивный
site snapshot только при включённом флаге.

## Compatibility API

Gateway сохраняет OpenAI-compatible `/v1/chat/completions` и legacy single-campaign endpoints. Они нужны для интеграций и отладки, но не должны становиться основой новых функций.

Party-scoped `POST /api/parties/{party_id}/checks` также сохранён для старых
клиентов. Для партии `rp-core.v2` он возвращает нейтральный narrative envelope,
не бросает кубик, не назначает success/failure и не создаёт запись проверки.

| Свойство | Light GUI | Showroom | `/v1/chat/completions` |
|---|---|---|---|
| Пользователь | Gateway account | Анонимная visitor cookie | Внешняя интеграция |
| Контекст | Явный `party_id` | `run_id -> party` внутри Gateway | Legacy/default campaign |
| Админ-инструменты | Да, по роли | Нет | Нет |
| Provider key | Server или party BYOK | Модель сценария | Заголовок/настройки совместимости |
| Рекомендуемый путь | Да | Да | Только compatibility |
| Внутренняя turn trace | Только admin/operator | Нет | Нет |

## Исходники

- [Light GUI](../../roles/apps/files/rp-stack/rp-light-gui)
- [Showroom](../../roles/apps/files/rp-stack/rp-showcase-gui)
- [Showroom ADR](../../roles/apps/files/rp-stack/docs/decisions/012-public-showroom-scenarios.md)
- [Training capabilities ADR](../../roles/apps/files/rp-stack/docs/decisions/015-training-scenario-interaction-capabilities.md)
- [Turn Trace Workbench ADR](../../roles/apps/files/rp-stack/docs/decisions/027-turn-trace-workbench.md)
- [Gateway endpoints](../../roles/apps/files/rp-stack/rp-gateway/app/main.py)
