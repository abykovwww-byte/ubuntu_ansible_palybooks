# Decision 043: provider canary 2026-09-02

## Граница проверки

Canary выполнен 2 сентября 2026 года из применённого Gateway container через
его effective provider endpoints. Production Party, SQLite, configuration,
containers и `RP_REBUILD_ENABLED=false` не менялись. OpenRouter key не
выводился; read-only `GET /api/v1/key` подтвердил HTTP 200, paid tier и
положительный остаток.

Публичные endpoints сверялись через `/api/v1/models/{author}/{slug}/endpoints`.
OpenRouter требует provider **slug**, а не display name; `only` ограничивает
allowlist, `order` задаёт порядок, а `allow_fallbacks:false` запрещает уход к
другому provider. Это соответствует официальному
[provider routing contract](https://openrouter.ai/docs/guides/routing/provider-selection).

Каждый принятый OpenRouter payload содержал одновременно:

```json
{
  "provider": {
    "order": ["exact-endpoint-slug"],
    "only": ["exact-endpoint-slug"],
    "allow_fallbacks": false,
    "require_parameters": true
  }
}
```

Ни один canary не использовал `auto`, `free`, `latest`, NVIDIA, model fallback
или retry target из environment. Transport failure повторялся только там, где
исхода модели не существовало, и только после изменения route/budget либо как
один bounded retry после upstream `429`. Неизменённый rejected semantic output
не повторялся.

## Atomic typed Lore contract

Итоговая canary revision `typed-lore-v4-location-boundary` получает ровно один
полный committed turn и обязательный `requested_kind`. Значение определяет
модель; код не использует regex/substrings. JSON schema является плоским
discriminated `oneOf`:

- `draft`: `result=draft`, exact `kind`, непустые `title`, `content`,
  `keywords`;
- `no_candidate`: `result=no_candidate`, exact `kind`, остальные поля `null`.

Для `location` prompt прямо отделяет именованное посещаемое место от предмета,
погоды, света и общей обстановки. Это corrective semantic context после
зафиксированного false positive, а не программный truth predicate.

### OpenRouter-кандидаты

| Model / exact provider | Transport/schema | Semantic результат | Решение |
| --- | --- | --- | --- |
| `deepseek/deepseek-v4-flash-0731` / `deepinfra/fp8` | exact route, strict JSON, `reasoning_tokens=0`; один обычный location получил provider `403` | исходный prompt дал 3 false-negative из 3 positive; после corrective prompt два positive прошли до `403` | endpoint и исходный prompt отклонены |
| `deepseek/deepseek-v4-flash-0731` / `cloudflare` | первый benign request получил `403` до generation | не проверено | endpoint отклонён |
| `deepseek/deepseek-v4-flash-0731` / `open-inference/fp8` | exact route; один upstream `429` успешно повторён тем же request hash | v4 дал три shaped positive, но event добавил неподтверждённого субъекта, а negative превратил предметы в location | model/route отклонён для atomic роли |
| `deepseek/deepseek-v4-flash` / `streamlake/fp8` | 4/4 HTTP 200, exact route, strict JSON, `reasoning_tokens=0` | 3 positive grounded; negative снова превращён в выдуманную комнату | model/route отклонён |
| `openai/gpt-oss-120b` / `deepinfra/bf16` | HTTP 400 до generation: reasoning обязателен и не может быть выключен | не проверено | несовместим с atomic `reasoning=off` |
| `qwen/qwen3.5-flash-02-23` / `alibaba` | 4/4 HTTP 200, exact route, `reasoning_tokens=0`, но provider не обеспечил required discriminator | во всех четырёх ответах отсутствовал `result` | strict contract не пройден |

Первый diagnostic payload с display name `DeepSeek` был отклонён до model call.
Исправленный slug `deepseek` также fail-closed: официальный endpoint не
объявляет `structured_outputs`, и `require_parameters:true` оставил ноль
маршрутов. Эти ошибки не считаются model canary и не маскируются fallback.

Сумма `usage.cost` всех состоявшихся OpenRouter atomic calls составила примерно
`$0.0007493`. Failed pre-generation routes не включены.

### Принятый atomic route

Effective local model `gemma-4-26b-a4b-it-rp-q4` вызван напрямую через
`rp-local-llm:8080`, без API key, model/provider fallback и OpenRouter. Все
четыре результата прошли schema, shape и ручную проверку фактов:

| Case | Request SHA-256 | Response ID | Latency | Outcome |
| --- | --- | --- | ---: | --- |
| `character` | `56c0d64dcfec4f4436fe1fc2e95ef1a1d88d2f53ccd1f4939b69e92e32a5c640` | `chatcmpl-d5su9DAhCCpMPqxKsTwGlDejitcxQBuW` | 5 018 ms | grounded `draft` |
| `event` | `4362094b966523a24a0c3aa396488d1f239fce4e8d00370e59a5f04239069fa1` | `chatcmpl-qaQUs2isRggKfB0d1iP4aFJ3xidZQrKm` | 5 115 ms | grounded `draft` |
| `location` | `7d960ef30b38072b0eeb35ceea568c19034c1b546a359363d986b51ce9378fac` | `chatcmpl-sorh0yVSbVqPtdh1rYuILPnA7HqFnNDN` | 4 432 ms | grounded `draft` |
| `no_candidate` | `f455af5a2b3ce7a4b7c19b63894fe6fefb63e506fdba81225708f72b62d05fd1` | `chatcmpl-gzRP7lMWFMq46idUuDu3l8FgSyPaAlwh` | 1 865 ms | exact `no_candidate`, three `null` fields |

Примеры результата: character сохранил имя и должность Елены Рудневой; event
сохранил только эвакуацию, время и причину из RAW; location дословно сохранил
станцию, площадь и вход; negative не превратил лампу, стол, окно или дождь в
место. Медианная latency четырёх последовательных calls — `4 725 ms`.

**Выбор для функционального среза:** новые atomic typed Lore и
`PlayerCorrection` используют существующую local service role с exact model
alias `gemma-4-26b-a4b-it-rp-q4`. Cloud atomic fallback не добавляется.

## Administrator candidate

`deepseek/deepseek-v4-pro` проверен на фактическом suggest-mode контракте через
exact `alibaba/fp8`, `only/order`, `allow_fallbacks:false` и strict
discriminated schema:

| Case | Budget | Request SHA-256 | Result | Usage / latency |
| --- | ---: | --- | --- | --- |
| нормальное окно | 512 | `a941b8e53be4e224714da960aa1274145ac77ad39dd80084f70930cf6b0fd94d` | exact `no_proposal` | 443 reasoning tokens; `$0.001905936`; 9 626 ms |
| повторная потеря agency | 512 | `df1e0859ec98463b325c22af67566d57394d4b3ebfe1664765a2d527df1d11f0` | `finish_reason=length`, JSON отсутствует | 512 reasoning tokens; `$0.002189136`; 8 926 ms |
| та же потеря agency после исправления budget | 2 048 | `8a7a8e6405471fd7f6c7c56c6188ef769a1f1fc04e18f7535211168210ee2b13` | grounded `suggest` только для `narrator_guidance` | 1 393 reasoning tokens; `$0.004924848`; 21 326 ms |

Budget `2 048` совпадает с существующим Administrator handler. Модель проходит
контракт, но дороже и медленнее local Gemma. Она остаётся сравнительным
кандидатом; активная Administrator binding не меняется до human acceptance и
измерения доли полезных accepted proposals. Claude escalation не запускалась:
нет доказанной низкой acceptance rate.

Суммарная reported стоимость всех состоявшихся paid calls этого evidence —
около `$0.0097692`.

## Итог и незакрытые границы

Provider pre-flight достаточен для начала disabled функционального среза:

- atomic route выбран и прошёл четыре typed Lore исхода;
- OpenRouter alternatives отклонены по сохранённым transport/schema/semantic
  причинам, а не только по цене;
- Administrator V4 Pro доказан как exact-route candidate на реальном бюджете,
  но не активирован;
- Narrator `openai/gpt-5.6-luna-pro` намеренно не менялся и проверяется на blind
  A/B после механического candidate image.

Это direct-provider evidence, не clean API/runner/storage/UI proof. Оно не
доказывает `PlayerCorrection`, Scenario Lore, `authoring_kind`, immutable
decision apply, Party ownership, restart recovery, image parity, activation или
live gameplay. Эти свойства принадлежат следующим шагам Plan 029.
