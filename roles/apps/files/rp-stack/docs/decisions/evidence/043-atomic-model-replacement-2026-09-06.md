# Decision 043: замена Atomic Service model, 2026-09-06

## Решение

Atomic Service clean RP закрепляется за:

- model: `deepseek/deepseek-v4-pro`;
- OpenRouter endpoint tag: `baidu/fp8`;
- `provider.order` и `provider.only`: только `baidu/fp8`;
- `allow_fallbacks=false`, `require_parameters=true`, reasoning off;
- credentials: только server-managed `SERVICE_OPENROUTER_API_KEY`; inventory
  по умолчанию использует существующий server OpenRouter key, а local override
  может задать отдельный service key.

Narrator остаётся на `openai/gpt-5.6-luna-pro` → `openai`, Administrator — на
local `gemma-4-26b-a4b-it-rp-q4`. Публичный Party API и job/storage schemas не
меняются.

## Почему прежний выбор снят

Первичный short canary 2 сентября проверял только четыре коротких typed Lore
ответа и дал Gemma `4/4`. Длинная human acceptance Party
`party_f9490fa8937d` показала production-like предел этого решения:

| Контур к version 60 | Результат |
|---|---:|
| Relationships jobs | `60 succeeded` |
| Runtime Lore jobs | `60 failed` |
| Story Memory jobs | `39 succeeded`, `21 failed` |
| Последний Story Memory snapshot | revision `4`, safe coverage `32` |
| Story Memory provider calls | `4 completed`, `64 timeout/error` |
| Средняя длительность completed Story Memory | `130.7 s` |
| Timeout Story Memory | около `150 s` |

Все 21 failed Story Memory jobs версий `40..60` исчерпали три попытки. Таким
образом, короткая проверка формата не доказала пропускную способность длинной
очереди, а Gemma перестала быть приемлемой Atomic model.

## Выбор модели

Актуальные model/endpoint сведения получены из публичного OpenRouter catalog и
Endpoints API. Для Baidu route на момент проверки объявлялись strict
`response_format`, reasoning control, около `$0.68614/M` input и `$1.37228/M`
output tokens. Endpoint не относится к NVIDIA.

Более дешёвые `openai/gpt-4.1-nano`, `openai/gpt-4.1-mini` и
`google/gemini-2.5-flash-lite` были отклонены: хотя отдельный Relationships
ответ проходил, Runtime Lore и/или Story Memory не принимали текущую production
JSON Schema либо возвращали невалидный strict result. Менять доменные schemas
ради дешёвой модели не стали. `openai/gpt-5.4-mini` на exact OpenAI route этой
учётной записи вернул `404` до генерации.

## Синтетический provider canary

Canary выполнялся из acceptance Gateway container с искусственными именами и
фактами. Он не читал Party SQLite, сохранённые prompts или ответы пользователя.
Валидация использовала неизменённые Pydantic schemas production-кода.

| Operation | HTTP | Strict result | Latency | Tokens input/output | Cost |
|---|---:|---:|---:|---:|---:|
| Relationships | `200` | pass | `2.608 s` | `631 / 7` | `$0.0004425603` |
| Runtime Lore | `200` | pass | `1.634 s` | `757 / 38` | `$0.00057155462` |
| Story Memory | `200` | pass | `3.511 s` | `2404 / 213` | `$0.0019417762` |

Итого: `3/3` strict schemas, около `7.75 s` и `$0.00295589112` за три вызова.
Эти числа сравнивают route compatibility на малом payload; они не доказывают
стоимость или latency 60-ходовой Party.

Если механически переоценить token usage только завершённых Gemma calls
60-ходовой Party по тарифу выбранного Baidu endpoint, получится около `$0.577`:
`$0.183` Relationships, `$0.359` Runtime Lore и `$0.034` Story Memory. Это не
счёт и не прогноз: timeout calls без usage не вошли, а валидный короткий
`no_candidate` новой модели должен существенно уменьшить Lore output.

## Изменяемый контракт

- Atomic Service больше не зависит от local runner и fail-closed требует
  server OpenRouter key при включённой роли.
- Все пять atomic operations используют один exact model/provider route и
  прежние strict schemas, temperature `0`, reasoning off и token budgets.
- Provider error и semantic rejection сохраняют прежнюю retry/terminal policy.
- Administrator по-прежнему требует local runner; очереди и credentials ролей
  не смешиваются.
- Atomic payload теперь пересекает внешний trust boundary OpenRouter/Baidu;
  Party BYOK и browser credentials в него не передаются.

## Границы доказательства

Первичный source-кандидат прошёл focused provider/lifecycle tests `26 passed`,
полный Gateway suite `100 passed` и aggregate `scripts/ci.ps1`. После замены
модели отдельная 60-ходовая Party `party_969f9fa93918` дошла до version `60`
без Narrator fallback, но выявила новый ограниченный дефект Story Memory. На
source version `58` real Atomic call вернул HTTP `200`, `720` output tokens и
2 975 символов при жёстком лимите L1 в 2 000 символов. Gateway корректно
отклонил результат, snapshot не записал, а следующие memory jobs не перескочили
через failed predecessor. Этот прогон доказал throughput выбранного route, но не
приемлемый bounded output.

Первый fix-forward, PR #139 / merge `fdeffc2b7d79a53bbd439a72c4cb860e2609d2b4`,
снизил L1 budget до `384` tokens. Replay тех же RAW 1–8 показал другую границу:
1 131 символов content, ровно `384` completion tokens и
`finish_reason=length`. Неполный ответ снова был отвергнут без snapshot. Поэтому
этот merge не считался runtime-closure.

Финальный минимальный fix-forward, PR #140 / merge
`7d92dc475756e7ab96d8dee077900df47dda7d3e`, сохранил L1 validator `≤2 000`
символов, поднял generation budget до `640` tokens и потребовал закончить
связный текст не более чем в восьми предложениях до исчерпания token budget.
Archive сохраняет validator `≤6 000`, budget `1 536` tokens и предел в 24
предложения. Обрезание готового текста, новый retry, сервис, dependency или
storage schema не добавлялись.

11 сентября merge `7d92dc4` применён штатным `ansible-local-apply.service`:
`ok=74`, `changed=6`, `unreachable=0`, `failed=0`. Server checkout и running
Gateway содержат один и тот же `provider.py` SHA-256
`79978a249e7ba18082125a68d941e0cb1c7b503539a60813efc622869bf29e9d`.
Gateway image `sha256:393e39d5ba77b8b5dc7ce13e49cffb12d69e8e31684f7e381e900a85f392c3ab`
healthy с restart count `0`; `:8010` вернул HTTP `200`. Обе production SQLite
дали `integrity_check=ok` и ноль foreign-key violations. Full suite того же
image с очищенным только внутри test-container service key: `103 passed` за
`7.88s`. Первый запуск с production env дал `102 passed, 1 failed`, потому что
негативный startup-test намеренно ожидает отсутствие service key; это влияние
окружения теста, а не результат, использованный как green proof.

Отдельный canary того же production image использовал только синтетические RAW
и packaged WorldPack, отдельные SQLite и не монтировал production data. Реальный
`deepseek/deepseek-v4-pro` → `baidu/fp8` вызов сжал 25 030 символов RAW 1–8 в
621 символ narrative за `6.279s`: HTTP `200`, `8 405 / 195` tokens,
`finish_reason=stop`, стоимость `$0.0084127693`. Job завершился с `attempts=0`,
snapshot revision `1` получил coverage `8`, а следующий локально собранный
Narrator prompt содержал exact snapshot и ровно RAW 9–58. Обе canary SQLite
остались `integrity_check=ok` без foreign-key violations.

Для bounded Story Memory это уровень `наблюдается` в изолированной Party на
фактически применённом production image: модельный результат сохранён в
авторитетном store и попал в следующий prompt. Уровень `держится` не заявляется:
не проверены второй live chunk, hierarchy после 130 000 символов и повторное
влияние памяти на сцены длинной человеческой Party. 60-ходовой прогон также не
закрывает human gates §6.2–§6.3 Decision 043.

Источники provider snapshot:

- <https://openrouter.ai/deepseek/deepseek-v4-pro>
- <https://openrouter.ai/api/v1/models/deepseek/deepseek-v4-pro/endpoints>
