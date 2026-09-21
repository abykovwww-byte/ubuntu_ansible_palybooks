# Приёмник логов AuditD и PT NGFW

Отдельный Compose-проект `ngfw-logs` на `abykovserv`: Debian 12 + пакетный
syslog-ng 3.38, без SIEM, веб-интерфейса и зависимости от traffic-runner.
Ansible-роль: `roles/ngfw_logs`; отдельный playbook: `playbooks/ngfw-logs.yml`.
Роль также подключена к `site.yml`: переносимые defaults выключены, а в
инвентаре этого стенда `inventories/local` приёмник явно включён.

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

### Локальная сеть и Tailscale

Оба пути сохраняются одновременно, не требуют пересборки/перенастройки
приёмника при переезде рабочего компьютера:

| Назначение | Из локальной сети | Удалённо через Tailscale |
| --- | --- | --- |
| SSH, запуск Ansible, Compose, чтение логов | `abykov@192.168.1.88` | `abykov@100.117.52.16` |
| MNGT HTTPS | `https://192.168.1.88:8443/` напрямую | тот же URL через SSH SOCKS5 на Tailscale-подключении |
| AuditD/NGFW → приёмник | внутренняя сеть `10.77.0.0/24` | та же внутренняя сеть, независимо от доступа оператора |

Для Windows есть helper, который проверяет SSH host key и имя `abykovserv`.
Режим Auto сначала пробует LAN, затем Tailscale; можно явно выбрать любой.
Он не читает содержимое приватного ключа, не копирует его на сервер и не
меняет SSH config, маршруты или сертификаты. Нужен уже разрешённый ключ:

```powershell
.\scripts\ngfw-access.ps1 -Action Check -Transport Auto
.\scripts\ngfw-access.ps1 -Action Ssh -Transport Tailscale
.\scripts\ngfw-access.ps1 -Action Proxy -Transport Tailscale
```

Если ключ лежит в другом месте, передать `-IdentityFile 'C:\path\to\existing_key'`.
Новый SSH host key сверяется с доверенным fingerprint отдельно; скрипт не делает
`accept-new`, `StrictHostKeyChecking=no` или agent forwarding.

`Proxy` держит SOCKS5 на **локальном** `127.0.0.1:1080`, пока открыт терминал.
В отдельном браузерном профиле можно настроить этот SOCKS5 и открыть исходный
URL MNGT. Это настройка браузера оператора, не глобальное изменение прокси Windows.
Проверка через curl с публичным CA лаборатории:

```powershell
curl.exe --proxy socks5h://127.0.0.1:1080 `
  --cacert 'C:\path\to\pt-ngfw-lab-root-ca.pem' `
  --fail --output NUL --write-out '%{http_code}' https://192.168.1.88:8443/
```

Windows curl/Schannel может сообщить `revocation status is unknown`, если у
частной CA не настроены CRL/OCSP. Это не повод добавлять `--insecure` или
отключать проверку сертификата. Сам туннель можно проверить клиентом TLS с
явно доверенным лабораторным CA и обязательной проверкой имени, либо браузером
с корректно установленным CA. Такой HTTPS-check через Tailscale выполнен
21.09.2026: HTTP 200, TLS 1.3, проверка цепочки и IP имени успешна; это не
подтверждение проверки отзыва сертификатов и не проверка всего UI.

SSH переносит соединение до того же сервера через VPN, URL и проверка SAN
сертификата остаются прежними. **Прямой** `https://100.117.52.16:8443/` не
считается готовым: для него отдельно нужны listener/firewall и сертификат с
этим IP или общим DNS-именем в SAN. Не обходить проверку TLS.
Порты приёма логов 5514/5515 намеренно не выставляются в LAN/Tailscale:
данные из VM идут по management, оператор читает их через авторизованный SSH.

### Применение

Предпосылки: Docker/Compose v2 и существующая management-сеть `10.77.0.1`
на `br-ngfw-mgmt`. Роль не создаёт VM/сети и не запускает NGFW. Адреса источников
приведены для этого лабораторного стенда; роль намеренно ограничивает их.

После merge штатное применение включает приёмник из инвентаря этого стенда.
Эквивалентные настройки ниже можно переопределить **на сервере** в приватных
overrides (они имеют приоритет над inventory):

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

Цепочка: `audit.log → отдельный rsyslog imfile → TCP → 10.77.0.1:5514`.
Роль `ngfw_audit_forwarder` запускает **отдельный процесс**, а не меняет
`/etc/rsyslog.conf` или конфигурацию штатного логирования PT. Не добавляет
dispatcher plugin, не меняет `/etc/audit`, правила, backlog или AuditD service.

Предпосылки: существующий `/usr/sbin/rsyslogd` с imfile, Python 3, активные
`auditd` и `pt-ngfw-core`, root-owned `/var/log/audit/audit.log`. Если компонента
нет, обычный playbook завершается **без установки пакетов на appliance**.
После отдельного разрешения на установку можно явно включить bootstrap ниже.

Запускать на `abykovserv` от обычного оператора, у которого уже есть доступ
Docker для проверки коллектора. Не через sudo всего Ansible: SSH known_hosts
должен принадлежать оператору. Вводятся пароли **пользователя ОС NGFW `ngfw`**
и его sudo, не пароль MNGT admin. Пароли не писать в inventory/extra-vars или Git.
Ansible core 2.19+ поддерживает SSH_ASKPASS без установки sshpass. Host key уже
должен быть проверен; `StrictHostKeyChecking=yes` не отключать.

```bash
cd /opt/ubuntu_ansible_palybooks
# Только проверка предпосылок и счётчиков, без изменений:
.venv/bin/ansible-playbook -i inventories/ngfw-audit-source/hosts.yml \
  playbooks/ngfw-audit-forwarder.yml --ask-pass --ask-become-pass
# После healthy collector — отдельное явное применение:
.venv/bin/ansible-playbook -i inventories/ngfw-audit-source/hosts.yml \
  playbooks/ngfw-audit-forwarder.yml --ask-pass --ask-become-pass \
  -e ngfw_audit_forwarder_mode=apply
```

Если rsyslog отсутствует, для этого PT NGFW 1.11.1 предусмотрена **отдельная
opt-in установка из уже встроенного** `file:/opt/pt-ngfw/ngfw-repo`:

```bash
.venv/bin/ansible-playbook -i inventories/ngfw-audit-source/hosts.yml \
  playbooks/ngfw-audit-forwarder.yml --ask-pass --ask-become-pass \
  -e ngfw_audit_forwarder_mode=apply \
  -e ngfw_audit_forwarder_install_packages=true
```

Разрешённый набор — `rsyslog`, `libestr0`, `libfastjson4`, `liblognorm5`;
точные версии и проверка плана находятся в
`roles/ngfw_audit_forwarder/files/package_preflight.py`. Сначала выполняются
read-only проверки версий, локального источника и симуляция транзакции. Любое
обновление/понижение уже установленного пакета, удаление или дополнительная
зависимость требуют отдельного решения. Нет `apt update`, внешнего репозитория,
рекомендованных пакетов или автоматической установки зависимостей Ansible.
`python3-apt` должен уже присутствовать. Режим `check`, `--check` и `stop`
пакеты не устанавливают; по умолчанию opt-in выключен.

Активный общий `rsyslog.service` — причина остановки, а не разрешение забрать
его управление. Неактивная общая служба маскируется **до установки**, запуск
из package scripts дополнительно запрещает временный `policy-rc.d=101`.
Ansible восстанавливает исходный `policy-rc.d` после транзакции. Работает только
отдельная `ngfw-audit-forwarder.service`. Маска общего rsyslog и установленные
пакеты сохраняются при `stop`/ошибке: откат отключает экспорт, не удаляет
пакеты и не запускает системный logger. Это изменение лабораторного appliance,
не утверждение о поддержке такого изменения производителем.

Если `/opt/ubuntu_ansible_palybooks` ещё не обновлён и оператору запрещено sudo
на хосте, допустим отдельный обычный checkout **точного merged commit** в
пользовательском каталоге. Запускать из его корня существующим интерпретатором
`/opt/ubuntu_ansible_palybooks/.venv/bin/ansible-playbook`, с теми же inventory
и playbook. Это меняет только гостя; не обновляет root-owned checkout и не
запускает host apply. Проверки Docker на контроллере всегда `become: false`.

Перед изменениями apply проверяет здоровье коллектора с контроллера и TCP
доступность из гостя. Конфиг валидируется **установленным** rsyslogd; сохраняются
резервные копии изменяемых собственных файлов. Ошибка применения останавливает
только `ngfw-audit-forwarder`, сохраняет offset/state и требует разбора ошибки.
Автоматически возвращать предыдущий экспорт после ошибки роль не пытается.
После старта playbook создаёт одну безопасную USER-запись через `auditctl -m`
и до 20 секунд ожидает её на коллекторе. Проверяются источник `10.77.0.20`,
поток `auditd`, тип USER, audit timestamp/serial и точный уникальный маркер.
В вывод попадают только эти метаданные, не сырые журналы. Это проверяет реальный
путь AuditD с синтетическим содержимым, но не полноту/отсутствие потерь при нагрузке.
Поиск ограничен последними 256 KiB текущего и предыдущего несжатого файла;
при сильном потоке/долгом backfill возможен безопасный отказ проверки.

Служба `/etc/systemd/system/ngfw-audit-forwarder.service`: 256 MiB, 50% CPU,
32 tasks, без capabilities, read-only filesystem кроме собственных state/runtime,
без чтения домашней директории. Root нужен для чтения локального audit.log;
основные службы не перезапускаются. Systemd IP-фильтр дополнительно ограничивает
сеть адресом коллектора (поддержка зависит от cgroup/BPF ОС; это не замена
изоляции стенда). Очереди в памяти по 4096 записей, без ожидания свободного
места при enqueue. Переполнение/остановка могут терять сообщения; это профиль
доступности, не гарантия полноты удалённого аудита.

Offset хранится в `/var/lib/ngfw-audit-forwarder`, обновляется после каждой
записи. При первом запуске читается **текущий audit.log с начала**, не архивы.
Это намеренный ограниченный backfill; возможны дубликаты при потере state.
`freshStartTail=off` выбран, чтобы не пропускать первые записи нового файла при
ротации. Размер исходного активного файла ограничивает существующая политика
AuditD; она не меняется. Приёмник хранит такие записи по своей политике ротации.

Откат экспорта, не удаляющий локальные логи/offset и не выключающий AuditD:

```bash
.venv/bin/ansible-playbook -i inventories/ngfw-audit-source/hosts.yml \
  playbooks/ngfw-audit-forwarder.yml --ask-pass --ask-become-pass \
  -e ngfw_audit_forwarder_mode=stop
```

`audisp-remote` в режиме `managed` нельзя направлять на этот порт: ему нужен
совместимый audit receiver с собственным протоколом, не обычный syslog-ng.

### Собственные журналы NGFW

Путь настройки в UI текущего стенда:

1. **Параметры → Syslog-серверы → Добавить**: имя `ngfw-lab-collector`, IP
   `10.77.0.1`, порт `5515`. В списке протоколов этой версии доступен **UDP**.
2. **Параметры → Отправка событий → Добавить**: имя `ngfw-lab-audit-export`,
   «Выбранные» типы журналов **Аудит** и **Аутентификация**, сервер из шага 1.
3. Сохранить только после проверки healthy collector. Если MNGT требует
   «Отправить на устройства», сначала проверить состав pending-изменений;
   не публиковать посторонние изменения вместе с экспортом.
4. Выполнить штатное административное действие/вход в MNGT и сопоставить
   native event из его журнала с записью в `ngfw/events.jsonl`.

21.09.2026 экспорт включён, конфигурация доставлена на устройства и сборщики.
В 08:23:54 UTC получено native audit-событие `CommitSnapshot` от `10.77.0.10`
в потоке `ngfw`. Это подтверждает одну реальную доставку журнала аудита;
категория аутентификации настроена, но отдельная доставка её события пока
не подтверждена. Не создавать повторный сервер/правило при следующем
применении: использовать существующие объекты с указанными именами. Для отката
выключить только это правило пересылки; не выключать сам сбор журналов PT.

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
Отдельный контейнерный тест rsyslog проверяет чтение файла, ротацию, offset после
перезапуска и недоступного получателя. Он не проверяет systemd/cgroup ограничения
в реальном appliance — это часть проверки после гостевого apply.

Приёмник логов не устраняет отдельный блокер нагрузочного теста: для `lost`,
`backlog`, CPU и диска по-прежнему нужен разрешённый read-only probe внутри
гостя. Поступающие события не доказывают отсутствие потерянных событий.

## Источники

- [Debian auditd: audisp-syslog](https://manpages.debian.org/bookworm/auditd/audisp-syslog.8.en.html)
  — преобразование событий AuditD в syslog, отдельное от native remote protocol.
- [audit remote protocol и форматы](https://manpages.debian.org/trixie/audispd-plugins/audisp-remote.conf.5.en.html)
  — ссылка описывает протокол, не инструкцию обновления appliance.
- [rsyslog imfile](https://docs.rsyslog.com/doc/configuration/modules/imfile.html)
  — чтение файлов и offsets;
  [freshStartTail](https://docs.rsyslog.com/doc/reference/parameters/imfile-freshstarttail.html)
  — риск пропуска первых записей, поэтому отправитель оставляет его выключенным.
- [Ansible SSH connection](https://docs.ansible.com/projects/ansible/latest/collections/ansible/builtin/ssh_connection.html)
  — интерактивный механизм SSH_ASKPASS, без паролей в файлах.
- [Ansible apt](https://docs.ansible.com/projects/ansible/latest/collections/ansible/builtin/apt_module.html)
  — `policy_rc_d`, запрет автоустановки module dependencies и удаления пакетов.
- [PT NGFW: таблица потоков, версия 1.8](https://help.ptsecurity.com/ru-RU/projects/ngfw/1.8/help/9490983819)
  — UDP syslog-export от MNGT; не подтверждение UI/API версии 1.11.1.
- [syslog-ng 3.38 control](https://manpages.debian.org/bookworm/syslog-ng-core/syslog-ng-ctl.1.en.html)
  и [logrotate](https://manpages.debian.org/bookworm/logrotate/logrotate.8.en.html).
- [OpenSSH dynamic forwarding](https://man.openbsd.org/ssh) и
  [curl SOCKS5](https://curl.se/docs/manpage.html#--socks5-hostname) — удалённый
  доступ к исходному HTTPS URL без отключения проверки сертификата.
