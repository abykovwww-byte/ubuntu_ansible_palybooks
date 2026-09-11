# Decision 043 — Lore/Correction boundary and partial gameplay

**Дата:** 2026-09-11. **Поручение:** «Делай сам дальше» после production
closure Story Memory. Это agent-played проверка Codex, не новая человеческая
оценка Narrator и не закрытие Plan 029 §6.2/§6.3.

## Контур и воспроизводимые anchors

Production в начале проверки: applied `7d92dc475756e7ab96d8dee077900df47dda7d3e`,
Gateway image `sha256:393e39d5ba77b8b5dc7ce13e49cffb12d69e8e31684f7e381e900a85f392c3ab`.
Новые игровые записи создавались только в отдельных acceptance SQLite. Auth
выключен только у этих контейнеров, внешние порты не опубликованы. Production
SQLite, аккаунты, WorldPack sources и narrator revision не изменялись.

| Party | Сценарий | Пройдено к остановке |
|---|---|---|
| `party_a692ad1189a4` | `book-independent`, Павел Орлов | committed v1–v6, включая opening |
| `party_b6268e8e8884` | `action-night-trainee` | committed v1–v5, включая opening |

Общий World hash: `cb32e65c02ee59101b4270a6a350ce72061ca23a68ce55b2a7d8169d7e8d086e`.
Scenario hashes: основной `b94579e02ca3123506f85014233172743926f18a344f2d870f9d6c70c4241bed`,
контрастный `7f001c5084230d4ff1db8b13a35f36075f094b6eba83a7cf6f5ebe04aefefb58`.

Маршруты не менялись: Narrator Luna → exact `openai`, Atomic V4 Pro → exact
`baidu/fp8`, Administrator → local `gemma-4-26b-a4b-it-rp-q4`. Fallback запрещён,
Atomic reasoning выключен. Действия игрока задавал Codex после чтения сцен;
HTTP-клиент выдерживает минимум 60 секунд после предыдущего ответа той же
Party. Повторные RAW replay для диагностики не считаются новыми игровыми ходами.

Server evidence root: `/srv/app-data/rp-stack/decision043-acceptance/`:

- `endurance-7d92dc4-20260911/`: исходные RAW и все шесть неуспешных Lore calls;
- `lore-output-c2d920f/data/`: только короткая инструкция/field bounds;
- `lore-output-c0906de/data/`: плоская Lore wire schema;
- `lore-output-c219177/data/`: разделение говорящего и адресата;
- `lore-output-5f23157/data/`: сохранение сравнения/неопределённости, шесть
  replay и настоящие main v5 / contrast v3;
- `lore-output-87ada44/data/`: SQLite backup предыдущего checkpoint, затем
  настоящие main v6 / contrast v4–v5 и принятые player operations.

В каждом контуре `rp_engine.db` содержит authoritative Party/RAW/jobs/cards;
`rp_gateway.db.service_call_log` содержит redacted actual prompt/response,
provider/model, latency и usage. `lore-replay-evidence.json` сохраняет результаты
replay; `acceptance-client-events.jsonl` — HTTP действия, решения и ответы.
Checkpoint создан SQLite backup API в новые файлы с `integrity_check=ok`, без
перезаписи предыдущих данных. Одинаковые Party IDs относятся к разным явно
указанным БД; evidence нельзя суммировать как независимые длинные партии.

## Обнаруженный дефект и проверенные варианты

На applied image шесть runtime Lore calls дали `finish_reason=length`, каждый
израсходовал 2048 output tokens. JSON не заканчивался: модель раздувала title
пересказом и повторениями. Карточки не записались (`0/6`), Gateway сохранил
terminal errors, а не обрезал невалидный ответ для записи.

`c2d920f` добавил ориентир короткого title/content и вывел существующие строгие
пределы 200/4000 символов в JSON Schema. Этого оказалось недостаточно: пять
replay снова оборвались, шестой получил transport error. Отдельный контрольный
вызов с тем же prompt, но без корневого `oneOf`, завершился на 259 tokens.
Это evidence зависимости от формы wire schema на проверенном endpoint, а не
утверждение о внутренней реализации provider.

`c0906de` убрал корневое `oneOf`, сохранив required fields, enum/null types,
strict extra-field rejection и весь post-parse discriminator validator.
Шесть ответов завершились, но чтение выявило ошибку: обращение к Павлу стало
именем говорящего NPC. `c219177` разделил говорящего/адресата и запретил вывод
способностей наблюдателя из самого факта наблюдения. Следующий replay ещё
превращал «вздрогнул, словно услышал приказ» в факт приказа. `5f23157` добавил
общее правило: сравнение/предположение не является событием, совпадение во
времени не доказывает причинность. Имена и правила конкретного мира в эти
инструкции не добавлялись.

Итоговый replay: `6/6` runtime Lore, `stop`, 122–207 output tokens,
4.694–6.827 секунды. На двух следующих Party вместе с пятью настоящими
продолжениями: `11/11` runtime Lore calls завершились и записали карточки,
107–207 output tokens, 4.694–6.946 секунды. Это bounded observed результат,
а не гарантия отсутствия будущих смысловых ошибок.

## Реальный PlayerCorrection

Contrast v3 ошибочно заменил время 17:42/17:47 на «семь сорок две/семь сорок
семь вечера». Первая player correction job `25` в `5f23157` оборвалась на
2048 tokens: поле `target_slot` бесконечно перечисляло выдуманные IDs.
HTTP ожидание вернуло `504 rp_player_operation_pending`, затем job стала
terminal failed. Неуспех оставлен в checkpoint и итоговых счётчиках.

Контроль без `oneOf` завершился, но вернул весь большой фрагмент и широкий
`raw:7`; результат не прошёл прежний предел 600 символов и не применялся.
`87ada4459bb895cda44a158cb70a9e2419619563` сделал correction wire schema плоской,
передал прежний предел `after=600` в schema и уточнил копирование ровно одной
точной цели из candidate. Gateway по-прежнему проверяет target membership,
discriminator, before/catalog/version, owner и explicit accept. RAW неизменяем.

С тем же запросом новая job `27` вернула draft за 5.186 секунды HTTP
(5.107 model call), 105 output tokens, `stop`. Proposal `1` выбрала
`raw:7:689d07054e2ff753b658`, исправила время крика на пять сорок две вечера.
Явный accept создал overlay revision `1`, applies-to version `4`.
Это одна правка, не массовая замена всех упоминаний в RAW.

## Цепочки до последующей сцены

Идентификаторы call/card ниже относятся к конечному checkpoint `87ada44`.

| Механика | Запись/решение | Следующий фактический prompt | Отдельное продолжение |
|---|---|---|---|
| Runtime Lore | main cards 1–4; card 8 после имени Антона в v5 | call 18 содержит cards 1–4; call 25 содержит также card 8 | main v6 различает Павла и Антона, Антон исследует след; неизвестная женщина отдельно называет себя Алиной |
| PlayerCorrection | job 27 → proposal 1 accepted → overlay revision 1 | contrast call 29 содержит exact target/after; call 32 уже без overlay | v4 использует 17:42/17:47; v5 снова 17:42, без повторного применения overlay |
| Player Lore location | job 31 → card 10, explicit confirm | contrast call 29 содержит card 10; call 32 — cards 10 и 11 | v4 группа приезжает на Верхнюю Масловку 18к2 и видит открытый замок; v5 проверяет обзор квартиры 48 и сохраняет противоречие показаний/осмотра |

Перед confirm карточки места Codex как тестовый игрок поправил draft:
восстановил «по словам Климова»/«по его памяти». Исходный draft терял это
уточнение, поэтому автоматическая semantic acceptance всего player Lore не
заявляется. Отдельный character draft job `26` повторял уже имеющуюся карточку
Антона; он не подтверждался и не считается успехом duplicate/no-candidate gate.

Prompt projection проверена по actual `service_call_log.prompt_text`, не только
локальной сборке prompt. Видимые следствия оценены чтением сохранённых новых
сцен. Наличие факта одновременно в Lore и ещё несжатом RAW не доказывает, что
именно Lore было единственной причиной его воспроизведения.

Relationships: 11 jobs завершены, наблюдаются causes, в том числе помощь
Антона. Полная самостоятельная semantic acceptance осей не закрыта.
Administrator: 11 jobs завершены, два реальных local calls вернули
`no_proposal`; остальные jobs используют действующую cadence. Suggest →
accept → последующая сцена не проверена. Story Memory на v6/v5 штатно `not_due`;
эта проверка не подменяет compression boundary после 50 RAW и длинный gate.

## Проверки и границы готовности

Exact runtime source: `87ada44`; проверенный image
`sha256:d8a8c3b00b4710f7815537dbdecf4bc70283b56aa8ac5d19e0f50de202e1552e`
(Docker image ID, не build config digest). SHA256 файлов внутри него:
`provider.py=a62ee7a75d90570a37477e92d193292dc91e586b5f6b457f6f89323ff9cc7394`,
`mechanics.py=788f640bbe71c1a1d456a8f9aaac1d4a72d7e81a9f99791f368c3484e96753c2`,
`main.py=deda498e101b5f6e98eb0f5e0a3f910ba386d57e7c0a4a4abe5cd68116e2327c`.
Image full Gateway suite: **107 passed in 8.07s**. Local CI на этой runtime
revision: **107 passed in 23.01s**, отдельно прошли repository contracts,
installed skill drift, DevKit policy/MCP и clean Light GUI tests. Последующие
изменения evidence и regression assertions не изменяют runtime source.
Финальный local full CI с дополнительной проверкой invalid `no_target`:
**107 passed in 22.65s**, CI wall gate 23.1s / 60s; retained clean test allowlist
5000 / 5000 physical LOC. GitHub job измеряется отдельно.

Старый `run-rp-stack-evals.ps1 -Mode Offline` не выполнен: retained wrapper
ссылается на удалённый clean-cutover `evals/run_evals.py`. Этот устаревший
контур не восстанавливался ради зелёного маркера.

Достигнут уровень **наблюдается** для исправленной output boundary и описанных
коротких цепочек. Уровень **держится** и весь Decision 043 не заявляются.
На момент написания evidence это source/candidate, не новый production apply.

Сознательно не реализованы в этом исправлении:

- новая настройка Narrator или A/B: сохраняется принятый revision; в сценах
  остаются NPC coaching, недоказанное сходство лица с женой и нарушения
  пространственной непрерывности (автомобиль/шестой этаж в contrast v4);
- новый semantic deduplicator, массовая правка RAW, regex-фильтры смысла,
  повтор неизменённого отклонённого ответа или fallback;
- автоматическая генерация/accept предложений Administrator ради закрытия gate;
- новая модель, расширение token budgets, изменение Story Memory threshold,
  новое обслуживание очередей или интерфейс ожидания long player operations;
- человеческие 20 ходов и 65+ endurance: ранние блокирующие output defects
  сначала доведены до проверенного исправления; длительный проход остаётся
  следующей работой после доставки кандидата.

Следующий delivery gate: PR → успешные проверки → merge → интерактивный Ansible
apply владельцем → отдельная проверка applied SHA, image/source и runtime.
Ни merge, ни приведённые изолированные сцены не являются production proof.
