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

На source-кандидате пройдены focused provider/lifecycle tests `26 passed`, полный
Gateway suite `100 passed` и aggregate `scripts/ci.ps1`, включая repository,
Wiki, devkit, JavaScript и Gateway gates. Merge, acceptance image, live job rows
и production apply фиксируются отдельно после выполнения; production inventory
Decision 043 остаётся выключенным.

Источники provider snapshot:

- <https://openrouter.ai/deepseek/deepseek-v4-pro>
- <https://openrouter.ai/api/v1/models/deepseek/deepseek-v4-pro/endpoints>
