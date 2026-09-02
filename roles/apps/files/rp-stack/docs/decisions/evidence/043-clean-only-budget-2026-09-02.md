# Decision 043: clean-only budget 2026-09-02

## Исход

Clean-only source-кандидат закрывает оба механических gate шага 4:

- production allowlist Gateway-проверок: **4 966 / 5 000** физических LOC,
  debt **0**;
- полный Gateway suite: **97 passed** за **6.40s** локально и
  **97 passed** за **5.07s** на GitHub runner; оба замера меньше
  лимита 60s;
- candidate содержит один исполняемый движок и один World
  `day-watch-moscow-v2`;
- xdist, новые dependencies и отдельный test service не добавлены.

Замер привязан к source commit
`0bc9177d8e79c7b188d4ce56c5bc09d37734153c` и базе
`d3d7d6e1933c3596b49ba7bc2df3b866bffb402e` в PR #131. Дата проверки:
2 сентября 2026 года.

## Точный LOC allowlist

`scripts/ci.ps1` считает весь текст ровно восьми сохранённых
Gateway test files, без focused-фильтра:

| Файл | Физические LOC |
| --- | ---: |
| `test_rp_gateway_integration.py` | 697 |
| `test_rp_gateway_lifecycle.py` | 173 |
| `test_rp_mechanics.py` | 1 053 |
| `test_rp_narrator_memory.py` | 968 |
| `test_rp_provider.py` | 748 |
| `test_rp_runner.py` | 629 |
| `test_rp_turn_engine.py` | 470 |
| `test_rp_world_scenario.py` | 228 |
| **Всего** | **4 966** |

Из allowlist исключены только иные типы артефактов и отдельные
глобальные gate:

- 12 `evals/rebuild/anchors/*.json` — immutable data/evidence, а не
  исполняемый verification code;
- application/runtime code, docs и авторский World — production content, а не
  скрытая часть Gateway suite;
- clean Light GUI contract, repository/devkit и Ansible — самостоятельные
  глобальные CI gate, но не Gateway LOC;
- generated/vendor content в candidate не добавлялся.

## Время и глобальный CI

Локальный `scripts/ci.ps1` на source commit дал `97 passed in 6.40s`;
отдельный wall-clock замер полного suite — **6.8s / 60s**. Тот же
полный suite в GitHub Actions run `33675434087`, job `100398845234`,
дал `97 passed in 5.07s`, shell `real 0m6.200s`; весь Gateway job занял
21s. Runner: Ubuntu 24.04, hosted image version `20260823.283.1`.

Остальные checks того же run:

| Gate | Исход | Время job |
| --- | --- | ---: |
| Browser clients | pass | 6s |
| Repository contracts | pass | 20s |
| Gateway | pass | 21s |
| Ansible syntax | pass | 32s |

Единственный warning в Gateway job — deprecation warning на границе
Starlette/httpx. Он не скрыл падений и не требует новой dependency в
этом срезе.

## Проверенные runner invariants

Full suite закрепляет четыре обязательных исхода:

1. `pending → running` выполняется атомарным `UPDATE` с предикатом
   исходного статуса в том же `UPDATE`;
2. `attempts` растёт только в `fail_*` после фактического отказа,
   но не при claim или release;
3. runner владеет startup, stop, cancel и await, а при cancellation
   освобождает незавершённую job;
4. atomic и Administrator имеют разные handlers, jobs и routes;
   Narrator остаётся синхронным turn path.

## Clean-only граница

Candidate удаляет из исполняемого контура contract revisions,
compatibility branches, legacy state/memory/supervisor/check/eval код и старые
WorldPacks. Narrator зафиксирован на exact OpenRouter route
`openai/gpt-5.6-luna-pro` с provider `openai` и `allow_fallbacks:false`.
Atomic и Administrator зафиксированы на отдельные local role paths с exact
model `gemma-4-26b-a4b-it-rp-q4`. Активных fallback/retry маршрутов,
включая NVIDIA и environment inheritance, нет.

Принятые Party HTTP/Light GUI, auth/security, BYOK и persistence контракты
сохранены. Legacy SQLite, `/srv/app-data`, state и backups не меняются
и не удаляются. IaC перед copy удаляет только управляемые
source-каталоги, если найдены маркеры retired source; это не data migration.

## Delivery state на момент фиксации

| Граница | Статус |
| --- | --- |
| Source | candidate commit `0bc9177d8e79c7b188d4ce56c5bc09d37734153c` |
| Push / PR CI | PR #131, все 4 checks green |
| Merge | ожидается после docs-only фиксации и повторного CI |
| Apply / activation / live verification | **не выполнялись** |

Сознательно не сделано в этом шаге: server apply; новая production
SQLite; provider и human A/B; live Party; удаление или миграция production data;
финальное зеркало RP Stack Wiki. Exact-image механическая приёмка
принадлежит шагу 5, human acceptance — шагу 6, production cutover —
шагу 7, а финальные evidence/Wiki/прополка — шагу 8.
