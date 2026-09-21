# Приёмник логов AuditD и PT NGFW

Отдельный Compose-проект `ngfw-logs` на `abykovserv`: Debian 12 + пакетный
syslog-ng 3.38, без SIEM, веб-интерфейса и зависимости от traffic-runner.
Ansible-роль: `roles/ngfw_logs`; отдельный playbook: `playbooks/ngfw-logs.yml`.
Роль также подключена к `site.yml`, но выключена по умолчанию.

Это **внешний syslog-приёмник**, а не штатный PT log collector, и не замена
локальному `/var/log/audit/audit.log`. Он не включает AuditD, не меняет его
правила/SSH/sudo, не меняет ACL/NAT, не публикует MNGT и не запускает нагрузку.

## Потоки и границы

| Поток | Получатель | Разрешённый сетевой источник | Файл на хосте |
| --- | --- | --- | --- |
| Linux AuditD, пересланный как syslog | `10.77.0.1:5514`, TCP или UDP | NGFW `10.77.0.20` | `/srv/app-data/ngfw-logs/logs/auditd/events.jsonl` |
| Собственные журналы PT NGFW, экспорт syslog | `10.77.0.1:5515`, TCP или UDP | MNGT/log collector `10.77.0.10`, NGFW `10.77.0.20` | `/srv/app-data/ngfw-logs/logs/ngfw/events.jsonl` |

Выбор файла определяется входным портом, а не полем hostname или текстом,
который прислал источник. В каждой JSONL-записи: `received_at` (время приёма),
`source_ip` (адрес TCP/UDP peer), `stream` и `raw` (исходный текст сообщения).
Syslog-ng не разбирает CEF/LEEF/audit поля: RFC3164/RFC5424 и текст CEF сохраняются
как текст внутри JSON. TCP: одно сообщение на строку, framing через LF.
Native managed-протокол `audisp-remote`, RELP и TLS в этой версии не поддерживаются.
Лимит сообщения — 64 KiB; большие сообщения и потери UDP нельзя считать
доставленными без дополнительной проверки на источнике.

Контейнер использует host network **только для привязки к `10.77.0.1`**:
нет Docker DNAT, публикации на `0.0.0.0` или LAN `192.168.1.88` и подключения
к dataplane-сетям. UFW разрешает вход через `br-ngfw-mgmt` от перечисленных IP;
фильтры syslog-ng дополнительно отбрасывают сообщения с других адресов.
Источник IP — allowlist, **не криптографическая аутентификация**: UDP может
быть подделан. Текст передаётся открыто. Это профиль только изолированной
лаборатории, не публичный/production collector. Host network также не является
изоляцией исходящих соединений контейнера.

## Хранение, права и отказы

- UID/GID `10001:10001`, read-only rootfs, `cap_drop: ALL`, no-new-privileges,
  1 CPU, 512 MiB RAM, 64 PID; нет Docker socket, host `/var/log` или `/dev/log`.
- Persistent bind mounts: `logs` (сообщения) и `state` (syslog-ng/logrotate).
  Каталоги `0700`, сообщения `0600`; читать на хосте — через разрешённый sudo.
  Сырые события не дублируются в `docker logs` и не публикуются в Git.
- Раз в 30 секунд проверяется ротация: ежедневно или при размере свыше 64 MiB;
  до 7 архивов **на поток**, с отложенным gzip. Это число архивов, не обещание
  семи дней хранения. Старейший архив автоматически удаляется при следующей
  ротации. Активные файлы переоткрываются; `copytruncate` не используется.
- Ориентир — около 1 GiB до сжатия для двух потоков, но это **не жёсткая квота**:
  за 30 секунд файл может превысить порог. Нужны свободное место, наблюдение за
  диском и отдельная filesystem quota, если требуется строгий предел.
- По умолчанию сохраняется полный `raw`, без редактирования персональных данных.
  Он может содержать имена пользователей, адреса, пути и аргументы команд.
  Перед включением реальных источников согласовать объём/срок хранения и доступ.
- Healthcheck проверяет daemon, успешный свежий цикл ротации и запись на диск,
  но **не доказывает поступление реальных событий**. Ошибка ротации или выход
  syslog-ng завершает контейнер с ошибкой; Compose может перезапустить его.
- При переполнении/недоступности возможны потери UDP и ограниченных очередей.
  TCP не даёт end-to-end подтверждения устойчивой записи. Не повышать блокирующие
  очереди AuditD ради гарантии удалённой доставки; локальный аудит сохраняется.
- Базовый образ и Debian security-пакеты обновляются при пересборке; это не
  bit-for-bit воспроизводимая сборка. Перед развёртыванием записать image ID и
  `syslog-ng --version`; не считать прежний CI проверкой будущих обновлённых пакетов.

## Развёртывание через Ansible

Предпосылки: Docker/Compose v2 и существующая management-сеть `10.77.0.1`
на `br-ngfw-mgmt`. Роль не создаёт VM/сети и не запускает NGFW. Адреса источников
приведены для этого лабораторного стенда; роль намеренно ограничивает их.

После merge оператор добавляет **на сервере**, в приватные overrides:

```yaml
ngfw_logs_enabled: true
ngfw_logs_start: true
# Выбранная политика хранения — полный raw, максимум 7 архивов на поток.
ngfw_logs_rotate_mib: 64
ngfw_logs_rotate_count: 7
```

Затем штатное применение (пароль только интерактивно):

```bash
sudo systemctl start ansible-local-apply.service
```

Для отдельного применения только приёмника после обновления checkout:

```bash
cd /opt/ubuntu_ansible_palybooks
sudo .venv/bin/ansible-playbook -i inventories/local/hosts.yml \
  playbooks/ngfw-logs.yml -e @/etc/ansible/local-overrides.yml
```

`ngfw_logs_enabled: true`, `ngfw_logs_start: false` — подготовить/остановить
приёмник и убрать его текущие UFW allow-правила, **не удаляя логи**. Просто
`enabled: false` пропускает роль и не останавливает уже запущенный контейнер.
При изменении портов/allowlist сначала выполнить stop со старыми значениями,
затем применить новые, чтобы не оставить старые allow-правила UFW.

## Подключение источников — отдельный шаг

### AuditD

Предпочтительно использовать уже разрешённый локальный syslog-forwarder.
Возможные цепочки:

1. `audit.log → rsyslog imfile → TCP syslog → 10.77.0.1:5514`.
   Наблюдение за файлом не добавляет remote plugin в очередь AuditD. Нужны
   разрешённое чтение, persistent offset, учёт ротации и ограниченная очередь.
2. `AuditD → audisp-syslog → локальный syslog-forwarder → 10.77.0.1:5514`.
   Это отдельное изменение dispatcher-профиля; не включать автоматически.

Первая схема сохраняет выбранный P1 и локальный аудит. Пример ниже — **шаблон
для согласования**, не команда установки на appliance. Использовать только
если rsyslog/imfile уже установлен и разрешён; роль не ставит сторонние
пакеты на NGFW. Не загружать imfile второй раз, если он уже подключён.

```text
module(load="imfile")
ruleset(name="ngfw_lab_audit_export") {
  action(type="omfwd" target="10.77.0.1" port="5514" protocol="tcp"
         TCP_Framing="traditional" template="RSYSLOG_SyslogProtocol23Format"
         queue.type="LinkedList" queue.size="4096"
         queue.timeoutEnqueue="0" action.resumeRetryCount="-1")
}
input(type="imfile" File="/var/log/audit/audit.log" Tag="auditd:"
      Facility="local6" Severity="info" PersistStateInterval="100"
      freshStartTail="on" Ruleset="ngfw_lab_audit_export")
```

Для imfile нужен существующий writable `workDirectory` rsyslog для state-файлов.
`freshStartTail=on` сознательно не экспортирует старую историю при первом
подключении. Очередь в примере в памяти и ограничена, при переполнении события
могут теряться; это выбор доступности NGFW, не обещание полной доставки.
Перед включением проверить конфигурацию установленной версией (`rsyslogd -N1`),
права, локальную ротацию и поведение при недоступном получателе.

`audisp-remote` в режиме `managed` нельзя направлять на этот порт: ему нужен
совместимый audit receiver с собственным протоколом, не обычный syslog-ng.

### Собственные журналы NGFW

В штатной настройке экспорта журналов PT MNGT/log collector задать получатель
`10.77.0.1`, порт `5515`. Выбирать поддерживаемый **данной версией PT** транспорт
и формат. Контейнер принимает TCP/LF и UDP; для PT syslog-export публичная
таблица потоков подтверждает UDP от MNGT. Точный путь UI/API PT 1.11.1 требует
отдельной live-проверки: переносить инструкции другой версии вслепую нельзя.

Сначала экспортировать минимальные системные/административные события.
Существующие тестовые ACL/NAT остаются без session logging: наличие приёмника
**не включает** traffic/IPS-журналы. Включение логирования всех 1200 правил
изменило бы профиль нагрузки и не входит в развёртывание приёмника.

## Проверка после применения

```bash
sudo docker compose --project-directory /srv/apps/ngfw-logs ps
sudo docker compose --project-directory /srv/apps/ngfw-logs exec -T collector \
  python3 /opt/collector/collector.py health
sudo docker compose --project-directory /srv/apps/ngfw-logs exec -T collector \
  syslog-ng-ctl stats --control=/run/syslog-ng.ctl
sudo ss -lntup '( sport = :5514 or sport = :5515 )'
```

После этого с **разрешённого источника**, используя уже имеющийся `logger`,
отправить синтетический уникальный маркер (это проверка транспорта, не AuditD):

```bash
logger --tcp --server 10.77.0.1 --port 5514 --tag lab-check 'SYNTHETIC-audit-receiver-UNIQUE'
logger --udp --server 10.77.0.1 --port 5515 --tag lab-check 'SYNTHETIC-ngfw-receiver-UNIQUE'
```

Проверить его только в нужном файле на получателе, затем отдельное реальное
событие AuditD (сопоставить serial/время с локальным журналом) и реальное
событие NGFW (сопоставить с журналом MNGT). Не публиковать raw-логи в Git/PR.
С постороннего IP сообщения не должны попадать ни в один файл. Не добавлять
LAN/localhost в allowlist только ради успешного smoke-теста.

CI проверяет реальные TCP/UDP-пакеты, обе независимые категории, JSON escaping,
отказ постороннему IP, UID/capabilities/read-only rootfs, права файлов,
ротацию/сжатие/переоткрытие, число архивов и контроль процесса.
CI использует loopback и синтетические события; это **не live-доставка с NGFW**.

Приёмник логов не устраняет отдельный блокер нагрузочного теста: для `lost`,
`backlog`, CPU и диска по-прежнему нужен разрешённый read-only probe внутри
гостя. Поступающие события не доказывают отсутствие потерянных событий.

## Источники

- [Debian auditd: audisp-syslog](https://manpages.debian.org/bookworm/auditd/audisp-syslog.8.en.html)
  — преобразование событий AuditD в syslog, отдельное от native remote protocol.
- [audit remote protocol и форматы](https://manpages.debian.org/trixie/audispd-plugins/audisp-remote.conf.5.en.html)
  — ссылка описывает протокол, не инструкцию обновления appliance.
- [rsyslog imfile](https://docs.rsyslog.com/doc/configuration/modules/imfile.html)
  — чтение файлов, offsets, ротация и freshStartTail.
- [PT NGFW: таблица потоков, версия 1.8](https://help.ptsecurity.com/ru-RU/projects/ngfw/1.8/help/9490983819)
  — UDP syslog-export от MNGT; не подтверждение UI/API версии 1.11.1.
- [syslog-ng 3.38 control](https://manpages.debian.org/bookworm/syslog-ng-core/syslog-ng-ctl.1.en.html)
  и [logrotate](https://manpages.debian.org/bookworm/logrotate/logrotate.8.en.html).
