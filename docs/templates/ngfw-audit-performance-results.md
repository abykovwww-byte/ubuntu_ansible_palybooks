# Результаты кампании NGFW / AuditD — ШАБЛОН, НЕ РЕЗУЛЬТАТЫ

Протокол: [NGFW-AUDIT-PERF-v2](../ngfw-audit-performance-plan.md).
Скопировать в приватный каталог кампании. Заполнить до запуска разделы 1–3;
после каждого прогона — 4–6; после сравнения — 7. Не перезаписывать шаблон
реальными raw-данными и не публиковать секреты. Пустое поле не равно нулю.
Источники S01–S14: [реестр](../ngfw-audit-performance-sources.md).
T18/T20 выполняются внутри допуска T00, а не после основной нагрузки.

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
| Выбранные H3–H9/T13–T20; отдельные разрешения и неприменимые тесты | NOT_SET |
| Архитектура доставки: плагин / читатель файла, транспорт, очереди, точки фильтра | NOT_COLLECTED |
| CPU per-core/IRQ/steal, guest/host I/O, wait counter: сбор, версии, разрешение времени | NOT_SET |
| Debug/capture/console/offload и способ read-back; ограничения видимости | NOT_COLLECTED |
| Ранние стопы: single-core CPU/время, disk/service latency, очередь bytes/age, запас места, drain deadline | NOT_SET |
| Источник метрик до/после фильтра коллектора; влияние общего host на VM | NOT_SET |

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
- [ ] T18: проверены kernel failure и disk/space/overflow actions; опасных или
  неизвестных критических действий нет либо запуск BLOCKED до отдельного решения.
- [ ] T20: тяжёлый debug/capture выключен; offload и наблюдение фиксированы,
  обязательные probe/guard не отключаются для сравнения.
- [ ] Для выбранных механизмов собраны дополнительные метрики и заполнены ранние
  стопы; указан оператор/внешний монитор, которых пока не заменяет runner.
- [ ] Для T15-F/T16/T17/T19 есть отдельный допуск, точный diff/rollback;
  T16 не затрагивает общий коллектор, обязательный канал или host firewall.

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

Производные профили P-RULE-S/C, P-IO-X/Y, P-TX/FAULT, B-SINK, A-NATIVE-X/Y
добавлять только для выбранных тестов; P и его исходный run фиксируются явно.
Имя производного профиля не доказывает, что отличается только один параметр.

| test_id | Профили / исходный P | Единственный diff / SHA256 read-back | Неизменные условия / проверка эквивалентности | Допуск / откат |
| --- | --- | --- | --- | --- |
| NOT_SET | NOT_SET | NOT_COLLECTED | NOT_EVALUATED | BLOCKED |

### Первичные критерии H3–H9 — заполнять до подтверждающего опыта

| hypothesis / test | Scope RUNTIME / CONFIG_ONLY | Первичная метрика / единицы / направление | E_min / spread_control / критерий | Дата фиксации / разрешение |
| --- | --- | --- | --- | --- |
| H3 / T13 | RUNTIME | NOT_SET | NOT_SET | NOT_SET |
| H4 / T14 | RUNTIME | NOT_SET | NOT_SET | NOT_SET |
| H5 / T15-R | RUNTIME | NOT_SET | NOT_SET | NOT_SET |
| H5 / T15-F | RUNTIME | NOT_SET | NOT_SET | NOT_SET |
| H6 / T16 | RUNTIME | NOT_SET | NOT_SET | NOT_SET |
| H7 / T17 | RUNTIME | NOT_SET | NOT_SET | NOT_SET |
| H8 / T18 | CONFIG_ONLY | условие → эффективное действие | read-back + документация версии | NOT_SET |
| H9 / T19 | RUNTIME | NOT_SET | NOT_SET | NOT_SET |

Критерии ожидания/атрибуции H4, ротации H5 и отказа H6/H9 берутся из протокола;
E_min нужен для сравнения затрат и не заменяет условия причинной атрибуции.
До разрешения fault injection T16 — BLOCKED, даже если read-only часть выполнена.

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
| T13 | H3: проверка правил | — | — | P-RULE-S / P-RULE-C | — | NOT_RUN | — |
| T14 | H4: ожидание audit | — | — | ссылка на T03–T10 | — | NOT_RUN | — |
| T15-R | H5: естественная ротация | — | — | P | — | NOT_RUN | — |
| T15-F | H5: режим записи | — | — | P-IO-X / P-IO-Y | — | NOT_RUN | — |
| T16 | H6: доставка / обратное давление | — | — | P-TX / P-TX-FAULT | — | BLOCKED | нужен отдельный допуск |
| T17 | H7: источник / коллектор | — | — | A / B / B-SINK / C-NET | — | NOT_RUN | — |
| T18 | H8: CONFIG_ONLY, до T01 | — | — | effective config | — | NOT_RUN | — |
| T19 | H9: обычные логи NGFW | — | — | A-NATIVE-X / A-NATIVE-Y | — | NOT_RUN | — |
| T20 | контроль наблюдения, до T01 | — | — | observer config | — | NOT_RUN | — |

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
- Scope RUNTIME/CONFIG_ONLY; Sxx и применимость к установленной версии: NOT_SET.
- CPU по ядрам user/system/IRQ/softirq/iowait/steal; нормализация: NOT_COLLECTED.
- Kernel failure/rate/backlog/wait controls; wait_actual start/end/delta/единицы;
  reset/boot boundaries; связь ожидания с процессом: NOT_COLLECTED.
- audit.log device/partition; bytes/inodes free, IOPS/throughput/latency, режим flush
  и freq; ротация start/end; конкурирующий host I/O: NOT_COLLECTED.
- Очереди kernel/dispatcher/sender/collector отдельно: глубина/bytes/oldest age,
  received/sent/dropped/retried; lag/drain/loss/duplicates: NOT_COLLECTED.
- Debug/capture/offload/console read-back; цена и частота наблюдения: NOT_COLLECTED.
- Single-variable diff / эквивалентность работы и входящего потока ≤5% там,
  где это требуется; вмешательство общих ресурсов: NOT_EVALUATED.
- Класс остановки / уровень атрибуции / подтверждающие данные: NOT_EVALUATED.
- Для T16: согласованный endpoint, способ/длительность разрыва ≤30 с,
  лимиты очереди/диска, rollback, состояние после восстановления: NOT_SET.

| Артефакт | Приватный путь / безопасная ссылка | SHA256 | Пробелы |
| --- | --- | --- | --- |
| summary.json | NOT_COLLECTED | NOT_COLLECTED | — |
| metrics.ndjson | NOT_COLLECTED | NOT_COLLECTED | — |
| results.csv / workload outputs / report.md | NOT_COLLECTED | NOT_COLLECTED | перечислить каждый файл отдельно |
| isolation / effective config / дополнительные метрики | NOT_COLLECTED | NOT_COLLECTED | перечислить каждый файл отдельно |

### Временная шкала механизма — не заменять её совпадением средних

| UTC / monotonic / boot ID | Изменение/событие | Генерация/очередь/ожидание/диск/доставка | Сервис NGFW | Наблюдение или интерпретация / evidence |
| --- | --- | --- | --- | --- |
| NOT_COLLECTED | NOT_COLLECTED | NOT_COLLECTED | NOT_COLLECTED | NOT_EVALUATED |

### T18 — только конфигурационный риск, без воспроизведения отказа

| Условие | Эффективная настройка / версия / read-back SHA256 | Действие / область отказа | Источник Sxx / документация версии | Риск / допуск |
| --- | --- | --- | --- | --- |
| Ошибка audit kernel / лимиты | NOT_COLLECTED | NOT_COLLECTED | S02, S07 | UNKNOWN / BLOCKED |
| Нехватка места / ошибка записи | NOT_COLLECTED | NOT_COLLECTED | S03, S08 | UNKNOWN / BLOCKED |
| Переполнение dispatcher | NOT_COLLECTED | NOT_COLLECTED | S03, S04 | UNKNOWN / BLOCKED |
| Отказ native log destination (если функция есть) | NOT_COLLECTED | NOT_COLLECTED | документация PT; S12 только аналогия | UNKNOWN |

Вердикт H8 имеет scope CONFIG_ONLY; runtime-проверка panic/halt/disk-full
не выполняется. Не переносить аналогию Cisco в эффективную конфигурацию PT.

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

### Отдельные сравнения механизмов H3–H7/H9

H2 не пересчитывается через новую «более удобную» метрику. Для T17 показать
как B-SINK против B, так и C-NET против B-SINK, с одинаковым A и 3 блоками.

| hypothesis / comparison / test | run IDs / порядок / workload | Метрика и значения по состояниям | E_min / spread / эффект | Работа и поток сопоставимы? | Атрибуция / обратимость / verdict |
| --- | --- | --- | --- | --- | --- |
| NOT_SET | NOT_COLLECTED | NOT_COLLECTED | NOT_CALCULATED | NOT_EVALUATED | NOT_EVALUATED |

Для T15-R перечислить каждую естественную ротацию и контрольные окна; отсутствие
3 событий на разрешённой длительности — INCONCLUSIVE, не повод форсировать заполнение.

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
| H3: стоимость проверки правил | NOT_TESTED | — | — | RUNTIME |
| H4: ожидание audit и NGFW | NOT_TESTED | — | — | RUNTIME |
| H5: ротация (T15-R) | NOT_TESTED | — | — | RUNTIME |
| H5: синхронизация (T15-F) | NOT_TESTED | — | — | RUNTIME |
| H6: отказ доставки / влияние на NGFW раздельно | NOT_TESTED | — | — | RUNTIME |
| H7: фильтрация у источника / коллектора | NOT_TESTED | — | — | RUNTIME |
| H8: настроенная политика отказа | NOT_TESTED | — | — | CONFIG_ONLY, не runtime-отказ |
| H9: штатные логи NGFW | NOT_TESTED | — | — | RUNTIME |
| T20: наблюдательные помехи исключены? | NOT_TESTED | — | — | контроль валидности |

Формулировка H1: «В конфигурации … группа … при … вызвала/не вызвала …;
сравнения …; восстановление A …; предел применимости …». До измерений: NOT_TESTED.

Формулировка H2: «Исключение … при … сняло …% добавленных затрат CPU
(все повторы …); полезная скорость/задержки …; T11 …; длительность …;
критерий выполнен/не выполнен/данных недостаточно потому что …».
До измерений: NOT_TESTED.

Формулировка H3–H7/H9: «При фиксированных … изменение только … привело к …;
первичная метрика …, порог …, повторы …; предполагаемый механизм … подтверждён
свидетельствами … / остаётся гипотезой; восстановление …; альтернативы …».
Стоимость ресурса и критическая деградация сервиса — отдельные подвыводы.
H8: «В конфигурации версии … при условии … настроено действие …; основание …;
фактическая остановка не воспроизводилась и не установлена этим тестом».

Практическое решение: какие правила оставить/сузить/исключить; потерянное покрытие
и остаточный риск; какая следующая проверка нужна: NOT_DECIDED.
Проверка перед публикацией: raw/секреты удалены, ссылки безопасны, отрицательные
и остановленные прогоны сохранены, выводы проверены вторым чтением: NOT_DONE.
