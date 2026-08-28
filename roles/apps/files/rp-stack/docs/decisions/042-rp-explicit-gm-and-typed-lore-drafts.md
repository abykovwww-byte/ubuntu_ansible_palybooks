# Decision 042: safe GM routing, response detail and typed Lore drafts

**Дата:** 2026-08-27

## Status

**Decision status: Accepted.** Пользователь принял staged scope и отдельно снял
совместимость текущих партий как ограничение для typed Lore 2026-08-27.

**Граница с [Decision 043](043-rp-stack-rebuild.md).** После принятия ребилда
старый план поставок из этого документа не исполняется на `Adjudicator`: не
создаются промежуточные revision 11/12 activation и compatibility-ветки. Decision
043 наследует отсюда продуктовые контракты typed Lore, Gateway-owned exact target
и безопасный принцип «никакой мутирующий correction без явного действия игрока»;
порядок реализации и финальные ворота задаёт Decision 043. Разделы ниже про
revision 8–12 и live-партии старого движка остаются обоснованием контрактов, а не
самостоятельной очередью поставки.

Уровень готовности по Decision 022 не заявляется: mechanism code, focused tests,
deployment и live evidence для этого решения ещё не существуют. Текущий runtime
остаётся на контрактах Decisions 037 и 038. Документ описывает одну продуктовую
правку и три последовательные поставки; сам по себе он не повышает source
ceiling, observed revision или revision какого-либо WorldPack.

## Context

Ручная проверка revision-11 партии выявила не один дефект валидатора, а три
разрыва между обещанием интерфейса и фактическим контрактом Gateway.

### Сломался автоматический маршрут, а не кнопка «Мастеру»

Обычная отправка из composer идёт как `channel=auto` (`rp-light-gui/app.js:2580`).
Gateway вызывает local `gm_intent` для каждого такого сообщения. Сейчас только
`uncertain` возвращает игроку выбор; уверенный `correction` молча переходит в
`gm_patch_draft` (`rp-gateway/app/main.py:2628-2643`).

В live-проверке сообщение «ГМ мод. Давай сцену и аутпут аобольше» было отправлено
обычным способом. Вызов `528` классифицировал его как `correction`, после чего
вызов `529` начал строить patch. Явная кнопка «Мастеру» в этой цепочке не
участвовала. Ещё раньше вызов `526` вернул `uncertain` на обычную реплику: за одну
минуту игрок дважды попал в служебный маршрут.

Local Gemma выбрала RAW turn и сама написала `before`. Gateway требует дословного
присутствия `before` в выбранном ходе и правильно отклонил выдуманную цитату
(`rp-gateway/app/services/rp_gm.py:577`). Игровой turn, state version, correction
overlay и memory job созданы не были.

Exact validator сработал верно: он не дал случайно переписать канон. Ошибки выше
по цепочке:

- автоматический classifier получил право без спроса открыть мутирующий flow;
- настройка стиля ответа попала в контракт исправления факта;
- точную неизменяемую цель и story-memory field поручили угадывать модели.

Вызов `529` показал границу опасности прямо: модель подставила в `before`
собственную внеигровую реплику игрока. Кандидат RAW сейчас склеивает
`player_message` и `narrative_response` (`rp-gateway/app/services/rp_gm.py:353-357`),
поэтому намерение игрока структурно способно стать каноном с authority `user`.

### Existing target slots недостаточно точны для RAW

Decision 038 уже использует `target_slot`, а запрос уже имеет `gm_target_slot`.
Memory и absolute rules имеют стабильные slots `memory:<field>:<fact_id>` и
`rule:<id>`. RAW представлен широким `raw:<turn_id>`, после чего модель выбирает
`before`, а публичный draft получает расширенный slot с hash выбранного моделью
текста (`rp-gateway/app/services/rp_gm.py:524`).

Новый slot layer поэтому не нужен. Заменяется только RAW-часть: Gateway заранее
выделяет exact narrator claims и владеет их текстом.

Текущий candidate catalog собирает memory, RAW и rules одним списком, ранжирует их
подстрочным score и жадно укладывает до восьми кандидатов и 4 000 символов
(`rp-gateway/app/services/rp_gm.py:308-394`). После нарезки одного RAW turn на
5–15 spans этот turn сможет занять весь payload, если явно не сохранить
разнообразие каталога.

Live-вызов `525` дошёл до текущего GM output ceiling: 300 из 300 tokens и
`finish_reason=length`. Обрыв уже отлавливается
(`rp-gateway/app/services/rp_gm.py:180`), но его причина — требование к модели
дословно повторить длинную цитату — остаётся.

### Lore draft обязан что-нибудь создать и может потратить весь ответ на reasoning

Кнопка «Сделать Lore Card из хода» отправляет целую завершённую игровую единицу.
Strict schema допускает только `title/content/keywords`
(`rp-gateway/app/models/schemas.py:424`): ни authoring kind, ни законного ответа
«здесь нечего сохранять» нет. Поэтому пересказ сцены — структурно валидный и
естественный ответ модели.

В первом наблюдении confirm gate сработал: проверенный scene recap не был
сохранён. Но 2026-08-27T12:14Z дефект прошёл всю цепочку на живой revision-11
партии `party_cac70558b50a`. Draft `566` вернул валидный strict JSON
(`finish_reason=stop`); игрок подтвердил его; появилась карточка `lore_cards.id=286`
на 642 символа с двенадцатью keywords; `turns.metadata_json.lore_card_ids`
содержит `286` в двенадцати подряд ходах 1867–1878, и партия продолжается. Пять
из них (1870–1874) карточка держалась в prompt без единого упоминания игроком:
её поддерживал только текст нарратора в окне `current+3`. Это единственная в базе
player-created карточка, дошедшая до narrator prompt, и она является пересказом
сцены. Метаданные, SHA-256 и потриггерная атрибуция — в
[манифесте доказательств](evidence/042-live-evidence-manifest.md).

Значит проблема не в том, что подтверждение слабое, а в том, что draft UX
подталкивает игрока дублировать историю, и подтверждённая копия остаётся в
prompt каждый ход. Для revision 8+ это особенно вредно: narrator уже получает
50–57 RAW units плюс uncovered tail, а карточка сцены превращает временную
ситуацию в долговременный повторяемый prompt-текст.

Live-вызов `533` исчерпал весь `LORE_CARD_DRAFT_OUTPUT_MAX_TOKENS=400`:
`reasoning_tokens=399`, content пустой, `finish_reason=length`. Успешный вызов
`522` также потратил 222 из 223 completion tokens на reasoning. Значит текущий
output budget фактически не является бюджетом видимого strict JSON.

`service_call_log.status=completed` в обоих случаях означает только успешный
HTTP/JSON transport: проверка `finish_reason` и прикладной схемы выполняется после
этой записи. `completed` не доказывает полученный Lore или GM draft.

Текущий Lore retrieval исправлять не требуется. Whole-match по title/keywords,
запрет self-trigger по hidden content, current-plus-three scan, whole-card budget,
exact card IDs в turn metadata и chips под ответом решают другую задачу.

## Decision

### Один ADR, три последовательные поставки

Поставка не должна блокироваться самой тяжёлой частью — RAW claim catalog.
Порядок работы:

1. **Detail + safe auto-route.** Additive `detail_level` для всех RP parties;
   безопасный трёхвариантный auto-route для RP revisions `9+` — там, где
   `RPGMService.enabled` уже истинно (`rp-gateway/app/services/rp_gm.py:54-55`).
   Новых model calls нет. Проверяется live на новой тестовой revision-11 партии.
2. **Typed Lore.** Authoring kind и законный `no_candidate` для всех RP parties с
   Lore Cards (revision `8+`), без revision gate: storage, retrieval и prompt не
   меняются — меняются draft-контракт, create-запрос и UI. Проверяется live на
   новой тестовой revision-11 партии.
3. **Gateway-owned RAW claims.** Exact RAW slots, Gateway-owned `before`, новое
   ранжирование и квота каталога реализуются под revision `12`; revisions `9..11`
   сохраняют whole-turn correction contract и текущее ранжирование.

Только третья mechanism-поставка поднимает source ceiling до `12`. Она не
повышает observed revision и не меняет WorldPack. Отдельная activation-поставка
поднимает выбранный WorldPack и observed revision до `12`, после чего создаётся
новая party. Existing parties не мигрируются и не являются compatibility target:
их удаление выполняется отдельной операционной процедурой, не этим механизмом.

Training и другие scenario types этим решением не меняются.

### Auto больше не открывает correction без явного выбора

Для RP revisions `9+` результат существующего `gm_intent` обрабатывается так:

- `scene` продолжает обычный narrator path;
- `correction` и `uncertain` возвращают `route_required` без GM draft и без
  mutation;
- `route_required` предлагает три действия: **Исправить факт**, **В сцену** и
  **Подробность ответа**.

Только повторный explicit request с `channel=gm` открывает correction draft. Этот
повтор не вызывает classifier заново, потому что `channel != auto` минует его уже
сейчас. Явная correction-кнопка и действие у конкретного memory fact по-прежнему
обходят classifier. Выбор «В сцену» повторно отправляет исходный текст как
`channel=scene`.

Выбор «Подробность ответа» не пересылает исходную фразу ни narrator, ни service
model. UI возвращает её в composer, открывает typed party setting и ничего не
записывает. После сохранения игрок сам решает, очистить, изменить или отправить
реплику в сцену.

Действие «Мастеру» становится launcher с двумя прямыми операциями:

1. **Исправить факт** — bounded correction из Decision 038.
2. **Подробность ответа** — `Обычно / Подробнее` в party settings.

Свободная GM-консоль, director note, добавление lore или правила через launcher не
появляются. Schema `gm_intent` и labels `scene|correction|uncertain` не
расширяются: третья кнопка является Gateway/UI route action, а не новым model
label.

Цена этого решения названа прямо: честная коррекция теперь стоит игроку один
лишний шаг, а каждый ложный `correction|uncertain` локальной модели стоит одного
прерывания. Частота прерываний измеряется в live-доказательствах первой поставки;
если она мешает обычной игре, следующим шагом сужается classifier, а не
расширяется диалог.

### `detail_level` — prompt setting, а не provider parameter

В существующий `parties.narrator_settings_json` добавляется optional party-level
поле:

```text
detail_level = default | expanded
```

Отсутствующее поле эквивалентно `default`, поэтому existing parties не меняют
поведение автоматически. Настройка доступна всем RP parties и любой active
narrator model без revision gate.

`default` не добавляет текст. `expanded` добавляет в существующие базовые narrator
rules ровно одну строку:

```text
Пиши подробнее обычного: развивай сцену через действия, реакции персонажей, значимые детали и уместный диалог; не повторяй известное, не решай за персонажа игрока и не обрывай сцену сразу после завязки.
```

Строка применяется к opening, ordinary turn, validation repair, prompt preview и
isolated autotest descendant. Free-form OOC text не сохраняется и не переносится
между ходами. Точное число слов, абзацев или событий не задаётся.

`detail_level` добавляется в `NarratorSettings`, иначе существующий
`extra="forbid"` его отклонит (`rp-gateway/app/models/schemas.py:364-370`). Он
читается до финальной prompt assembly и передаётся в `scenario_rules()`
(`rp-gateway/app/services/narrative.py:1218`) как внутренний prompt flag. Он не
реализуется в `apply_party_narrator_settings()` (`rp-gateway/app/main.py:4004`):
эта функция вызывается уже после сборки сообщений, продолжает владеть только
provider/model controls и не отправляет `detail_level` в transport payload.
Opening и `PromptInspector` получают то же значение явно; новый prompt block не
создаётся.

Сохранение продолжает использовать existing `PartyModelUpdate` и передаёт текущий
`model_profile_id`. При detail-only save на неизменной модели значения
`reasoning_effort`, `temperature`, `top_p` и `max_tokens` до и после сохранения
совпадают. При смене модели `detail_level` сохраняется, а несовместимые четыре
model-specific поля сбрасываются по прежнему правилу.

Смена `default -> expanded` один раз меняет system rules,
`stable_prompt_prefix_hash` и provider cache prefix. Следующие ходы с тем же
значением снова используют стабильную основу. Сохранение настройки меняет только
party settings и `updated_at`; turn, state version, GM correction, story-memory
artifact, relationship artifact и world-clock tick не создаются.

`expanded` не увеличивает `max_tokens` автоматически. **Только при
`detail_level=expanded`** ответ narrator с `finish_reason=length` получает
диагностический исход `truncated`: validation repair не запускается, обрезанный
ход не коммитится, UI предлагает увеличить `max_tokens` либо вернуться к «Обычно»
и повторить действие. Автоматического retry или повышения бюджета нет.

Ходы при `detail_level=default` сохраняют нынешнее поведение без изменений: в
narrator path сегодня проверки `finish_reason` нет вообще, и это решение её туда
не вносит. Общая политика обрезанных narrator-ответов — предмет отдельного
решения, а не побочный эффект настройки подробности.

### Decision 038 расширяется, а не получает новый slot layer

Всё в этом разделе относится только к revision `12` и третьей поставке. Revisions
`9..11` сохраняют текущие slots, текущее ранжирование и whole-turn correction
contract.

Форматы `memory:<field>:<fact_id>` и `rule:<id>` сохраняются. Для RAW прежняя
цепочка `raw:<turn_id> -> hash model-before` заменяется на Gateway-owned exact
slot:

```text
source_hash = SHA256(saved narrative_response UTF-8)
claim_id    = SHA256(canonical_json([turn_id,start,end,source_hash]) UTF-8)[0:20]
target_slot = raw:<turn_id>:<claim_id>
```

`start` и `end` — zero-based offsets в Unicode code points; `end` exclusive.
Canonical JSON не содержит whitespace и сохраняет указанный порядок массива.
`source_hash` считается по точной сохранённой строке без Unicode/newline
normalization и записывается lowercase hex. Публичные поля draft сохраняют
`target_id=str(turn_id)` и `target_turn_id=turn_id`; exact identity находится
только в `target_slot`.

Отдельный claim catalog не персистится. Gateway детерминированно пересобирает его
из авторитетных turns при draft и при confirm. Между этими действиями
`target_slot` живёт в публичном `PartyGMPatchDraft`; после confirm существующий
player-correction artifact хранит slot и exact `before`. Новая таблица или колонка
не создаётся.

RAW claims выделяются только из сохранённого `narrative_response`, включая
assistant-only `opening_scene`. Player message остаётся намерением и не может
через correction стать authority `user` fact. Каждый span:

- является дословной подстрокой одного narrator response;
- не превышает существующий 600-character correction limit;
- хранит `turn_id`, start/end offsets и source hash в вычисляемой identity;
- передаётся модели как `{target_slot, text, allowed_actions}`.

Разбиение остаётся малым и детерминированным внутри `rp_gm.py`: newline, затем
границы предложений; fragment длиннее 600 символов режется по последнему
whitespace до границы без потери или усечения текста. Если whitespace в первых 600
Unicode code points нет, применяется hard split ровно после 600 code points;
следующий fragment начинается с первого ещё не использованного code point.

#### Ранжирование и квота живут в payload, а не в каталоге

Instruction и candidate text проходят `casefold`, затем tokenization по
`[\w-]{3,}`; lexical overlap равен размеру пересечения двух множеств tokens. Это
заменяет нынешний подстрочный score (`rp-gateway/app/services/rp_gm.py:387-394`)
только под revision `12`. Memory, RAW и rule candidates ранжируются одним общим
списком по overlap, затем по recency и stable `target_slot`. Recency равен
`turn_id` для RAW, `max(source_turn_ids, default=0)` для memory и `0` для absolute
rule.

Внутри существующих восьми мест payload:

- резервируется один лучший целиком помещающийся memory candidate, если его
  lexical overlap больше нуля;
- резервируется один лучший целиком помещающийся absolute-rule candidate, если его
  lexical overlap больше нуля;
- остальные места заполняются общим рейтингом;
- один `turn_id` даёт не более четырёх RAW spans.

Это распределение внутри прежних `8 candidates / 4 000 characters`, а не новый
продуктовый лимит.

**Квота и рейтинг применяются только при сборке model payload (`patch_payload`).**
Каталог разрешения целей (`correction_candidates`) остаётся полным: при confirm и
при exact hint он обязан содержать все spans указанного turn. Иначе пятый и
последующие spans одного хода стали бы неподтверждаемыми — draft создавался бы, а
confirm возвращал «GM correction target is no longer available».

#### Hint matching работает в обе стороны

Поле `gm_target_slot` не переименовывается. Его rev12 semantics:

- `memory:<field>:<fact_id>` и `rule:<id>` остаются exact hints;
- существующий memory suffix `:replace|:retract` дополнительно ограничивает action;
- `raw:<turn_id>` является broad compatibility hint и сужает каталог до spans этого
  turn;
- полный `raw:<turn_id>:<claim_id>` является exact hint;
- неизвестный или устаревший slot даёт safe `no_target`.

Нынешний фильтр хинтов сравнивает только в одну сторону — `item == hint` либо
`hint.startswith(item + ":")` (`rp-gateway/app/services/rp_gm.py:379-385`). Такой
фильтр не способен раскрыть broad hint в span-кандидаты. Под revision `12`
добавляется симметричная ветка `item.startswith(hint + ":")`, иначе
`raw:<turn_id>` всегда давал бы пустой каталог и `no_target`. Valid exact hint
обходит квоту; целевые spans всё равно передаются целиком.

Существующий потолок двадцати активных correction slots
(`ACTIVE_PLAYER_CORRECTION_LIMIT`) не меняется числом, но под revision `12`
считает claims, а не ходы: один turn может занять несколько из двадцати. Это
принятое следствие точной адресации, а не регресс Decision 038;
`require_capacity_before_model` продолжает работать до вызова модели.

#### Service result

Модель больше не возвращает `before`, `field`, `target_kind`, `target_id` или
`section_key`. Gateway выводит их из выбранного candidate:

- memory использует field и section выбранного fact;
- absolute rule получает `field=null`;
- RAW всегда получает `field=canon`, `section_key=situation`
  (`rp-gateway/app/services/rp_story_memory.py:34`).

Это сознательно помещает пользовательское исправление RAW в общий долговечный
канон. Попытка угадать более узкую memory section потребовала бы ещё одного
classifier и снова передала бы authority модели.

Strict service result остаётся одним плоским object с
`additionalProperties=false`, а не root `anyOf`. Все поля обязательны; nullable
выражается типом поля:

```text
result           = draft | no_target
target_slot      = string | null
action           = replace | retract | null
after            = string | null
forbidden_claims = array
```

Для `no_target` три nullable-поля равны `null`, `forbidden_claims=[]`. Для `draft`
Gateway разрешает slot, подставляет exact `before` и формирует прежний публичный
`PartyGMPatchDraft` для preview/confirm. Для absolute rule модель по-прежнему
возвращает полный новый `forbidden_claims`; для остальных типов массив пуст.

На confirm Gateway заново строит spans, находит пересчитанный `claim_id` из
`target_slot` и принимает RAW draft только при полном совпадении slot и exact
`before`; offsets и source hash отдельно через client draft не переносятся. State
version, allowed action, отличающийся `after`, capacity, overlay latest 20 slots,
`gm_correction` history, authority `user`, one-section absorption valve и safe
coverage `min()` сохраняются по Decision 038.

Exact validator не становится fuzzy. `no_target` — валидный read-only исход,
semantic retry запрещён.

### Player Lore Card начинается с authoring kind

Раздел относится ко второй поставке и ко всем RP parties, где Lore Cards уже
существуют (revision `8+`). Revision gate здесь не применяется: любой новый
player draft/create обязан иметь kind. Таблица `lore_cards`, reads, retrieval и
narrator prompt не меняются. Untyped compatibility branch для старых партий,
клиентов и фикстур не сохраняется.

До model call игрок обязательно выбирает один kind:

```text
character | event | location
```

Кнопка под narrator response называется «Запомнить…». Light GUI отправляет ровно
один turn ID — тот, под которым нажата кнопка. `opening_scene` допустим:
техническая `[AUTO_START]`-реплика подавляется, в service prompt входит только
opening narration. Обычный `narrative` должен иметь полные player и narrator
messages. `gm_correction`, world command, training и незавершённые turns
недопустимы. `PartyLoreCardDraftRequest.source_turn_ids` сохраняет имя поля, но
принимает ровно один complete eligible turn (`min_length=1`, `max_length=1`);
range selector и multi-turn draft path не сохраняются.

`PartyLoreCardDraftRequest.kind` и `PartyLoreCardCreate.kind` обязательны и не
имеют default/null. Отсутствующий или неизвестный kind отклоняется schema
validation до model call, storage и audit. Все клиенты и фикстуры обновляются
одновременно; untyped create API не сохраняется.

Один существующий OpenRouter `lore_card_draft` call получает один source unit и
выбранный kind. Model остаётся `deepseek/deepseek-v4-pro`, input limit — 8 000
characters, output budget — 400 tokens. В payload обязательно добавляется:

```json
{"reasoning":{"enabled":false}}
```

Reasoning отключается, чтобы все 400 completion tokens были доступны strict JSON.
Бюджет не повышается, второй call, fallback и retry не добавляются.
`finish_reason=length` является failure, а не `no_candidate`.

Возможность отключить reasoning у этой модели — допущение провайдера, а не
установленный факт. Перед реализацией второй поставки выполняется один
изолированный canary-вызов, подтверждающий `reasoning_tokens=0`. Если провайдер
это не поддерживает, вторая поставка приостанавливается и выбор модели или бюджета
возвращается пользователю; молча повышать `max_tokens` нельзя.

Strict response остаётся одним плоским object с `additionalProperties=false` и
всеми required fields:

```text
result   = draft | no_candidate
kind     = character | event | location
title    = string | null
content  = string | null
keywords = array<string> | null
```

Gateway требует, чтобы output kind совпадал с input. Для `draft` `title/content` и
хотя бы один keyword непустые. Для `no_candidate` все три content fields равны
`null`. Неверная комбинация является malformed output и не повторяется
автоматически.

Kind semantics:

- `character` — устойчивый факт об индивидуальном персонаже: identity, role,
  knowledge, goal или habit; текущая эмоция и динамика отношений остаются в
  relationship layer;
- `event` — только завершившееся событие с долговременным последствием;
- `location` — устойчивое свойство места либо уже произошедшее длительное
  изменение места;
- scene recap, атмосфера, временная расстановка, общий итог диалога и «игрок
  пришёл/увидел/поговорил» без долговременного факта запрещены.

Организация, правило и понятие не являются ни одним из трёх model-draft kinds.
Модель обязана вернуть `no_candidate`, а не натянуть «Ночной Дозор» на `character`.
Игрок может выбрать конкретного члена организации, завершённое событие с её
участием или связанное место. Самостоятельная reviewed organization/rule/concept
card остаётся ответственностью автора WorldPack.

Draft только заполняет редактируемую форму. Existing ручная форма получает тот же
обязательный selector из трёх kinds и не предлагает `other`. При `no_candidate`
игрок может выбрать другой kind, другой turn или вручную описать объект одного из
трёх допустимых kinds. До explicit confirm storage не меняется.

Gateway не выполняет semantic classification свободного ручного текста: для direct
create он доверяет явно выбранному игроком authoring kind и проверяет только
структуру. Поэтому намеренно или ошибочно названная вручную organization card
технически возможна; запрещать её без semantic validator или удаления manual
create было бы отдельным изменением.

Kind не добавляется в таблицу Lore Cards и не участвует в retrieval. Конкретное
доказательство выбора хранится в существующем audit event
(`rp-gateway/app/main.py:1326`):

```text
audit_events.event_type = lore_card_created
event_json.authoring_kind = character | event | location
```

Каждое новое событие также сохраняет нынешние `card_id`, `title`,
`source_turn_ids` и `confirmed_by_player=true`. Исторические audit events без
поля остаются читаемыми и не мигрируются. Current `always_on=false`, явная галка
и organization/rule/clue WorldPack cards не переписываются: обязательность kind
относится только к новым player write requests и audit.

Current-plus-three scan, whole title/keyword match, hidden-content non-activation,
optional authored `always_on`, 4 000-character whole-card block, overflow order,
exact metadata IDs и UI chips не меняются.

#### Триггеры карточки и её выключение

Непустых `keywords` недостаточно: карточка `286` прошла бы и этот контракт.
Поэтому draft-инструкция и ручная форма требуют, чтобы каждый keyword точно
обозначал выбранного персонажа, событие или место. Общая сценическая лексика —
«проверка», «квартира», «телефон», «разговор», «утро» — запрещена как trigger.

Честная граница, измеренная на `party_cac70558b50a`: одно это правило описанный
случай **не предотвратило бы**. Активацию там поддерживали именно точные
обозначения — `Игорь`, `Игоря`, `кот`, — и по контракту `event` они законны.
Реальная причина долгой жизни карточки в другом: `recent_rp_scan_text` включает
ответы нарратора за три предыдущих хода (`rp_history.py:115`), поэтому карточка о
продолжающемся сюжете поддерживает себя сама, пока нарратор о нём пишет. Это
поведение retrieval из Decision 037; решение 042 его не меняет и не притворяется,
что чинит. Если такой self-sustain признаётся дефектом, источник scan-текста
меняется отдельным решением, а не этой поставкой.

Из этого следуют требования к доказательству, а не новые лимиты:

- сохранённая на живой партии карточка проверяется человеком как долговечный
  факт, а не как пересказ сцены; проверка выполняется на реальной карточке, а не
  только на canary;
- когда триггеры карточки исчезают из окна `current+3` целиком — то есть сцена
  ушла от темы и нарратор о ней больше не пишет, — карточка обязана пропасть из
  `lore_card_ids` следующего хода;
- намеренное точное упоминание включает её снова, а последующий выход триггеров
  из окна снова выключает;
- приватная проверка фиксирует, какой именно trigger и из какого хода активировал
  карточку, и отдельно — пришёл он из сообщения игрока или только из текста
  нарратора. Новая production-телеметрия не нужна: это вычисляется из уже
  сохранённых `turns` и `keywords` тем же whole-match правилом, что и retrieval.

### Failure UX

Публичный UI не показывает внутренние ошибки наподобие `GM patch before value is
not present in the target RAW turn`.

- `route_required` объясняет, что ничего не отправлено и не записано;
- correction `no_target` предлагает уточнить факт или выбрать конкретный memory
  fact;
- Lore `no_candidate` предлагает другой kind/turn либо ничего не сохранять;
- Lore/GM `finish_reason=length` показывает, что service draft оборван и не принят;
- narrator `truncated` при `expanded` предлагает увеличить `max_tokens` или
  вернуться к «Обычно», не выдавая repair за продолжение ответа;
- malformed service output предлагает повторить позже; игра и память не изменены.

Точные ошибки остаются в существующей admin/service diagnostic surface.

## Consequences

- Обычная фраза больше не попадает в мутирующий correction flow только потому, что
  classifier уверен в label; взамен честная коррекция стоит одного лишнего явного
  шага.
- Настройка длины ответа становится явным party control и проверяется на новой
  revision-11 партии без миграции старой.
- После второй поставки новую player Lore Card нельзя создать без явного
  authoring kind; ранее сохранённые cards читаются и извлекаются как раньше.
- Local Gemma выбирает bounded slot, но не сочиняет immutable `before` и не
  угадывает story-memory field.
- RAW spans не могут единолично занять восьмислотовый payload, а релевантные
  memory/rule candidates сохраняют место; каталог разрешения при этом остаётся
  полным.
- Потолок двадцати активных corrections под revision 12 считает claims, поэтому
  один ход может занять несколько слотов.
- Lore draft может честно вернуть пустой результат и получает все 400 tokens на
  JSON вместо hidden reasoning.
- Lore storage и retrieval остаются простыми: authoring kind нужен для создания и
  audit, но не превращается в новый runtime artifact.
- Decision 042 не мигрирует и не удаляет parties, cards, corrections или audit
  history. Exact RAW claims проверяются только в новой revision-12 party после
  отдельной activation; удаление старых партий является отдельной операцией.
- Новая database column, таблица, dependency, service role или provider не нужны.

## Non-goals

- universal free-form GM console, persistent director notes или natural-language
  narrator settings;
- fuzzy/semantic matching `before`, embeddings, LLM judge истины RAW или случайный
  target при низкой уверенности;
- свободное добавление facts, rules, state patch или lore через correction;
- semantic classifier для выбора RAW memory field;
- общая политика обрезанных narrator-ответов вне режима `expanded`;
- точное число слов/абзацев, автоматическая смена `max_tokens` или retry
  обрезанного narrator response;
- автоматическое сохранение Lore Card, scene recap cards, retry до непустого draft
  или semantic validator карточки;
- semantic policing или автоматическая переклассификация вручную введённой Lore
  Card;
- изменение Lore retrieval, prompt budget, relationship mechanics, story-memory
  sections, world clock или authored WorldPack cards;
- миграция или bulk deletion existing parties/cards/corrections, новая
  dependency, telemetry, provider, model route или NVIDIA fallback.

## Verification gates

### Source and offline contract

- `channel=auto` с `gm_intent=correction|uncertain` возвращает трёхвариантный
  `route_required`; только explicit `channel=gm` создаёт draft, и повторная
  отправка не вызывает classifier второй раз;
- выбор narrator settings не пересылает исходный текст и не создаёт turn,
  state/memory/GM/world-clock artifacts;
- `detail_level` принимается `NarratorSettings` с `extra="forbid"`, читается до
  prompt assembly и отсутствует в provider payload;
- detail-only save сохраняет прежний `model_profile_id` и значения всех четырёх
  model-specific fields; model switch сохраняет detail, но применяет прежний
  capability reset к остальным;
- exact expanded string присутствует ровно один раз во всех narrator paths;
- narrator `finish_reason=length` при `expanded` даёт `truncated` без repair и
  commit; при `default` поведение не изменилось ни в одном тесте;
- payload соблюдает прежние `8 / 4 000`, резервы positive-overlap memory/rule и
  максимум четыре RAW spans одного turn; при этом каталог разрешения возвращает все
  spans хода, и пятый span того же turn успешно проходит confirm;
- broad hint `raw:<turn_id>` возвращает spans этого turn, exact
  `raw:<turn_id>:<claim_id>` возвращает ровно один span, неизвестный slot даёт
  `no_target`;
- service GM result не содержит `before` или `field`; RAW получает
  `canon/situation` от Gateway;
- GM и Lore используют плоский strict discriminator без root `anyOf`;
- Lore reasoning отключён в exact outbound payload; output budget остаётся 400;
- UI и draft API отправляют ровно один eligible turn; opening допустим;
- draft/create без `kind` отклоняются schema validation до model call: service
  call, card row и `lore_card_created` отсутствуют;
- `no_candidate` не создаёт форму/card и не вызывает retry;
- каждый новый `lore_card_created.event_json.authoring_kind` содержит один из
  трёх non-null kinds и совпадает с выбором игрока; ранее сохранённая card без
  storage-kind продолжает читаться и извлекаться;
- revisions `9..11` сохраняют whole-turn correction contract, прежние slots и
  прежнее ранжирование — доказывается тестом, а не утверждением;
- существующие `mock_intent` и `mock_patch`
  (`rp-gateway/app/services/rp_gm.py:588-612`) переписаны под route и slot-only
  contracts; offline tests запрещают model-owned `before/field`.

Если к моменту первого реального code slice registry ещё является действующим
контрактом репозитория, создаётся `registry/042.yml` с отдельными requirements для
safe explicit correction/detail, typed Lore и exact RAW claims. Каждый получает
уровень не выше фактически доказанного по Decision 022. До появления кода
`каркас` не заявляется. Старый staged rollout ради заполнения registry не
выполняется.

### Provider canary

Перед второй поставкой выполняется отдельный pre-flight canary: один
`lore_card_draft` с `reasoning.enabled=false`, у которого
`usage_json.completion_tokens_details.reasoning_tokens = 0`. Отрицательный
результат останавливает вторую поставку и возвращается пользователю.

Для новых isolated canary request IDs существует минимум один `gm_patch_draft` и
четыре bounded `lore_card_draft`: по одному ожидаемому `character`, `event`,
`location` и законному `no_candidate`. Во всех сохранённых raw responses:

- `choices[0].finish_reason != length`;
- content не пуст и проходит новый flat strict contract;
- для Lore `usage_json.completion_tokens_details.reasoning_tokens` существует и
  равно нулю.

Каждый Lore draft endpoint возвращает HTTP 200, после чего Gateway принимает его
как `draft` соответствующего kind либо как `no_candidate`; transport JSON,
отвергнутый cross-field validation, gate не проходит.

Сохранённые результаты четырёх Lore canaries отдельно проверяются человеком или
read-only acceptance fixture на отсутствие scene recap и выдуманного факта. Это
acceptance evidence, а не runtime semantic retry или LLM validator.

Capture-test отдельно доказывает `reasoning.enabled=false` в outbound Lore payload,
потому что `service_call_log.prompt_text` хранит messages, а не полный transport
payload. `service_call_log.status=completed` сообщается только как transport
evidence и не считается доказательством принятого draft. Исторические строки с
`finish_reason=length` не удаляются и не переписываются.

### Real-party evidence

После первой поставки новая RP party, созданная после apply с revision `11`,
проверяет detail/safe-route chain:

```text
ordinary channel=auto + correction-like text
-> route_required with three choices
-> no gm_patch_draft before explicit choice
-> choosing Подробность ответа restores text and opens settings
-> no turn or mechanic mutation

detail_level=expanded saved
-> fixed string appears exactly once in next recorded prompt
-> stable_prompt_prefix_hash changes once
-> next expanded turn in the same RAW-anchor keeps that hash
-> no GM/state/memory/world-clock artifact from the setting save
```

Отдельно сообщается наблюдение по прерываниям: сколько ходов подряд было сыграно и
сколько из них вызвали `route_required`, с распределением labels
`scene|correction|uncertain` из `service_call_log`. Это наблюдение, а не порог:
решение о сужении classifier принимает пользователь.

Субъективное «ответ стал подробнее» оценивается игроком и не заменяет проверяемый
gate.

После второй поставки новая RP party, созданная после apply с revision `11`,
проверяет Lore chain:

```text
eligible turn + chosen authoring_kind
-> valid draft or honest no_candidate
-> explicit confirm only for draft
-> lore_card_created audit contains authoring_kind
-> saved card read by a human as a durable fact, not a scene recap
-> later whole title/keyword mention
-> exact card ID in narrator prompt metadata and UI chip
-> named trigger and source turn for every activation, player text or narrator text
-> triggers leave the current+3 window entirely
-> card absent from the next turn lore_card_ids
-> deliberate exact mention brings it back
```

Каждая активация отчитывается парой «trigger — ход, из которого он взят» и
пометкой, пришёл ли он от игрока или только из текста нарратора. Цепочка
считается пройденной только если хотя бы одно выключение наблюдалось на реальной
партии; «карточка всё ещё в промте» результатом не является.

После третьей поставки и отдельной activation новая revision-12 party проверяет
correction chain:

```text
auto correction -> explicit route choice
-> exact raw:<turn_id>:<claim_id>
-> Gateway-owned before + canon/situation
-> confirm -> overlay -> one section -> authority user
-> absorbed -> later narration
```

Статусы local, committed, pushed, applied, HTTP-verified и browser/live-verified
сообщаются раздельно.

## Принятые развилки

Три развилки зафиксированы этим решением и не пересматриваются по ходу
реализации без отдельного изменения ADR:

1. **`truncated` только при `expanded`.** Общая политика обрезанных
   narrator-ответов не вводится, потому что сегодня её нет вовсе и она изменила бы
   поведение коммита у всех текущих партий.
2. **Typed Lore без revision gate и untyped compatibility branch.** Kind является
   обязательным входным инвариантом каждого нового player draft/create request;
   nullable default и legacy create path не вводятся. Текущие партии не являются
   compatibility target и удаляются отдельной операцией.
3. **Новое ранжирование и квота — только revision 12.** Под revisions `9..11`
   отбор целей не меняется, потому что там нет spans и менять нечего.

## Related decisions

- [Decision 022](022-readiness-and-observability-policy.md)
- [Decision 032](032-rp-history-first-prompt-and-sectioned-memory.md)
- [Decision 037](037-rp-authored-lore-cards-and-confirmed-drafts.md)
- [Decision 038](038-rp-gm-corrections-and-player-overlay.md)
- [Decision 041](041-rp-narrative-presets-and-opening-seeds.md)
