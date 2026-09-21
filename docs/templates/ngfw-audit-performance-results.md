# Результаты кампании NGFW / AuditD — ШАБЛОН, НЕ РЕЗУЛЬТАТЫ

Протокол: [NGFW-AUDIT-PERF-v1](../ngfw-audit-performance-plan.md).
Скопировать в приватный каталог кампании. Заполнить до запуска разделы 1–3;
после каждого прогона — 4–6; после сравнения — 7. Не перезаписывать шаблон
реальными raw-данными и не публиковать секреты. Пустое поле не равно нулю.

## 1. Паспорт и предварительная фиксация критериев

| Поле | Значение |
| --- | --- |
| campaign_id | NOT_SET |
| Дата/время UTC начала и окончания | NOT_SET |
| Оператор и согласованный диапазон действий | NOT_SET |
| Версия протокола / Git commit | NOT_SET |
| Версии NGFW, ОС, kernel, auditd, runner; digest образов | NOT_COLLECTED |
| vCPU/RAM VM; лимиты генератора и коллектора | NOT_COLLECTED |
| SHA256 эффективной политики NGFW; ACL/NAT, inspection/session logging | NOT_COLLECTED |
| Топология и контроль пути через NGFW | NOT_COLLECTED |
| Источник B: исходный проблемный набор / новый лабораторный кандидат | NOT_SET |
| Критерии H1/H2 приняты до измерений; дата/ответственный | NOT_SET |
| Первичная метрика и порог H2 по выбранной версии протокола | NOT_SET |
| Точный перечень workload_id и ступеней; SHA256 runner configs | NOT_SET |
| Термодатчик, порог старта, остановки, условия восстановления | NOT_SET |
| Как наблюдаем температуру/частоты/throttling; оператор остановки | NOT_SET |
| Фоновые службы/EDR/пересылка; ограничения сравнения | NOT_COLLECTED |
| Хранилище raw-артефактов и срок хранения | NOT_SET |

## 2. T00 — допуск

- [ ] Проверен безопасный откат и идентифицирован нужный snapshot.
- [ ] Сохранены исходные настройки/загруженные правила и их хэши.
- [ ] Изоляция проверена при работающих конечных точках и выключенной NGFW VM;
  свидетельство моложе 24 ч и соответствует текущей топологии.
- [ ] После включения исправны NGFW/MNGT, dataplane и приёмник логов.
- [ ] Probe работает без интерактивных запросов во время измерений; audit enabled,
  auditd active, lost=0; обязательные метрики получены. Секретов в отчёте нет.
- [ ] Известны stop-условия фактического runner.json; проверен механизм остановки.
- [ ] Температурные поля заполнены, охлаждение/фон стабильны; место для логов есть.
- [ ] UTC синхронизирован, boot ID и точность времени зафиксированы.
- [ ] Подготовлены сбор состава событий и измерения, которых нет в runner.
- [ ] Выбраны безопасные действия T11, ожидаемые события и порядок отката.

Решение допуска: **BLOCKED**. Причина / кто и когда разрешил переход: NOT_SET.
Ссылки и SHA256 свидетельств T00: NOT_COLLECTED.

## 3. Эффективные профили

Ссылки на приватные read-back артефакты, не текст секретных конфигов.

| profile_id | Группы / точная разница | effective_rules_sha256 | auditd_conf_sha256 | Kernel controls / boot ID | Проверен |
| --- | --- | --- | --- | --- | --- |
| A | NOT_SET | NOT_COLLECTED | NOT_COLLECTED | NOT_COLLECTED | нет |
| A-PKT | NOT_SET | NOT_COLLECTED | NOT_COLLECTED | NOT_COLLECTED | нет |
| A-SYS | NOT_SET | NOT_COLLECTED | NOT_COLLECTED | NOT_COLLECTED | нет |
| A-EXEC | NOT_SET | NOT_COLLECTED | NOT_COLLECTED | NOT_COLLECTED | нет |
| A-FILE | NOT_SET | NOT_COLLECTED | NOT_COLLECTED | NOT_COLLECTED | нет |
| B | NOT_SET | NOT_COLLECTED | NOT_COLLECTED | NOT_COLLECTED | нет |
| C-PKT | NOT_SET | NOT_COLLECTED | NOT_COLLECTED | NOT_COLLECTED | нет |
| C-SYS | NOT_SET | NOT_COLLECTED | NOT_COLLECTED | NOT_COLLECTED | нет |
| C-NET | NOT_SET | NOT_COLLECTED | NOT_COLLECTED | NOT_COLLECTED | нет |

## 4. Реестр тестов и реальных запусков

Начальные строки — план, не один выполненный запуск. Добавлять отдельную строку
для каждого run_id (включая контроли, повторы и STOPPED). comparison_id связывает
сопоставимые A/B/C одного workload/ступени и повторения. T00/T02/T11 могут иметь
ручной evidence ID вместо каталога runner; способ сбора должен быть указан.

| test_id | Тезис / роль | comparison_id | run_id / evidence ID | profile_id | workload_id / ступень / повтор | Статус | Артефакты |
| --- | --- | --- | --- | --- | --- | --- | --- |
| T00 | допуск H1/H2 | — | — | — | — | NOT_RUN | — |
| T01 | контроль H1/H2 | — | — | A | — | NOT_RUN | — |
| T02 | применимость H1/H2 | — | — | — | — | NOT_RUN | — |
| T03 | H1: N-PKT | — | — | A / A-PKT | — | NOT_RUN | — |
| T04 | H1: N-SYS | — | — | A / A-SYS | — | NOT_RUN | — |
| T05 | H1: E-EXEC | — | — | A / A-EXEC | — | NOT_RUN | — |
| T06 | H1: F-FILE | — | — | A / A-FILE | — | NOT_RUN | — |
| T07 | H1, контроль H2 | — | — | A / B | — | NOT_RUN | — |
| T08 | H2: N-PKT | — | — | A / B / C-PKT | — | NOT_RUN | — |
| T09 | H2: N-SYS | — | — | A / B / C-SYS | — | NOT_RUN | — |
| T10 | H2: N-PKT + N-SYS | — | — | A / B / C-NET | — | NOT_RUN | — |
| T11 | H2: полезный аудит | — | — | A / B / C-NET | — | NOT_RUN | — |
| T12 | длительная устойчивость | — | — | A / C-NET | — | NOT_RUN | — |

### Карточка одного запуска — повторять для каждого run_id

- test_id / hypothesis_id / comparison_id / run_id: NOT_SET.
- profile_id / workload_id / ступень / повтор / порядок: NOT_SET.
- Начало, конец UTC; measurement отдельно от warmup/idle: NOT_COLLECTED.
- Предложенная/достигнутая нагрузка и единицы: NOT_COLLECTED.
- Полезная скорость, CPS, ошибки, UDP loss, p95/p99 и метод оценки: NOT_COLLECTED.
- CPU гостя/хоста; CPU генератора; частота/температура/throttling: NOT_COLLECTED.
- CPU auditd/PT/отправителя, если измерены; иначе явно пробел: NOT_COLLECTED.
- Lost до/после, backlog max/p95 и лимит, audit enabled/service: NOT_COLLECTED.
- Audit records/s, events/s, bytes/s по группам; ротации/пробелы: NOT_COLLECTED.
- Диск/память/доставка/management/dataplane: NOT_COLLECTED.
- Snapshot профиля/read-back и SHA256; изменения фона: NOT_COLLECTED.
- Статус выполнения; время и причина остановки: NOT_RUN.
- Валидность сравнения и альтернативные причины: NOT_EVALUATED.
- Возврат A / восстановление доступа / подтверждение оператора: NOT_COLLECTED.

| Артефакт | Приватный путь / безопасная ссылка | SHA256 | Пробелы |
| --- | --- | --- | --- |
| summary.json | NOT_COLLECTED | NOT_COLLECTED | — |
| metrics.ndjson | NOT_COLLECTED | NOT_COLLECTED | — |
| results.csv / workload outputs / report.md | NOT_COLLECTED | NOT_COLLECTED | перечислить каждый файл отдельно |
| isolation / effective config / дополнительные метрики | NOT_COLLECTED | NOT_COLLECTED | перечислить каждый файл отдельно |

## 5. Парные сравнения и расчёт H2

Одна строка на сравнение и повтор. A — среднее сопоставимых контрольных окон
до/после; при дрейфе INVALID. Формула и критерии значимости — в протоколе,
не менять их в этой копии по результатам. Не объединять разные workload/ступени.

| comparison_id | test_id | workload / ступень / повтор | run_id A до/после, B, C | CPU A/B/C | spread_A; B−A выше шума? | Разница фактической нагрузки ≤5%? | Снято затрат, % | Валидность / причина |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| NOT_SET | NOT_SET | NOT_SET | NOT_COLLECTED | NOT_COLLECTED | NOT_EVALUATED | NOT_EVALUATED | NOT_CALCULATED | NOT_EVALUATED |

Все значения повторов / медиана / диапазон: NOT_CALCULATED.
Изменение throughput, задержек, потерь, EPS и байт — отдельные показатели: NOT_COLLECTED.
Взаимодействие N-PKT/N-SYS и остаточная нагрузка: NOT_EVALUATED.

## 6. T11 — сохранность полезного аудита

До начала выбрать безопасные действия с откатом. Не менять пользователей,
sudoers, часы, политики или службу NGFW без отдельного согласования.
Одна синтетическая USER-запись доказывает канал, но не покрытие нужных правил.

| action_id | Согласованное действие / откат | Ожидаемые key/type | profile_id / run_id | Локальный audit ID | Получение на коллекторе / время | Итог / пробел |
| --- | --- | --- | --- | --- | --- | --- |
| NOT_SET | NOT_SET | NOT_SET | NOT_SET | NOT_COLLECTED | NOT_COLLECTED | NOT_RUN |

## 7. Выводы по тезисам

| Тезис / подвывод | Вердикт | test_id / comparison_id / run_id | Числа и ссылки | Границы / альтернативные причины |
| --- | --- | --- | --- | --- |
| H1: N-PKT | NOT_TESTED | — | — | — |
| H1: N-SYS | NOT_TESTED | — | — | — |
| H1: E-EXEC | NOT_TESTED | — | — | — |
| H1: F-FILE | NOT_TESTED | — | — | — |
| H1: комбинация B | NOT_TESTED | — | — | — |
| H1: общий вывод | NOT_TESTED | — | — | — |
| H2: только N-PKT | NOT_TESTED | — | — | — |
| H2: только N-SYS | NOT_TESTED | — | — | — |
| H2: оба исключения + сохранность T11 | NOT_TESTED | — | — | — |
| H2: общий вывод | NOT_TESTED | — | — | — |
| Длительность T12 | NOT_TESTED | — | — | — |

Формулировка H1: «В конфигурации … группа … при … вызвала/не вызвала …;
сравнения …; восстановление A …; предел применимости …». До измерений: NOT_TESTED.

Формулировка H2: «Исключение … при … сняло …% добавленных затрат CPU
(все повторы …); полезная скорость/задержки …; T11 …; длительность …;
критерий выполнен/не выполнен/данных недостаточно потому что …».
До измерений: NOT_TESTED.

Практическое решение: какие правила оставить/сузить/исключить; потерянное покрытие
и остаточный риск; какая следующая проверка нужна: NOT_DECIDED.
Проверка перед публикацией: raw/секреты удалены, ссылки безопасны, отрицательные
и остановленные прогоны сохранены, выводы проверены вторым чтением: NOT_DONE.
