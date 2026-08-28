# Live evidence manifest for Decision 042

**Снято:** 2026-08-27T12:42:24Z (UTC) read-only из `/data/rp_gateway.db`
(`mode=ro`) внутри контейнера `rp-gateway`.
**Checkout на момент снятия:** `80ab6d3` (`codex/rp-gm-lore-adr`).
**Мутаций не выполнялось.**

Этот файл — обезличенный указатель на production-доказательства, на которых стоит
[Decision 042](../042-rp-explicit-gm-and-typed-lore-drafts.md). Он содержит только
идентификаторы, метаданные и SHA-256. Полные `prompt_text`, `raw_response` и
тексты карточек в репозиторий не попадают: они хранятся приватно вне Git и
сверяются по хэшам из этой таблицы.

**Приватный полный экспорт:** `evidence-raw.json`, `evidence-566.json`,
`evidence-cards.json`, `evidence-prompt.json`
(SHA-256 основного файла: `40665fbadc80ec0d6f7d848ffbc77a607896cda2f6c66ca91923283ee5130c91`).

## Сохранённый party-closure — срез 1 Decision 043

**Manifest:** `/srv/backups/rp-evidence-2026-08-28/manifest.json` на abykovserv.
SHA-256 manifest:
`e0bae901f6f5f69242e6c745d0a1fa21feb0b0c7cc7bb696b35e5307c56d8cdd`.

**Payload:** `/srv/backups/rp-evidence-2026-08-28/payload.tar`, 398 540 800 bytes.
SHA-256 всего tar:
`d7dbf2622a0c458a320e7d33acedd2aac7b7260141e20eeb4542e7ca00d15c1c`.
Manifest находится рядом с tar и не входит в собственный hash: hash каждого из
11 файлов внутри tar записан в manifest, а hash manifest закреплён здесь.

Источник открыт только с `mode=ro`; согласованная копия получена через
`sqlite3.Connection.backup()` 2026-08-28T17:49:28–17:49:29Z. Из неё создана
**новая** SQLite с выбранными строками, а не полный dump с удалёнными записями.
Архив зафиксирован в 17:49:47Z; отдельное повторное чтение прошло в 17:51:07Z.

При финальной сверке первой выгрузки обнаружены 50 пропущенных service calls
веток: их `party_id` хранит `state_campaign_id` ветки. Критерий исправлен;
в окончательной выгрузке **153** calls: 75 / 16 / 12 / 50 по четырём партиям.
Все остальные таблицы и прежние выбранные строки совпали по полным row hashes.
Первоначальные tar и manifest сохранены без изменения байтов в `attempt-1/`;
их прежние SHA записаны в `supersedes` нового manifest. Первая попытка не
считается доказательством полноты. Обе попытки охватывают только те же четыре
партии; ни одна исходная строка или партия для исправления не менялась.

Внутри — `party-evidence.sqlite3` и десять `state/**/current.json`. Сохранены
четыре партии, десять кампаний, основные ходы **19 / 7 / 5 / 168** и 1031 строка
ходов шести веток «Старосты»: всего **1230** строк turns. Расширение с прежних
17 ходов V2 и включение всех веток подтверждены владельцем. Все 168 committed
RAW «Старосты» сохранены без курирования и обрезки.

В выгрузке 26 непустых прикладных таблиц и внутренний `sqlite_sequence`;
полная матрица охвата всех 45 исходных таблиц находится в manifest. Остальные
73 партии не выгружены; их party/campaign/branch IDs не найдены в выбранных
TEXT/JSON и state-файлах. Таблицы `users`, `sessions`, `showroom_visitors`,
`provider_api_keys` отсутствуют. Единственные изменения данных — 20
`owner_user_id → NULL`: parties 4, player_characters 4, party_branches 6,
autotest_runs 6. Остальные колонки, включая JSON и доказательные key/hash/token
поля, сохранены без изменений и сверены по всем строкам.

**Applied SHA:** `865a2ef66c709d02c3326ca7cf48fa617fba74da`. Это подтверждено
журналом успешного Ansible apply (`failed=0`, завершение 14:23:12Z) и совпадением
50 файлов Git / host deployment / работающего Gateway. Apply выполнен вне
этой задачи; срез 1 не менял runtime, deployment или live state.

**Повторная проверка:** все 11 member hashes и hash tar совпали; извлечённая
SQLite открыта с `mode=ro`, `integrity_check=ok`, `foreign_key_check` пуст,
`count(parties)=4`, основные counts — 19/7/5/168, service calls — 75/16/12/50.
Хэши всех строк 26 таблиц сверены повторно. Полная временная копия и
рабочий каталог удалены до фиксации manifest; временная копия для повторного
чтения тоже удалена. Источник SQLite и state-файлы не изменялись этой задачей.

На архиве воспроизведены все восемь calls из таблицы ниже и их prompt/response
hashes. Draft 566 совпадает с card 286 по title/content/keywords, audit 1870
подтверждает ручное сохранение. Card 286 находится ровно один раз в каждом
**фактическом** сохранённом prompt 1867–1881, с тем же content hash; metadata
подтверждает те же IDs. Это 15 ходов на момент snapshot. Историческое слово
«десяти» было неточным уже для диапазона 1867–1878 из 12 ходов; теперь V2
продолжена до 1881. Фактический metadata path — `prompt_assembly.lore_card_ids`.
Исторический общий запрос `authored_key IS NULL` давал две карты; в текущем
согласованном источнике и архиве он даёт одну — 286. Причина изменения второй
исторической записи этим срезом не исследовалась.

Для повторения запросов ниже извлекают только `party-evidence.sqlite3` из tar
во временный приватный каталог вне архива, открывают его URI с `mode=ro`,
закрывают соединение и удаляют временный файл. Gateway/PartyStore для чтения
архива не запускаются: их startup-миграции недопустимы.

**Хранение относится ко всей директории архива**, включая tar, каждый payload
member, manifest и сохранённую первую попытку `attempt-1/`. Она переживает
purge; удаление допустимо только после
replacement evidence сохраняемых механик и отдельного явного решения
владельца о retention. Каталог имеет режим `0500`, файлы — `0400`; hash
manifest закреплён в Git. Это read-only seal, **не WORM и не `chattr +i`**:
владелец/root технически может изменить права. Изменения архива на месте
запрещены правилами хранения. Сохранение архива не является разрешением на
удаление партий или доказательством приёмки нового движка.

## Партии, которые нельзя удалять до замены доказательства

| Party | Revision | Роль в доказательстве |
| --- | --- | --- |
| `party_30fd9d3cc6ef` | 10 | Отказы GM-коррекции: `525` (обрыв по бюджету), `527` (выдуманное правило), `529` (в `before` попала внеигровая реплика игрока); первый scene-recap draft `522` |
| `party_3e09b9092765` | 11 | Lore draft `533`: весь output-бюджет ушёл в reasoning, content пустой |
| `party_cac70558b50a` | 11 | **Полная цепочка дефекта**: draft `566` → сохранённая карточка `286` → её попадание в prompt двенадцати подряд ходов при исторической сверке; пятнадцати — в архиве среза 1 |
| `party_16c210a8a099` | 0 | Историческая probe-цель `registry/020.yml` и `registry/021.yml`; в срезе 1 Decision 043 требования понижены до `подключено`, RAW сохранён для seeded memory run |

## Service calls

| ID | UTC | role | provider | model | finish | completion | reasoning | party | turn | sha256(prompt)[:16] | sha256(response)[:16] |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `522` | 2026-08-27T09:14:47Z | `lore_card_draft` | openrouter | `deepseek/deepseek-v4-pro` | `stop` | 223 | 222 | `party_30fd9d3cc6ef` | 1849 | `68d8aa062ac35f9a` | `20c32bd5625dc38d` |
| `525` | 2026-08-27T09:25:05Z | `gm_patch_draft` | local | `gemma-4-26b-a4b-it-rp-q4` | `length` | 300 | — | `party_30fd9d3cc6ef` | — | `1684a3ae933e4f5b` | `ec6118ebcfdd0005` |
| `526` | 2026-08-27T09:26:37Z | `gm_intent` | local | `gemma-4-26b-a4b-it-rp-q4` | `stop` | 18 | — | `party_30fd9d3cc6ef` | — | `fee55049dd2ab1cb` | `6b5fd049711c19a8` |
| `527` | 2026-08-27T09:27:02Z | `gm_patch_draft` | local | `gemma-4-26b-a4b-it-rp-q4` | `stop` | 109 | — | `party_30fd9d3cc6ef` | — | `1dec4c6c58ee5861` | `f60bb8a8e22f8450` |
| `528` | 2026-08-27T09:27:46Z | `gm_intent` | local | `gemma-4-26b-a4b-it-rp-q4` | `stop` | 40 | — | `party_30fd9d3cc6ef` | — | `e51d7d0ea32fdcb1` | `5521e8c71f1f00b8` |
| `529` | 2026-08-27T09:27:56Z | `gm_patch_draft` | local | `gemma-4-26b-a4b-it-rp-q4` | `stop` | 140 | — | `party_30fd9d3cc6ef` | — | `f89b4028c6719722` | `7e5395f823b3198d` |
| `533` | 2026-08-27T09:53:48Z | `lore_card_draft` | openrouter | `deepseek/deepseek-v4-pro` | `length` | 400 | 399 | `party_3e09b9092765` | 1853 | `146f37bc0511ab12` | `582181829b15872c` |
| `566` | 2026-08-27T12:14:30Z | `lore_card_draft` | openrouter | `deepseek/deepseek-v4-pro` | `stop` | 328 | 0 | `party_cac70558b50a` | 1866 | `766c71abaaf75089` | `aae4cdbfb5ef66d2` |

Все восемь строк имеют `status=completed`: это только успешный HTTP/JSON
transport. Прикладной исход у `525` и `533` — отказ.

Про `566`: `reasoning_tokens=0` наблюдалось **без** `reasoning.enabled=false` в
payload — такого поля в текущем коде нет. Строка доказывает, что нулевой
reasoning достижим и что strict JSON при этом валиден, но не доказывает, что
провайдер позволяет reasoning отключить. Pre-flight canary из решения остаётся
обязательным.

## Полная цепочка дефекта Lore (party_cac70558b50a)

| Звено | Значение |
| --- | --- |
| Источник | turn `1866`, narrator `openai/gpt-5.6-luna-pro`, `fallback=false`, `validator_valid=true` |
| Draft | service call `566`, strict JSON валиден, `finish_reason=stop` |
| Подтверждение | audit `1870`, `event_type=lore_card_created`, `confirmed_by_player=true`, sha256 `20529bb2f67375d1c81978a519d408531a492415247db46663d6e4b285465eb6` |
| Карточка | `lore_cards.id=286`, `always_on=0`, `enabled=1`, `source_turn_ids=[1866]`, 642 символа, 12 keywords |
| | sha256(title) `8a10e0b04137ba38a0daefbec7af7a4e6e6822eca8b2efea66e38a1c4494613a` |
| | sha256(content) `0fb942d2182944956a4776cb40891578a364439e30a5eb0ea188b9bffc80f57c` |
| Попадание в prompt | `turns.metadata_json.prompt_assembly.lore_card_ids` содержит `286` в ходах 1867–1878 — двенадцать подряд на момент повторной сверки 2026-08-27T13:0xZ; архивная сверка дополнительно проверяет фактические prompts до 1881 |

### Чем именно активировалась карточка

Пересчитано тем же whole-match правилом, что и retrieval
(`state_store.py:lore_cards_for_prompt`, `whole_match=True`), по окну
`recent_rp_scan_text` — три предыдущих хода целиком плюс текущая реплика игрока
(`rp_history.py:115`). Новая телеметрия не потребовалась.

| Ход | Триггеры из реплики игрока | Триггеры только из текста нарратора |
| --- | --- | --- |
| 1867 | `Игоря` (в окне) | `Игорь`, `кот`, `магический след`, `телефон штаба`, `квартира` |
| 1868 | `Игоря` (в окне) | `Игорь`, `кот`, `магический след`, `телефон штаба`, `квартира` |
| 1869 | `Игоря` (в окне) | `Игорь`, `кот`, `магический след`, `телефон штаба`, `квартира` |
| 1870 | **нет** | `Игорь`, `кот`, `магический след` |
| 1871 | **нет** | `Игорь`, `кот`, `Демиург` |
| 1872 | **нет** | `Игорь`, `Игоря`, `кот`, `Демиург` |
| 1873 | **нет** | `Игорь`, `Игоря`, `кот`, `Демиург` |
| 1874 | **нет** | `Игорь`, `Игоря`, `кот` |
| 1875 | `Игоря` (текущая реплика) | `Игорь`, `кот` |
| 1876–1878 | `Игоря` (в окне) | `Игорь`, `Игоря`, `кот` |

Пять ходов подряд (1870–1874) карточка держалась в prompt **без единого
упоминания игроком** — ни в текущей реплике, ни в просканированных репликах: её
поддерживал только текст нарратора. Это наблюдаемый self-sustain, а не гипотеза.

Второе, что показывает та же таблица: общая сценическая лексика (`квартира`,
`телефон штаба`, `проверка`) держала карточку лишь первые три хода, а дальше
работали точные обозначения `Игорь`/`Игоря`/`кот`. Запрет общих keywords сам по
себе этот случай не предотвратил бы.

При исторической сверке это был единственный в базе экземпляр player-created
карточки, прошедшей весь путь до narrator prompt: тогда в `lore_cards` были две
записи с `authored_key IS NULL`, вторая (`id=1`) относилась к партии 2026-08-05
и в prompt не входила. Результат текущей архивной сверки указан выше отдельно.

## Воспроизведение

Read-only, из контейнера Gateway:

```python
import sqlite3
c = sqlite3.connect('file:/data/rp_gateway.db?mode=ro', uri=True)
c.execute("select id,role,status,provider,model,party_id,turn_id from service_call_log where id in (522,525,526,527,528,529,533,566)").fetchall()
c.execute("select id,campaign_id,always_on,enabled,source_turn_ids_json from lore_cards where authored_key is null").fetchall()
c.execute("select id,metadata_json from turns where campaign_id='party_cac70558b50a' order by id").fetchall()
c.execute("select id,event_type,event_json from audit_events where event_type='lore_card_created'").fetchall()
```

`finish_reason` и `usage` читаются из `raw_response`/`usage_json` тех же строк.

## Условия удаления партий

Механизм Decision 042 историю не чистит. Явное удаление партии допустимо только
после того, как выполнены все пункты:

1. снят приватный snapshot базы, открывающийся в `mode=ro`, и на нём
   воспроизведены запросы из раздела «Воспроизведение»;
2. зафиксированы время снятия, SHA-256 snapshot и applied SHA сервера;
3. для `party_16c210a8a099` получена эквивалентная causal chain на новой партии и
   заменён `probe_command` в `registry/020.yml` и `registry/021.yml` — либо
   соответствующие требования честно понижены до `подключено`;
4. эквивалентные контракты typed Lore и safe explicit correction из Decision 042
   проверены на новом `RPTurnEngine` по воротам Decision 043; отдельная activation
   старого revision-11/12 пути не требуется;
5. этот манифест обновлён ссылками на новые party/call ID либо на immutable
   party-closure с manifest SHA-256, из которого воспроизводятся запросы выше.

`DELETE /api/parties/{party_id}` удаляет `service_call_log`, `turns`,
`audit_events`, `state_versions`, `lore_cards` и остальные данные партии
перечислением таблиц (`party_store.py:1947`), поэтому операция необратима.
Отдельное ограничение того же метода: он обрабатывает не более 200 веток партии;
на текущих данных максимум — 6, но как общий контракт удаления это дефект.
