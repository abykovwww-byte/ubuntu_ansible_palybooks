# Decision 043: цены и маршруты моделей на 2026-09-02

Снимок снят 2026-09-02T16:39Z из публичного OpenRouter API без API key:

```text
curl -sS https://openrouter.ai/api/v1/models
curl -sS https://openrouter.ai/api/v1/models/:author/:slug/endpoints
```

`GET /api/v1/models` вернул HTTP `200`. Значения ниже — содержимое каталога на
момент снимка, а не гарантия будущей цены или доступности конкретного endpoint.
Все цены приведены в долларах США за миллион токенов.

## Каталог

| Exact model ID | Input | Output | Cache read | Context | `structured_outputs` | `reasoning` | NVIDIA endpoint |
| --- | ---: | ---: | ---: | ---: | --- | --- | --- |
| `openai/gpt-5.6-luna-pro` | 0.200000 | 1.200000 | 0.020000 | 1 050 000 | да | да | нет, 5 endpoints |
| `deepseek/deepseek-v4-flash` | 0.084000 | 0.168000 | 0.016800 | 1 048 576 | да | да | нет, 17 endpoints |
| `deepseek/deepseek-v4-flash-0731` | 0.065000 | 0.180000 | 0.016000 | 1 310 720 | да | да | нет, 31 endpoint |
| `deepseek/deepseek-v4-pro` | 1.030776 | 2.061552 | 0.085898 | 1 048 576 | да | да | нет, 18 endpoints |
| `openai/gpt-oss-120b` | 0.037000 | 0.170000 | — | 131 072 | да | да | нет, 21 endpoint |
| `qwen/qwen3.5-flash-02-23` | 0.065000 | 0.260000 | — | 1 000 000 | да | да | нет, 1 endpoint |

`—` означает, что `pricing.input_cache_read` отсутствует в ответе каталога. Это
не цена `$0` за cached token: отдельная скидка кэша не заявлена, поэтому при
оценке длинной партии cached input нельзя считать бесплатным.

`anthropic/claude-sonnet-4.6`, упомянутый ниже только как возможная
Administrator-эскалация, также проверен: input `$3.00/M`, output `$15.00/M`,
cache read `$0.30/M`, context `1 000 000`, оба параметра поддерживаются,
NVIDIA отсутствует среди 9 endpoints.

## Дисквалификации

Из отбора исключаются:

- все `*-latest`: это скользящие aliases того же класса риска, что
  `openrouter/auto`; на момент снимка каталог содержит 14 таких ID, включая
  `~deepseek/deepseek-v4-flash-latest`,
  `~anthropic/claude-sonnet-latest` и `openai/gpt-chat-latest`;
- все `:free`: на момент снимка каталог содержит 18 таких ID;
- `nvidia/*` и любая модель, чей endpoint-каталог содержит NVIDIA provider.

У шести основных кандидатов и у `anthropic/claude-sonnet-4.6` NVIDIA endpoint
на момент снимка не найден. Это только catalog gate. Функциональный срез всё
равно обязан отправлять явный `provider.order` и `allow_fallbacks:false`, а
provider-canary — сохранить фактический outbound payload. Текущее
`provider.ignore=["nvidia"]` само по себе не заменяет exact route proof.

## Влияние кэша и сравнение

Для одинакового входа `deepseek/deepseek-v4-flash-0731` дешевле недатированного
`deepseek/deepseek-v4-flash` по input на `22.6%`, но дороже по output на `7.1%`.
По принятой в Light GUI контрольной оценке `95k input / 650 output`:

| Модель | Cold | При 80% cache read |
| --- | ---: | ---: |
| `deepseek/deepseek-v4-flash` | `$0.008089` | `$0.002982` |
| `deepseek/deepseek-v4-flash-0731` | `$0.006292` | `$0.002568` |
| `qwen/qwen3.5-flash-02-23` | `$0.006344` | `$0.006344`, скидка не заявлена |
| `openai/gpt-oss-120b` | `$0.003626` | `$0.003626`, скидка не заявлена |

По этой явно определённой оценке датированный DeepSeek экономит `22.2%` cold и
`13.9%` warm относительно недатированного DeepSeek. Старые оценки `−24%` и
`≈−18% на задание` больше не воспроизводятся на ценах этого снимка и поэтому не
переносятся как факт.

Ключевое наблюдение: у Qwen из таблицы кэш-выгода равна нулю — каталог не
объявляет отдельный cache-read tariff. Поэтому на длинной партии со стабильным
префиксом он стоит примерно в `2.47` раза дороже warm
`deepseek/deepseek-v4-flash-0731` по контрольному профилю, несмотря на низкую
цену обычного input. Стабильный префикс окупается только на маршруте с реальной
cache-read скидкой и подтверждённой provider-метрикой.

## Предлагаемые привязки

Это предложение для provider-canary и следующего функционального PR. В этом
docs/evidence PR конфигурация не меняется.

- **Атомарная служебная роль:** проверить
  `deepseek/deepseek-v4-flash-0731` вместо недатированного
  `deepseek/deepseek-v4-flash`. Сравнительный кандидат —
  `openai/gpt-oss-120b`. Решение принимает canary Decision 042 по strict output,
  `reasoning_tokens=0`, latency и качеству четырёх Lore исходов, а не одна цена.
- **Administrator:** основной кандидат `deepseek/deepseek-v4-pro`; переход на
  `anthropic/claude-sonnet-4.6` рассматривается только если доля принятых
  proposals на первой приёмочной партии окажется низкой.
- **Narrator:** оставить `openai/gpt-5.6-luna-pro` до завершения исходного blind
  A/B. Смена модели одновременно с движком сделает сравнение нечитаемым.

Для любого принятого OpenRouter route обязательны exact model ID,
`provider.order`, `allow_fallbacks:false`, отсутствие NVIDIA в endpoint-каталоге
на дату canary и отсутствие наследования fallback/retry targets из environment.
