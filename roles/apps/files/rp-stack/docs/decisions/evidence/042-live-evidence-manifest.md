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

## Партии, которые нельзя удалять до замены доказательства

| Party | Revision | Роль в доказательстве |
| --- | --- | --- |
| `party_30fd9d3cc6ef` | 10 | Отказы GM-коррекции: `525` (обрыв по бюджету), `527` (выдуманное правило), `529` (в `before` попала внеигровая реплика игрока); первый scene-recap draft `522` |
| `party_3e09b9092765` | 11 | Lore draft `533`: весь output-бюджет ушёл в reasoning, content пустой |
| `party_cac70558b50a` | 11 | **Полная цепочка дефекта**: draft `566` → сохранённая карточка `286` → её попадание в prompt десяти подряд ходов |
| `party_16c210a8a099` | 0 | Цель `probe_command` в `registry/020.yml` и `registry/021.yml`; уровни `наблюдается` перестанут быть воспроизводимыми после удаления |

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
| Попадание в prompt | `turns.metadata_json.lore_card_ids` содержит `286` в ходах 1867–1878 — двенадцать подряд на момент повторной сверки 2026-08-27T13:0xZ; партия продолжается |

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

Это единственный в базе экземпляр player-created карточки, прошедшей весь путь до
narrator prompt: всего в `lore_cards` две записи с `authored_key IS NULL`, вторая
(`id=1`) относится к партии 2026-08-05 и в prompt не входит.

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
