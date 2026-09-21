# NGFW / AuditD: источники и границы переноса выводов

Проверено 21.09.2026. Приложение к
[NGFW-AUDIT-PERF-v2](ngfw-audit-performance-plan.md), не отчёт об испытаниях.
Источники — upstream-документация и материалы производителей; пересказ, не копии
статей. Версии upstream и страниц меняются: перед применением параметров сверять
с установленными kernel/auditd/NGFW. Наличие опции в master не доказывает её наличие
в appliance. Личные примеры и форумы не используются как доказательство причины.

## Linux Audit: основной предмет эксперимента

| ID / источник | Что подтверждено источником | Что проверяем у себя / ограничение |
| --- | --- | --- |
| S01 — [Linux Audit: audit.rules(7)](https://github.com/linux-audit/audit-userspace/blob/master/docs/audit.rules.7) | Syscall-аудит включает проверку правил в ядре; число и организация правил влияют на производительность | H3/T13: цена семантически эквивалентных правил. Документация не задаёт процент накладных расходов PT NGFW |
| S02 — [Linux Audit: auditctl(8)](https://github.com/linux-audit/audit-userspace/blob/master/docs/auditctl.8) | Настройки backlog, времени ожидания и failure action; critical failure при соответствующей настройке может приводить к panic | H4/T14: ожидание требует наблюдения; H8/T18: конфигурация отказа. Нельзя утверждать, что всякий пакетный audit-контекст ждёт так же, как syscall |
| S03 — [Linux Audit: auditd.conf(5)](https://github.com/linux-audit/audit-userspace/blob/master/docs/auditd.conf.5) | sync синхронизирует данные/метаданные при каждой записи; incremental_async выполняет flush асинхронно; ротация занимает время; есть очереди диспетчера и действия при ошибках/нехватке места | H5/T15, H6/T16, H8/T18. Значения и расположение параметров зависят от версии; смена режима не разрешена одной ссылкой на документацию |
| S04 — [Linux Audit: Performance and monitoring](https://github.com/linux-audit/audit-userspace/blob/master/README.md) | Рост backlog может означать чрезмерный поток событий или медленную обработку плагином | H4/H6: сначала определить архитектуру. Читатель уже записанного файла не равен синхронному плагину |
| S05 — [nftables: Log statement](https://netfilter.org/projects/nftables/manpage.html) | log level audit пишет сведения о совпавших пакетах в audit buffer; syslog и NFLOG — иные пути | T02/T03/T08: доказать наличие источника на реальном dataplane. Не создавать его без отдельного решения ради подтверждения гипотезы |
| S06 — [Red Hat: backlog exceeded при frozen filesystem](https://access.redhat.com/solutions/473223) | Описана цепочка: auditd не может писать в замороженную FS, растёт backlog, возможен hung system | H4/H5: хранилище может быть исходной причиной. Статус страницы Solution Unverified; для подтверждения случая требуется vmcore. Это не универсальная установленная причина каждого backlog exceeded |
| S07 — [Red Hat: netlink errors и panic с -f 2](https://access.redhat.com/solutions/7058400) | В публичной части Verified KB для RHEL 8/9/10 описаны ошибки No buffer space available после ротации/обновления и panic при -f 2 | H8/T18: проверить настройки, а не воспроизводить panic. Полный разбор закрыт подпиской; его содержание здесь не предполагается |
| S08 — [Red Hat: внезапная остановка Security Auditing Service](https://access.redhat.com/solutions/6616171) | Verified KB связывает остановку системы с настроенным halt при недостатке места под аудит | H8/T18: read-back условия и действия; не заполнять диск. Не доказательство такого режима по умолчанию в PT NGFW |

## Сетевые устройства: независимые механизмы и контроль помех

| ID / источник | Документированный пример | Использование в методике |
| --- | --- | --- |
| S09 — [Palo Alto: high dataplane CPU caused by packet-diag](https://knowledgebase.paloaltonetworks.com/KCSArticleDetail?id=kA10g000000boHqCAI&lang=en_US) | Включённое packet-diag logging может дать 99–100% dataplane CPU; удаление фильтра до отключения logging расширяет захват до всех пакетов и может затронуть LACP | T20: debug/capture — отдельная нагрузка, не доказательство причины AuditD. Не переносить команды PAN-OS на PT |
| S10 — [Fortinet: high CPU/vCPU due to WAD debug](https://community.fortinet.com/fortigate-3/technical-tip-mitigate-high-cpu-or-vcpu-due-to-wad-debug-commands-179071) | Technical Tip для FortiGate/FortiProxy описывает IRQ от консольного вывода; в примере одно ядро 100% IRQ при гораздо меньшем среднем CPU | T20 и метрики per-core/IRQ. Это не FortiWeb и не подтверждённый разбор личного инцидента proxyd |
| S11 — [Palo Alto: Management CPU is 100% because of %wa](https://knowledgebase.paloaltonetworks.com/kCSArticleDetail?id=kA10g0000008UPf) | В случае PA-3020/PAN-OS 8.1.11: вход 23 231 записи/с против записи 2 604/с, переполнение очереди и высокий I/O wait | H5/H9: учитывать вход/выход, диск и потери. Эти числа не являются порогом PT NGFW или современного PAN-OS |
| S12 — [Cisco ASA: logging command reference](https://www.cisco.com/c/en/us/td/docs/security/asa/asa-cli-reference/I-R/asa-command-ref-I-R/m_log-lz.html) | При недоступном TCP syslog возможен запрет новых сессий по политике безопасности; увеличение очереди на младших платформах может отнимать DMA-память у других функций | H6/H8: отличать ресурсное истощение от fail-closed; увеличение буфера не считать универсальным исправлением. Наличие аналогичной функции PT неизвестно до read-back |
| S13 — [Palo Alto: Disable hardware offload for packet captures](https://docs.paloaltonetworks.com/ngfw/administration/monitoring/take-packet-captures/disable-hardware-offload) | Отключение offload для диагностики может повышать dataplane CPU | T20: фиксировать offload, не выключать ускорение ради наблюдения. Не утверждать, что обычное логирование всегда отключает offload |
| S14 — [Palo Alto: Session logging considerations](https://docs.paloaltonetworks.com/network-security/security-policy/administration/security-rules/session-logging-considerations) | Session-start logging и смены App-ID могут увеличивать число записей и нагрузку management plane | H9/T19: тестировать только доступную штатную настройку PT, отдельно от Linux Audit и без изменения инспекции |

## Как использовать эти сведения

1. В карточке опыта указать Sxx и точную проверяемую связь, а не просто «логи
   вредят производительности». Например: S02 → H4 → T14 → wait delta / процесс /
   задержка сервиса / восстановление контроля.
2. Linux Audit работает не только внутри auditd: стоимость правил, формирование
   записей, очередь ядра, запись и доставка — разные этапы. Низкий CPU auditd
   не исключает накладных расходов ядра или ожидания I/O.
3. Определить место исключения: снижение выхода коллектора не доказывает снижение
   генерации. H7 — экспериментальный вывод из разделения этих этапов, не готовый
   результат vendor benchmark.
4. Разделять результаты: потеря аудита, задержка доставки, деградация управления,
   деградация трафика, panic/halt и тепловая остановка имеют разные свидетельства.
   Не объявлять рост backlog причиной без временной связи и атрибуции.
5. Положительный результат другого устройства не подтверждает H1/H2 для PT NGFW.
   Отрицательный результат в ограниченной лабораторной матрице не доказывает
   отсутствие проблемы на любой интенсивности или в другой реализации dataplane.

В просмотренных источниках не найден публичный подтверждённый разбор отказа
именно PT NGFW из-за исследуемого сетевого Linux Audit. Это пробел поиска,
не доказательство отсутствия проблемы. Ни один источник не разрешает включать
panic/halt, глобальный debug, отключать offload или проводить разрушительный тест.
