origin/main `c2e26536e3e35ce70c7e795ccdb0e0f76f20eda2`, 2026-09-06; `git fetch origin` выполнен; новый worktree при начале аудита чистый (`git status --porcelain` пуст).

# Decision 043: аудит соответствия source и живых данных

Готовность к cutover не подтверждена: производственный Gateway остаётся старым, а актуальный acceptance Gateway уже показывает отказ памяти на новом Atomic route. Ненулевые relationships теперь существуют, но ручная проверка обнаруживает необоснованную привязку анонимного участника к каноническим NPC. Runtime Lore имеет предъявленную цепочку до релевантного следующего prompt. Полная цепочка ручного решения Администратора в доступной актуальной БД отсутствует.

Это один evidence-документ, не ADR, план исправления или новая система gates. Изменения продукта, WorldPack, wiki, skills, validator, tests и inventory не выполнялись. Сервер не изменялся: без sudo, deploy/apply, restart, новых партий/ходов, записи SQL или provider canary. Существующая новая партия продвигалась внешним процессом, не этим аудитом. Чтение SQLite — только `sqlite3.connect("file:...?...mode=ro", uri=True)`, сначала schema, затем bounded/scalar queries. Конструкторы приложения/engine/provider client на сервере не запускались; probes импортировали только чистые schema/config/helper functions.

База проверки: отдельный worktree `codex-worktrees/decision043-conformance-audit-20260906`, ветка `codex/decision043-conformance-audit-20260906`. Исходный checkout не менялся. Статусы Delivery в [Decision 043](../043-rp-stack-rebuild.md) не использованы как доказательство; само решение использовано только как перечень требований. Source ссылки и номера строк относятся к указанному базовому SHA.

Сокращения путей: **G** = `roles/apps/files/rp-stack/rp-gateway`; **U** = `roles/apps/files/rp-stack/rp-light-gui`; **W** = `roles/apps/files/rp-stack/worldpacks/day-watch-moscow-v2`. E1–E11 ниже — собственные измерения и конкретные evidence; B-ID — описание каждого незакрытого результата. Полные locations всех расширившихся целевых grep приведены в приложении. Search hits сами по себе не считаются подтверждением механики.

Вердикты: **закрыто** относится к узкому названному исходу, предъявленному на указанном проверочном контуре, и не означает production activation. **закрыто в source** означает присутствующую реализацию без соответствующей production-приёмки. **не закрыто** требует наблюдённого дефекта/несоответствия; **не доказано** — недостатка данных для вывода о соответствии; **не проверено на живом** — конкретной невыполненной live-проверки. Метод «локальный тест» включает статическое измерение/прочитанный исполняемый код и не доказывает игровое качество; «ничем» означает отсутствие достаточного доказательства полного исхода при наличии перечисленного частичного материала.

## Часть A — соответствие

Итог по 57 строкам требований (повторы одного механизма в разных разделах Decision не являются разными дефектами): **закрыто 11; не закрыто 17; не доказано 5; закрыто в source 22; не проверено на живом 2.**

| ID | Боль / требование | Вердикт | Доказательство | Чем доказано |
| --- | --- | --- | --- | --- |
| C01 | Context 449–453: контрактные ревизии и 123 гейта | закрыто | E2: в исполняемом clean app нет contract_revision; во всех 12 таблицах schema 8 такого столбца нет. | container probe |
| C02 | Context 454: восемь RP-миров на разных ревизиях | закрыто в source | E3/E8: loader допускает один exact slug; production ещё старый. | container probe |
| C03 | Context 455–458: отношения дают ноль полезных применений | не закрыто | E5: текущие 3 non-seed causes применены, но анонимный NPC ошибочно связан с двумя canonical character_id; старый v60 дал 0. | чтение БД |
| C04 | Context 459–469: claim/restart расходует attempts | закрыто в source | E4/E6: source increments только в failure handlers, реальные failures объясняют attempts; полного restart experiment текущего образа нет (K08). | чтение БД |
| C05 | Context 470–473: отказ теряет ход либо коммитит fallback | закрыто | E4: call 105 error → call 112 same request → ровно один turn 46; шаблонный commit удалён. | чтение БД |
| C06 | Context 474–476: обычная проза отсекается substring-валидатором | закрыто | E4: deployed clean modules совпадают с source; forbidden_claims только в явном PlayerCorrection, substring-проверки narrator output нет. | container probe |
| C07 | Context 477–478: история раздувает prompt | не закрыто | E6/E7: RAW не теряется, но memory coverage новой партии застыло на 17; long queue деградирует. | чтение БД |
| C08 | Context 479–480: три байт-идентичных narrative preset | закрыто | E3: materialized 12 presets / четыре старта / три разных scenario system; World hash общий. | container probe |
| C09 | Context 481–493: храповик тестов, validator и полного CI | не закрыто | E9: консервативно 6479 LOC; exact prose и source-string gates; весь Gateway на каждом PR. | локальный тест |
| G01 | §6.1: RAW → candidate → применение → prompt → сцена | не закрыто | E5: provenance и доставка в prompt есть; правильная адресация и влияние на отношение NPC не подтверждены, найдена ошибочная адресация. | чтение БД |
| G02 | §6.2: RAW → runtime Lore с provenance → релевантный следующий prompt | закрыто | E5: card 1/source v1/call 263 → narrator v2/call 264, текст относится к продолжающейся сцене. Это один предъявленный проход, не гарантия качества всех карт. | чтение БД |
| G03 | §6.3: RAW → admin proposal → ручной accept/reject → revision → prompt | не доказано | E5/E8: в актуальной БД 0 proposals/0 guidance; полного живого обмена нет. | чтение БД |
| D01 | Удаление: ревизии 0–11 и compatibility | закрыто в source | E2: нет revision-dispatch; сохранённая legacy DB отделена от нового движка. | container probe |
| D02 | Удаление: RP Scene State | закрыто в source | E2/E3: прежнего RPSceneState нет; Scenario.initial_state остаётся отдельным JSON, не прежним runtime-классом. | локальный тест |
| D03 | Удаление: D20, /check, IntentParser, RuleEngine | закрыто в source | E2: RP вызовов/route нет; d20 в CSS — часть hex-цвета, не бросок. | локальный тест |
| D04 | Удаление: prose validator, repair, template fallback | закрыто | E4: три grep-совпадения — docstring, allow_fallbacks=false и catalog tag; один plain-text narrator call. | container probe |
| D05 | Удаление: substring forbidden_claims при сохранении World laws | закрыто | E4: field остаётся в typed rule correction и accepted prompt overlay, не сравнивается с прозой. | container probe |
| D06 | Удаление: channel=auto, gm_intent, route_required | закрыто в source | E2/E4: активного routing нет, явная PlayerCorrection сохранена. | локальный тест |
| D07 | Удаление: MemorySummarizer и параллельные memory endpoints | закрыто в source | E2/E7: прежнего модуля/маршрута нет, одна clean memory цепочка. | локальный тест |
| D08 | Удаление: восемь single-campaign /api/state* и /api/world* | закрыто в source | E2: legacy routes отсутствуют; /api/worldpacks и /api/worldpacks/{id} — действующий каталог, не старые single-campaign endpoints. | локальный тест |
| D09 | Удаление: старый supervisor после переноса полезного | закрыто в source | E8: clean status + отдельный Administrator; прежнего RPSupervisorService нет. | локальный тест |
| D10 | Удаление: пять полных state seeds | не закрыто | E3: root seed удалён; четыре Scenario-owned full state-seed.json остаются, все читаются loader и 12 presets. | container probe |
| D11 | Удаление: root aliases, четыре GM prompt copies, SillyTavern export | закрыто в source | E3: прежние presets/{book,action,strategic}, root aliases/export отсутствуют; три scenario-experience имеют разных владельцев и содержимое. | локальный тест |
| D12 | Удаление: exact-prompt snapshots и тесты внутренних процедур | не закрыто | E9: пять русских exact prompt phrases, одна английская фраза и GUI source-string assertions; полного golden hash нет. | локальный тест |
| D13 | Удаление: revision matrix, retired-world fixtures | закрыто в source | E2/E9: восемь текущих тестовых файлов; revision-named матрица/fixtures снятых миров отсутствуют. | локальный тест |
| D14 | Удаление: 38 startup migrations | закрыто в source | E2: clean schema bootstrap с user_version/application_id, а не цепочка старых миграций; legacy DB не мигрируется. | container probe |
| D15 | Удаление: mock:// в девяти production modules | закрыто в source | E2: активного mock:// и прежних девяти модулей нет; test doubles остаются в tests. | локальный тест |
| P01 | Процесс 1: замена заканчивается удалением | не закрыто | E3/E9: остаются четыре полных seed и четыре осиротевшие WorldPack validator functions (658 LOC). | локальный тест |
| P02 | Процесс 2: legacy suite заморожена, clean без compatibility | закрыто в source | E2/E9: текущий clean путь не имеет revision flag, legacy test matrix удалена. Это состояние базы аудита, не аудит каждого исторического PR. | локальный тест |
| P03 | Процесс 3: автоматическая проверка только по риску | не закрыто | E9: перечислены проверки prose и наличия строк, не проверяющие игровой исход. | локальный тест |
| P04 | Процесс 4: 5000 LOC / 60s CI / 30s focused / без второго образа | не закрыто | E9: scripts/ci.ps1 печатает 5028/28, полный охват даёт 6479/1479; local 8.8s, GitHub full 5.084s; второго образа в workflow нет. | локальный тест |
| P05 | Процесс 5: один executable owner World/Scenario | закрыто в source | E3/E9: новый world.json/rp-world.v1 не описан в repository validator; старые manifest.json ветки осиротели, но не валидируют новый формат. | локальный тест |
| P06 | Процесс 6: gate соответствует этапу, focused ordinary PR | не закрыто | E9: все четыре jobs включаются для каждого PR независимо от paths; полный Gateway и Docker build также для doc-only. | локальный тест |
| K01 | Критерий 1: один World API, чистая RP DB, отдельный Awareness | закрыто в source | E3/E10: acceptance schema 8 содержит только clean Parties; production rp_engine.db отсутствует. Внешняя Awareness-приёмка отдельно не повторялась. | container probe |
| K02 | Критерий 2: preset и принципиально иной free Scenario | не доказано | E3/E10: текущие три Parties — presets; сохранённого free flow с наблюдением различий нет. | чтение БД |
| K03 | Критерий 3: человек читает 12 blind A/B, строгий prose verdict | не доказано | E11: human A/B принят (8/1/3), narrator.py неизменен; residual 10–12 остаются, строгий итог без нарушений по всем 12 не подтверждён этим аудитом. | ничем |
| K04 | Критерий 4: ручные первые 20 ходов + контрастный второй старт | не доказано | E5/E11: ручная выборка причин не равна полной 20-turn continuity приёмке; контрастный второй живой старт не предъявлен. | ничем |
| K05 | Критерий 5: настоящая W+2A партия, RAW/budget/coverage/cache | не закрыто | E6/E7: W=50 A=8; две актуальные настоящие Parties v60; новая memory застыла на17 и дала13truncation failures. Второй anchor66 не достигнут. | чтение БД |
| K06 | Критерий 6: все три цепочки §6 целиком | не закрыто | E5: одна runtime-Lore цепочка предъявлена; relationship causal quality не закрыта, admin chain не доказана. | чтение БД |
| K07 | Критерий 7: provider fail без commit, поле/retry, Atomic off не блокирует | не проверено на живом | E4: DB retry доказан; DOM сохранения поля и Atomic-disabled режим в текущем живом контуре не переключались. | чтение БД |
| K08 | Критерий 8: restart с непустой очередью без потерь/роста attempts | не проверено на живом | E4: owner runner/source и существующие tests проходят; снимков до/после restart текущего образа нет. | локальный тест |
| K09 | Критерий 9: World не изменён, Party hash переживает update World | не доказано | E3: stored pairs/hash согласованы, immutable triggers есть; события обновления source World между двумя DB наблюдениями нет. | чтение БД |
| K10 | Критерий 10: модель/status/success/error/last_error/kill switch трёх ролей | не закрыто | E8: Atomic status ошибочно сообщает local Gemma; GUI source не выводит success_count/error_count. | container probe |
| L01 | §1: WorldDefinition отвергает пять Scenario-only полей | закрыто | E3: все пять добавлений отклонены ValidationError в container pure schema probe; boundary test существует. | container probe |
| L02 | §1: независимые snapshot+hash, RAW и исходный World не переписываются | закрыто | E3: обе пары всех трёх Parties пересчитаны, 13 immutable triggers, RAW/request consistency; дальнейший source update — K09. | чтение БД |
| L03 | §4–5: local_overrides читается при исполнении | закрыто в source | E3: 13 мест поля; narrator, Lore API, budget validation, draft context используют scenario lore; гипотеза «только хранится» неверна. | container probe |
| L04 | §4: Lore origin world/scenario/runtime и Scenario → prompt | закрыто в source | E3: три origin поддержаны; в текущих Parties scenario cards=0, поэтому live Scenario → prompt не доказан. | container probe |
| L05 | §4: независимое принятие relationship candidates | не закрыто | E5: domain-invalid candidate изолируется в loop 604–685; shape-invalid candidate отвергает весь strict result ещё до loop. | container probe |
| L06 | §3: атомарный pending → running с status в UPDATE | закрыто в source | E4: оба claim выполняются BEGIN IMMEDIATE + outer AND status='pending'; локальная конкурентная проверка существует. | локальный тест |
| L07 | §3: startup/shutdown/cancel/await у runner | закрыто в source | E4: runner.py:64–89, отдельные worker tasks и CancelledError release. | локальный тест |
| L08 | §2–3: Administrator и Atomic — разные handlers/очереди | закрыто | E6/E8: отдельные таблицы, реальные DeepSeek Atomic и local Gemma admin calls, разные worker handlers. | чтение БД |
| L09 | §2/§5: suggest accept/reject и безопасный PlayerCorrection | закрыто в source | E4/E8: owner-scoped API/GUI, allowlist, immutable target/before, versioned guidance; текущие proposal/correction таблицы пусты. | локальный тест |
| F01 | Удалённый flag: внешние docs/skills не должны обещать переключатель | не закрыто | E1: 11 flag hits в семи внешних поверхностях описывают удалённый source/IaC механизм. | локальный тест |
| F02 | Provider: auto/free/nvidia отвергаются до вызова | закрыто | E8: pure route probe отклонил все пять вариантов до конструирования клиента; активный narrator catalog exact. | container probe |
| F03 | Provider: immutable exact Party binding/BYOK endpoint/no fallback | закрыто в source | E3/E8: stored exact binding и immutable trigger, preflight route/BYOK checks; ошибочный endpoint живым запросом не посылался. | локальный тест |
| F04 | Atomic route replacement решает длинную очередь | не закрыто | E6: relationships/lore быстрее, memory13/36завершённых jobs failed;24pending каждого типа после внешнего отключения Atomic. HTTP200 не равен succeeded. | чтение БД |
| F05 | Runtime Lore сохраняет attribution/grounding и полезную релевантность | не закрыто | E5/E6: cards7/15 требуют фактов вне selected spans; newest-fit projection и задержка23/24→27; keyword budget failure source34. | чтение БД |

## Часть B — описание каждого незакрытого исхода

Каждая карточка относится только к соответствующей строке A. Для source-only/непроверенного результата причина — конкретный разрыв доказательства, а не автоматически объявленный дефект кода. Проверки исхода здесь описаны для возможной последующей приёмки; они не выполнялись и не являются новым планом/gate. Очерёдность, необходимость ADR и обновление Delivery-статуса решения выбирает владелец.

Категории radius приведены из задания. «Локально в одном модуле» означает существующую функциональную границу без World/Scenario/schema изменения; когда нужны оба края API/GUI, это явно сказано в оценке среза. Для четырех seeds рассмотрена граница общего World-Scenario контракта; **вариант изменения versioned initial_state schema затронет authoring skills и production WorldPack loader/validator, потребует решения владельца и нескольких срезов**. Оснований заводить новый формат в scripts/validate-repository.py аудит не нашёл.

### B-C02 — Context 454: восемь RP-миров на разных ревизиях

Текущий вердикт: **закрыто в source**. Конкретное наблюдение: E3/E8: loader допускает один exact slug; production ещё старый.

- **Симптом и цена:** Пользователь production получает прежний RP-контур; наличие удаления/замены в main ещё не является его новым игровым поведением.
- **Причина и механизм:** Дефект нового алгоритма этим вердиктом не установлен. Граница запуска: `G/app/main.py:80`, `roles/apps/templates/rp-stack.compose.yml.j2:55`, `roles/apps/templates/rp-stack.env.j2:6`. Production image содержит main.py от 2ad61019, тогда как проверенный acceptance app соответствует c2e2653 (E10).
- **Сохраняемые инварианты:** Раздельные source/apply/activation/live; чистая RP DB; сохранность legacy SQLite/state/backups; без compatibility и переноса старых Parties.
- **Проверка реального исхода:** После отдельно разрешённой поставки: сопоставить image и исходники; прочитать схему и Parties новой production DB mode=ro; пройти относящийся к этой строке API/GUI путь и связать его с committed turn/request. Для K01 отдельно требуется evidence внешнего Awareness gate.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Изменение исходников по этой строке не обосновано; нужна отдельная поставка и приёмка по решению владельца. Категория radius относится к границе запуска, не к разрешению менять весь продукт.

### B-C03 — Context 455–458: отношения дают ноль полезных применений

Текущий вердикт: **не закрыто**. Конкретное наблюдение: E5: текущие 3 non-seed causes применены, но анонимный NPC ошибочно связан с двумя canonical character_id; старый v60 дал 0.

- **Симптом и цена:** Отношение канонического NPC меняется из-за реплики неназванного участника либо из-за обещания вместо исполненного поступка. Следующий prompt получает ложную причинную историю.
- **Причина и механизм:** `G/app/rp/provider.py:114` передаёт single-turn spans и общий canonical catalog; `G/app/rp/mechanics.py:606` проверяет membership character_id, а :611 — существование span, но это не доказывает identity, actor/direction и смысл event. В E5 один мужчина получает edgar, затем anton-gorodetsky.
- **Сохраняемые инварианты:** Модель разрешает смысл/alias/участника/направление; код проверяет schema/ID/provenance/atomic apply. Нельзя вернуть substring-семантику, править RAW, seed историю или молча переадресовать сохранённые causes.
- **Проверка реального исхода:** В живой партии вручную сопоставить полный контекст и выбранные spans для 5–10 реальных causes; проверить actor/target/event, committed delta, точный следующий prompt и последующее видимое поведение NPC. Одних ненулевых counts недостаточно.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Bounded extraction boundary допускает один срез, но критерий разрешения анонимной личности и обращение с уже ошибочными causes требует решения владельца; аудит не выбирает способ.

### B-C04 — Context 459–469: claim/restart расходует attempts

Текущий вердикт: **закрыто в source**. Конкретное наблюдение: E4/E6: source increments только в failure handlers, реальные failures объясняют attempts; полного restart experiment текущего образа нет (K08).

- **Симптом и цена:** Текущий образ не прошёл наблюдаемую проверку рестарта с непустой очередью; source correctness не доказывает recovery после остановки реального процесса.
- **Причина и механизм:** `G/app/rp/runner.py:64–89` владеет recover/start/cancel/await; :113–115/:161–163 освобождает claim; `turn_engine.py:1092–1122` восстанавливает status без attempts. Нет связанного before/after process timeline текущего образа.
- **Сохраняемые инварианты:** Attempts только за фактический failure; at-most-once persistence, отсутствие lost jobs; отдельные handlers; без queue-platform.
- **Проверка реального исхода:** После отдельного разрешения зафиксировать DB pending/running/attempts и container start; выполнить restart; связать recovered job IDs, provider failures, attempts и ровно один persisted result каждого job.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Один recovery experiment, не предварительная реализация. Рестарт запрещён рамками этого аудита.

### B-C07 — Context 477–478: история раздувает prompt

Текущий вердикт: **не закрыто**. Конкретное наблюдение: E6/E7: RAW не теряется, но memory coverage новой партии застыло на 17; long queue деградирует.

- **Симптом и цена:** Служебное покрытие останавливается, дорогие повторные полные ответы заканчиваются обрезанием, последовательный worker задерживает также relations/lore. Игрок продолжает ходить, но играет с устаревшими производными данными.
- **Причина и механизм:** `G/app/rp/provider.py:346` собирает полную структурную память; :405–413 даёт max_tokens=16384; `provider.py:632` отвергает length. `runner.py:91–137` обслуживает общий Atomic worker, terminal rejection не обновляет coverage. На активном v55:10length, safe17, lag22; итоговый v60:13truncation и24pending каждого типа после внешнего отключения роли (E6), не зависание worker.
- **Сохраняемые инварианты:** Не обрезать uncovered RAW; safe coverage=min sections, monotonic; immutable snapshots/RAW; exact provider, без fallback; bounded non-RAW и hard input. HTTP200 не считать семантическим успехом.
- **Проверка реального исхода:** После отдельного разрешённого изменения прочитать живую настоящую партию до W+2A и после дренирования очереди: section coverage, actual payload/finish_reason, failed/succeeded/pending, длительности, source_version lag; сопоставить каждый RAW и оба anchor shifts. Не засевать snapshots/jobs и не выдавать seeded run за настоящую партию.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Первый объект расследования локален provider memory boundary; изменение модели, формы памяти либо политики общей очереди — решение владельца. Данные не доказывают, что одного повышения лимита достаточно.

### B-C09 — Context 481–493: храповик тестов, validator и полного CI

Текущий вердикт: **не закрыто**. Конкретное наблюдение: E9: консервативно 6479 LOC; exact prose и source-string gates; весь Gateway на каждом PR.

- **Симптом и цена:** Изменение одной границы требует лишних проверок; осиротевшие правила продолжают занимать проверочный бюджет. Автоматическая зелень скрывает отсутствие игровой приёмки.
- **Причина и механизм:** `scripts/validate-repository.py:38,162,381,498` сохраняет четыре старых WorldPack function, вызовы :1386–1389; текущих */manifest.json нет. `.github/workflows/ci.yml:4` запускает общий PR workflow, gateway :44 — полный pytest; paths boundary отсутствует.
- **Сохраняемые инварианты:** Не добавлять зеркальные validator/registry/gates; один loader/schema; не удалить общие security/deploy/Awareness проверки; существенные runtime/storage границы должны остаться защищены.
- **Проверка реального исхода:** Это процессная боль: БД сама не доказывает удаление gate. Нужны проверка реально исполненного CI job graph и повторный LOC inventory; затем авторитетная живая Party с неизменёнными commits, memory/cause/lore/proposal цепочками подтверждает, что сокращение проверок не заменило функциональность.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Validator cleanup локален, path-aware CI относится к уже выделенному общему треку. Совместить их в один исправительный срез нельзя считать обязательным; последовательность выбирает владелец.

### B-G01 — §6.1: RAW → candidate → применение → prompt → сцена

Текущий вердикт: **не закрыто**. Конкретное наблюдение: E5: provenance и доставка в prompt есть; правильная адресация и влияние на отношение NPC не подтверждены, найдена ошибочная адресация.

- **Симптом и цена:** Отношение канонического NPC меняется из-за реплики неназванного участника либо из-за обещания вместо исполненного поступка. Следующий prompt получает ложную причинную историю.
- **Причина и механизм:** `G/app/rp/provider.py:114` передаёт single-turn spans и общий canonical catalog; `G/app/rp/mechanics.py:606` проверяет membership character_id, а :611 — существование span, но это не доказывает identity, actor/direction и смысл event. В E5 один мужчина получает edgar, затем anton-gorodetsky.
- **Сохраняемые инварианты:** Модель разрешает смысл/alias/участника/направление; код проверяет schema/ID/provenance/atomic apply. Нельзя вернуть substring-семантику, править RAW, seed историю или молча переадресовать сохранённые causes.
- **Проверка реального исхода:** В живой партии вручную сопоставить полный контекст и выбранные spans для 5–10 реальных causes; проверить actor/target/event, committed delta, точный следующий prompt и последующее видимое поведение NPC. Одних ненулевых counts недостаточно.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Bounded extraction boundary допускает один срез, но критерий разрешения анонимной личности и обращение с уже ошибочными causes требует решения владельца; аудит не выбирает способ.

### B-G03 — §6.3: RAW → admin proposal → ручной accept/reject → revision → prompt

Текущий вердикт: **не доказано**. Конкретное наблюдение: E5/E8: в актуальной БД 0 proposals/0 guidance; полного живого обмена нет.

- **Симптом и цена:** Администратор наблюдается как роль и отвечает, но без сохранённого proposal и ручного decision его полезное воздействие на партию не доказано.
- **Причина и механизм:** `G/app/main.py:1174–1209` экспонирует list/decision; `turn_engine.py:2308–2435` делает reject либо новую guidance revision; `narrator.py:247–262` проецирует guidance; GUI buttons `U/app.js:195–199`. В DB 0 proposals/guidance/corrections.
- **Сохраняемые инварианты:** Suggest по умолчанию, owner check, allowlist/expiry/before/idempotency; revision guidance отдельно от gameplay current_version; RAW/World immutable; classifier не открывает correction.
- **Проверка реального исхода:** На живом RAW получить proposal с provenance, вручную reject один и accept другой; прочитать decision, новую guidance revision, unchanged Party version и следующий saved prompt. Для PlayerCorrection отдельно подтвердить explicit initiation и immutable target.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Нужен один приёмочный обмен; если модель не предлагает полезного, это отдельный model-boundary вопрос владельца, а не повод засевать proposal.

### B-D01 — Удаление: ревизии 0–11 и compatibility

Текущий вердикт: **закрыто в source**. Конкретное наблюдение: E2: нет revision-dispatch; сохранённая legacy DB отделена от нового движка.

- **Симптом и цена:** Пользователь production получает прежний RP-контур; наличие удаления/замены в main ещё не является его новым игровым поведением.
- **Причина и механизм:** Дефект нового алгоритма этим вердиктом не установлен. Граница запуска: `G/app/main.py:80`, `roles/apps/templates/rp-stack.compose.yml.j2:55`, `roles/apps/templates/rp-stack.env.j2:6`. Production image содержит main.py от 2ad61019, тогда как проверенный acceptance app соответствует c2e2653 (E10).
- **Сохраняемые инварианты:** Раздельные source/apply/activation/live; чистая RP DB; сохранность legacy SQLite/state/backups; без compatibility и переноса старых Parties.
- **Проверка реального исхода:** После отдельно разрешённой поставки: сопоставить image и исходники; прочитать схему и Parties новой production DB mode=ro; пройти относящийся к этой строке API/GUI путь и связать его с committed turn/request. Для K01 отдельно требуется evidence внешнего Awareness gate.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Изменение исходников по этой строке не обосновано; нужна отдельная поставка и приёмка по решению владельца. Категория radius относится к границе запуска, не к разрешению менять весь продукт.

### B-D02 — Удаление: RP Scene State

Текущий вердикт: **закрыто в source**. Конкретное наблюдение: E2/E3: прежнего RPSceneState нет; Scenario.initial_state остаётся отдельным JSON, не прежним runtime-классом.

- **Симптом и цена:** Пользователь production получает прежний RP-контур; наличие удаления/замены в main ещё не является его новым игровым поведением.
- **Причина и механизм:** Дефект нового алгоритма этим вердиктом не установлен. Граница запуска: `G/app/main.py:80`, `roles/apps/templates/rp-stack.compose.yml.j2:55`, `roles/apps/templates/rp-stack.env.j2:6`. Production image содержит main.py от 2ad61019, тогда как проверенный acceptance app соответствует c2e2653 (E10).
- **Сохраняемые инварианты:** Раздельные source/apply/activation/live; чистая RP DB; сохранность legacy SQLite/state/backups; без compatibility и переноса старых Parties.
- **Проверка реального исхода:** После отдельно разрешённой поставки: сопоставить image и исходники; прочитать схему и Parties новой production DB mode=ro; пройти относящийся к этой строке API/GUI путь и связать его с committed turn/request. Для K01 отдельно требуется evidence внешнего Awareness gate.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Изменение исходников по этой строке не обосновано; нужна отдельная поставка и приёмка по решению владельца. Категория radius относится к границе запуска, не к разрешению менять весь продукт.

### B-D03 — Удаление: D20, /check, IntentParser, RuleEngine

Текущий вердикт: **закрыто в source**. Конкретное наблюдение: E2: RP вызовов/route нет; d20 в CSS — часть hex-цвета, не бросок.

- **Симптом и цена:** Пользователь production получает прежний RP-контур; наличие удаления/замены в main ещё не является его новым игровым поведением.
- **Причина и механизм:** Дефект нового алгоритма этим вердиктом не установлен. Граница запуска: `G/app/main.py:80`, `roles/apps/templates/rp-stack.compose.yml.j2:55`, `roles/apps/templates/rp-stack.env.j2:6`. Production image содержит main.py от 2ad61019, тогда как проверенный acceptance app соответствует c2e2653 (E10).
- **Сохраняемые инварианты:** Раздельные source/apply/activation/live; чистая RP DB; сохранность legacy SQLite/state/backups; без compatibility и переноса старых Parties.
- **Проверка реального исхода:** После отдельно разрешённой поставки: сопоставить image и исходники; прочитать схему и Parties новой production DB mode=ro; пройти относящийся к этой строке API/GUI путь и связать его с committed turn/request. Для K01 отдельно требуется evidence внешнего Awareness gate.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Изменение исходников по этой строке не обосновано; нужна отдельная поставка и приёмка по решению владельца. Категория radius относится к границе запуска, не к разрешению менять весь продукт.

### B-D06 — Удаление: channel=auto, gm_intent, route_required

Текущий вердикт: **закрыто в source**. Конкретное наблюдение: E2/E4: активного routing нет, явная PlayerCorrection сохранена.

- **Симптом и цена:** Пользователь production получает прежний RP-контур; наличие удаления/замены в main ещё не является его новым игровым поведением.
- **Причина и механизм:** Дефект нового алгоритма этим вердиктом не установлен. Граница запуска: `G/app/main.py:80`, `roles/apps/templates/rp-stack.compose.yml.j2:55`, `roles/apps/templates/rp-stack.env.j2:6`. Production image содержит main.py от 2ad61019, тогда как проверенный acceptance app соответствует c2e2653 (E10).
- **Сохраняемые инварианты:** Раздельные source/apply/activation/live; чистая RP DB; сохранность legacy SQLite/state/backups; без compatibility и переноса старых Parties.
- **Проверка реального исхода:** После отдельно разрешённой поставки: сопоставить image и исходники; прочитать схему и Parties новой production DB mode=ro; пройти относящийся к этой строке API/GUI путь и связать его с committed turn/request. Для K01 отдельно требуется evidence внешнего Awareness gate.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Изменение исходников по этой строке не обосновано; нужна отдельная поставка и приёмка по решению владельца. Категория radius относится к границе запуска, не к разрешению менять весь продукт.

### B-D07 — Удаление: MemorySummarizer и параллельные memory endpoints

Текущий вердикт: **закрыто в source**. Конкретное наблюдение: E2/E7: прежнего модуля/маршрута нет, одна clean memory цепочка.

- **Симптом и цена:** Пользователь production получает прежний RP-контур; наличие удаления/замены в main ещё не является его новым игровым поведением.
- **Причина и механизм:** Дефект нового алгоритма этим вердиктом не установлен. Граница запуска: `G/app/main.py:80`, `roles/apps/templates/rp-stack.compose.yml.j2:55`, `roles/apps/templates/rp-stack.env.j2:6`. Production image содержит main.py от 2ad61019, тогда как проверенный acceptance app соответствует c2e2653 (E10).
- **Сохраняемые инварианты:** Раздельные source/apply/activation/live; чистая RP DB; сохранность legacy SQLite/state/backups; без compatibility и переноса старых Parties.
- **Проверка реального исхода:** После отдельно разрешённой поставки: сопоставить image и исходники; прочитать схему и Parties новой production DB mode=ro; пройти относящийся к этой строке API/GUI путь и связать его с committed turn/request. Для K01 отдельно требуется evidence внешнего Awareness gate.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Изменение исходников по этой строке не обосновано; нужна отдельная поставка и приёмка по решению владельца. Категория radius относится к границе запуска, не к разрешению менять весь продукт.

### B-D08 — Удаление: восемь single-campaign /api/state* и /api/world*

Текущий вердикт: **закрыто в source**. Конкретное наблюдение: E2: legacy routes отсутствуют; /api/worldpacks и /api/worldpacks/{id} — действующий каталог, не старые single-campaign endpoints.

- **Симптом и цена:** Пользователь production получает прежний RP-контур; наличие удаления/замены в main ещё не является его новым игровым поведением.
- **Причина и механизм:** Дефект нового алгоритма этим вердиктом не установлен. Граница запуска: `G/app/main.py:80`, `roles/apps/templates/rp-stack.compose.yml.j2:55`, `roles/apps/templates/rp-stack.env.j2:6`. Production image содержит main.py от 2ad61019, тогда как проверенный acceptance app соответствует c2e2653 (E10).
- **Сохраняемые инварианты:** Раздельные source/apply/activation/live; чистая RP DB; сохранность legacy SQLite/state/backups; без compatibility и переноса старых Parties.
- **Проверка реального исхода:** После отдельно разрешённой поставки: сопоставить image и исходники; прочитать схему и Parties новой production DB mode=ro; пройти относящийся к этой строке API/GUI путь и связать его с committed turn/request. Для K01 отдельно требуется evidence внешнего Awareness gate.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Изменение исходников по этой строке не обосновано; нужна отдельная поставка и приёмка по решению владельца. Категория radius относится к границе запуска, не к разрешению менять весь продукт.

### B-D09 — Удаление: старый supervisor после переноса полезного

Текущий вердикт: **закрыто в source**. Конкретное наблюдение: E8: clean status + отдельный Administrator; прежнего RPSupervisorService нет.

- **Симптом и цена:** Пользователь production получает прежний RP-контур; наличие удаления/замены в main ещё не является его новым игровым поведением.
- **Причина и механизм:** Дефект нового алгоритма этим вердиктом не установлен. Граница запуска: `G/app/main.py:80`, `roles/apps/templates/rp-stack.compose.yml.j2:55`, `roles/apps/templates/rp-stack.env.j2:6`. Production image содержит main.py от 2ad61019, тогда как проверенный acceptance app соответствует c2e2653 (E10).
- **Сохраняемые инварианты:** Раздельные source/apply/activation/live; чистая RP DB; сохранность legacy SQLite/state/backups; без compatibility и переноса старых Parties.
- **Проверка реального исхода:** После отдельно разрешённой поставки: сопоставить image и исходники; прочитать схему и Parties новой production DB mode=ro; пройти относящийся к этой строке API/GUI путь и связать его с committed turn/request. Для K01 отдельно требуется evidence внешнего Awareness gate.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Изменение исходников по этой строке не обосновано; нужна отдельная поставка и приёмка по решению владельца. Категория radius относится к границе запуска, не к разрешению менять весь продукт.

### B-D10 — Удаление: пять полных state seeds

Текущий вердикт: **не закрыто**. Конкретное наблюдение: E3: root seed удалён; четыре Scenario-owned full state-seed.json остаются, все читаются loader и 12 presets.

- **Симптом и цена:** Четыре полных legacy-shaped начальных состояния продолжают входить в каждый ScenarioSnapshot; PlayerCharacter встречается даже в World-owned prose и оставляет неоднозначную ссылку на прежний контракт игрока. Фактическая подмена выбранного игрока из-за этого слова не установлена.
- **Причина и механизм:** `G/app/rp/content.py:278` читает referenced initial_state; 12 `scenario-presets/*.json:14` ссылаются на четыре state-seed. `world.json` включает `rules/checks.md:31` и `world-info/index.md:11`; последние относятся к World, openings — к Scenario. E3 перечисляет все 50 hits.
- **Сохраняемые инварианты:** Четыре старта/три стиля и уникальный канон сохранить; не менять snapshots начатых Parties; World не содержит player binding; разделённые hashes.
- **Проверка реального исхода:** После разрешённого изменения materialize все starts/preset и free; прочитать фактические ScenarioSnapshot, first prompt/turn и player identity, убедиться, что стартовые NPC/отношения/ресурсы сохранены, а новый игрок не подменяется PlayerCharacter.
- **Blast radius:** общий контракт World-Scenario
- **Один срез или решение владельца:** Сокращение только authored JSON без смены schema может быть одним срезом. Удаление/переопределение initial_state из versioned schema уже требует решения владельца, согласования authoring skills и WorldPack loader/validator; такой вариант не помещается в один срез и не выбран аудитом.

### B-D11 — Удаление: root aliases, четыре GM prompt copies, SillyTavern export

Текущий вердикт: **закрыто в source**. Конкретное наблюдение: E3: прежние presets/{book,action,strategic}, root aliases/export отсутствуют; три scenario-experience имеют разных владельцев и содержимое.

- **Симптом и цена:** Пользователь production получает прежний RP-контур; наличие удаления/замены в main ещё не является его новым игровым поведением.
- **Причина и механизм:** Дефект нового алгоритма этим вердиктом не установлен. Граница запуска: `G/app/main.py:80`, `roles/apps/templates/rp-stack.compose.yml.j2:55`, `roles/apps/templates/rp-stack.env.j2:6`. Production image содержит main.py от 2ad61019, тогда как проверенный acceptance app соответствует c2e2653 (E10).
- **Сохраняемые инварианты:** Раздельные source/apply/activation/live; чистая RP DB; сохранность legacy SQLite/state/backups; без compatibility и переноса старых Parties.
- **Проверка реального исхода:** После отдельно разрешённой поставки: сопоставить image и исходники; прочитать схему и Parties новой production DB mode=ro; пройти относящийся к этой строке API/GUI путь и связать его с committed turn/request. Для K01 отдельно требуется evidence внешнего Awareness gate.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Изменение исходников по этой строке не обосновано; нужна отдельная поставка и приёмка по решению владельца. Категория radius относится к границе запуска, не к разрешению менять весь продукт.

### B-D12 — Удаление: exact-prompt snapshots и тесты внутренних процедур

Текущий вердикт: **не закрыто**. Конкретное наблюдение: E9: пять русских exact prompt phrases, одна английская фраза и GUI source-string assertions; полного golden hash нет.

- **Симптом и цена:** Переформулировка инструкции или эквивалентная реализация интерфейса ломает test, хотя игрок не получает ухудшения. Одновременно GUI test не проверяет реальный пользовательский путь.
- **Причина и механизм:** `G/tests/test_rp_narrator_memory.py:635–647` фиксирует пять точных фраз; `G/tests/test_rp_provider.py:447–453` — английскую prose instruction; `U/rp-clean-flow.test.js:9–44` — endpoint/identifier/source substrings (полная классификация E9).
- **Сохраняемые инварианты:** Сохранить риск потери/смешения данных, atomicity, idempotency, provider/storage contracts. Не удалять относительное равенство hash, защищающее cache, вместе с запрещёнными golden prose assertions.
- **Проверка реального исхода:** Проверить фактический browser exchange и сохранённый request/turn, а не текст app.js; prose — человеческая живая оценка; memory/cache — saved prompts и usage. Удаление source-string gate отдельно подтверждается diff и CI inventory, не БД.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Локальные удаления assertions укладываются в срез; полный пересмотр UI-приёмки и бюджета не следует автоматически включать.

### B-D13 — Удаление: revision matrix, retired-world fixtures

Текущий вердикт: **закрыто в source**. Конкретное наблюдение: E2/E9: восемь текущих тестовых файлов; revision-named матрица/fixtures снятых миров отсутствуют.

- **Симптом и цена:** Пользователь production получает прежний RP-контур; наличие удаления/замены в main ещё не является его новым игровым поведением.
- **Причина и механизм:** Дефект нового алгоритма этим вердиктом не установлен. Граница запуска: `G/app/main.py:80`, `roles/apps/templates/rp-stack.compose.yml.j2:55`, `roles/apps/templates/rp-stack.env.j2:6`. Production image содержит main.py от 2ad61019, тогда как проверенный acceptance app соответствует c2e2653 (E10).
- **Сохраняемые инварианты:** Раздельные source/apply/activation/live; чистая RP DB; сохранность legacy SQLite/state/backups; без compatibility и переноса старых Parties.
- **Проверка реального исхода:** После отдельно разрешённой поставки: сопоставить image и исходники; прочитать схему и Parties новой production DB mode=ro; пройти относящийся к этой строке API/GUI путь и связать его с committed turn/request. Для K01 отдельно требуется evidence внешнего Awareness gate.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Изменение исходников по этой строке не обосновано; нужна отдельная поставка и приёмка по решению владельца. Категория radius относится к границе запуска, не к разрешению менять весь продукт.

### B-D14 — Удаление: 38 startup migrations

Текущий вердикт: **закрыто в source**. Конкретное наблюдение: E2: clean schema bootstrap с user_version/application_id, а не цепочка старых миграций; legacy DB не мигрируется.

- **Симптом и цена:** Пользователь production получает прежний RP-контур; наличие удаления/замены в main ещё не является его новым игровым поведением.
- **Причина и механизм:** Дефект нового алгоритма этим вердиктом не установлен. Граница запуска: `G/app/main.py:80`, `roles/apps/templates/rp-stack.compose.yml.j2:55`, `roles/apps/templates/rp-stack.env.j2:6`. Production image содержит main.py от 2ad61019, тогда как проверенный acceptance app соответствует c2e2653 (E10).
- **Сохраняемые инварианты:** Раздельные source/apply/activation/live; чистая RP DB; сохранность legacy SQLite/state/backups; без compatibility и переноса старых Parties.
- **Проверка реального исхода:** После отдельно разрешённой поставки: сопоставить image и исходники; прочитать схему и Parties новой production DB mode=ro; пройти относящийся к этой строке API/GUI путь и связать его с committed turn/request. Для K01 отдельно требуется evidence внешнего Awareness gate.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Изменение исходников по этой строке не обосновано; нужна отдельная поставка и приёмка по решению владельца. Категория radius относится к границе запуска, не к разрешению менять весь продукт.

### B-D15 — Удаление: mock:// в девяти production modules

Текущий вердикт: **закрыто в source**. Конкретное наблюдение: E2: активного mock:// и прежних девяти модулей нет; test doubles остаются в tests.

- **Симптом и цена:** Пользователь production получает прежний RP-контур; наличие удаления/замены в main ещё не является его новым игровым поведением.
- **Причина и механизм:** Дефект нового алгоритма этим вердиктом не установлен. Граница запуска: `G/app/main.py:80`, `roles/apps/templates/rp-stack.compose.yml.j2:55`, `roles/apps/templates/rp-stack.env.j2:6`. Production image содержит main.py от 2ad61019, тогда как проверенный acceptance app соответствует c2e2653 (E10).
- **Сохраняемые инварианты:** Раздельные source/apply/activation/live; чистая RP DB; сохранность legacy SQLite/state/backups; без compatibility и переноса старых Parties.
- **Проверка реального исхода:** После отдельно разрешённой поставки: сопоставить image и исходники; прочитать схему и Parties новой production DB mode=ro; пройти относящийся к этой строке API/GUI путь и связать его с committed turn/request. Для K01 отдельно требуется evidence внешнего Awareness gate.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Изменение исходников по этой строке не обосновано; нужна отдельная поставка и приёмка по решению владельца. Категория radius относится к границе запуска, не к разрешению менять весь продукт.

### B-P01 — Процесс 1: замена заканчивается удалением

Текущий вердикт: **не закрыто**. Конкретное наблюдение: E3/E9: остаются четыре полных seed и четыре осиротевшие WorldPack validator functions (658 LOC).

- **Симптом и цена:** Изменение одной границы требует лишних проверок; осиротевшие правила продолжают занимать проверочный бюджет. Автоматическая зелень скрывает отсутствие игровой приёмки.
- **Причина и механизм:** `scripts/validate-repository.py:38,162,381,498` сохраняет четыре старых WorldPack function, вызовы :1386–1389; текущих */manifest.json нет. `.github/workflows/ci.yml:4` запускает общий PR workflow, gateway :44 — полный pytest; paths boundary отсутствует.
- **Сохраняемые инварианты:** Не добавлять зеркальные validator/registry/gates; один loader/schema; не удалить общие security/deploy/Awareness проверки; существенные runtime/storage границы должны остаться защищены.
- **Проверка реального исхода:** Это процессная боль: БД сама не доказывает удаление gate. Нужны проверка реально исполненного CI job graph и повторный LOC inventory; затем авторитетная живая Party с неизменёнными commits, memory/cause/lore/proposal цепочками подтверждает, что сокращение проверок не заменило функциональность.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Validator cleanup локален, path-aware CI относится к уже выделенному общему треку. Совместить их в один исправительный срез нельзя считать обязательным; последовательность выбирает владелец.

### B-P02 — Процесс 2: legacy suite заморожена, clean без compatibility

Текущий вердикт: **закрыто в source**. Конкретное наблюдение: E2/E9: текущий clean путь не имеет revision flag, legacy test matrix удалена. Это состояние базы аудита, не аудит каждого исторического PR.

- **Симптом и цена:** Пользователь production получает прежний RP-контур; наличие удаления/замены в main ещё не является его новым игровым поведением.
- **Причина и механизм:** Дефект нового алгоритма этим вердиктом не установлен. Граница запуска: `G/app/main.py:80`, `roles/apps/templates/rp-stack.compose.yml.j2:55`, `roles/apps/templates/rp-stack.env.j2:6`. Production image содержит main.py от 2ad61019, тогда как проверенный acceptance app соответствует c2e2653 (E10).
- **Сохраняемые инварианты:** Раздельные source/apply/activation/live; чистая RP DB; сохранность legacy SQLite/state/backups; без compatibility и переноса старых Parties.
- **Проверка реального исхода:** После отдельно разрешённой поставки: сопоставить image и исходники; прочитать схему и Parties новой production DB mode=ro; пройти относящийся к этой строке API/GUI путь и связать его с committed turn/request. Для K01 отдельно требуется evidence внешнего Awareness gate.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Изменение исходников по этой строке не обосновано; нужна отдельная поставка и приёмка по решению владельца. Категория radius относится к границе запуска, не к разрешению менять весь продукт.

### B-P03 — Процесс 3: автоматическая проверка только по риску

Текущий вердикт: **не закрыто**. Конкретное наблюдение: E9: перечислены проверки prose и наличия строк, не проверяющие игровой исход.

- **Симптом и цена:** Переформулировка инструкции или эквивалентная реализация интерфейса ломает test, хотя игрок не получает ухудшения. Одновременно GUI test не проверяет реальный пользовательский путь.
- **Причина и механизм:** `G/tests/test_rp_narrator_memory.py:635–647` фиксирует пять точных фраз; `G/tests/test_rp_provider.py:447–453` — английскую prose instruction; `U/rp-clean-flow.test.js:9–44` — endpoint/identifier/source substrings (полная классификация E9).
- **Сохраняемые инварианты:** Сохранить риск потери/смешения данных, atomicity, idempotency, provider/storage contracts. Не удалять относительное равенство hash, защищающее cache, вместе с запрещёнными golden prose assertions.
- **Проверка реального исхода:** Проверить фактический browser exchange и сохранённый request/turn, а не текст app.js; prose — человеческая живая оценка; memory/cache — saved prompts и usage. Удаление source-string gate отдельно подтверждается diff и CI inventory, не БД.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Локальные удаления assertions укладываются в срез; полный пересмотр UI-приёмки и бюджета не следует автоматически включать.

### B-P04 — Процесс 4: 5000 LOC / 60s CI / 30s focused / без второго образа

Текущий вердикт: **не закрыто**. Конкретное наблюдение: E9: scripts/ci.ps1 печатает 5028/28, полный охват даёт 6479/1479; local 8.8s, GitHub full 5.084s; второго образа в workflow нет.

- **Симптом и цена:** Оператор видит debt 28, хотя автоматически исполняемый RP-контур превышает лимит как минимум на 732, а по правилу mixed files — на 1479 LOC.
- **Причина и механизм:** `scripts/ci.ps1:12–31` учитывает восемь Gateway tests (5028), но отдельные `U/rp-clean-flow.test.js:9–44` (46 физических строк файла) и `scripts/validate-repository.py:38–1401` (1405 физических строк файла) не входят в напечатанную цифру. Полный расчёт и исключения — E9; это измерение, новый validator не требуется.
- **Сохраняемые инварианты:** 5000 физических строк, shared files до разделения считаются RP; исключения только оговорённые Decision; 60s full/30s focused; не превращать показатель в blocking LOC gate.
- **Проверка реального исхода:** Повторить инвентарь реально запускаемых файлов и фактическое время GitHub full run. Живая БД не измеряет LOC: при сокращении требуется отдельно сохранить игровые цепочки и данные в реальной партии.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Честный показатель локален ci.ps1; устранение самого долга затрагивает несколько существующих границ и требует решения владельца об объёме, без нового слоя контроля.

### B-P05 — Процесс 5: один executable owner World/Scenario

Текущий вердикт: **закрыто в source**. Конкретное наблюдение: E3/E9: новый world.json/rp-world.v1 не описан в repository validator; старые manifest.json ветки осиротели, но не валидируют новый формат.

- **Симптом и цена:** Source owner нового формата определён, но его применение в production и отсутствие ложных legacy ограничений на реальной авторской сборке ещё не предъявлены.
- **Причина и механизм:** `G/app/rp/content.py:56` / :85 и materializers :231–343 — исполняемый владелец; `scripts/validate-repository.py:38–699` содержит только старые manifest contracts и не описывает world.json. Это закрытие в source, не найденный второй validator нового формата.
- **Сохраняемые инварианты:** Не переносить World-specific markers в repository validator; authoring docs меняются только при внешнем контракте; immutable World/Scenario snapshots.
- **Проверка реального исхода:** На отдельно разрешённой сборке/партии прочитать оба snapshots/hash и фактический prompt; подтвердить, что production loader принял новый source и не активировал старую manifest ветку.
- **Blast radius:** общий контракт World-Scenario
- **Один срез или решение владельца:** Исправление формата не обосновано. Для live подтверждения достаточно одного ограниченного flow; смена schema не предлагается.

### B-P06 — Процесс 6: gate соответствует этапу, focused ordinary PR

Текущий вердикт: **не закрыто**. Конкретное наблюдение: E9: все четыре jobs включаются для каждого PR независимо от paths; полный Gateway и Docker build также для doc-only.

- **Симптом и цена:** Изменение одной границы требует лишних проверок; осиротевшие правила продолжают занимать проверочный бюджет. Автоматическая зелень скрывает отсутствие игровой приёмки.
- **Причина и механизм:** `scripts/validate-repository.py:38,162,381,498` сохраняет четыре старых WorldPack function, вызовы :1386–1389; текущих */manifest.json нет. `.github/workflows/ci.yml:4` запускает общий PR workflow, gateway :44 — полный pytest; paths boundary отсутствует.
- **Сохраняемые инварианты:** Не добавлять зеркальные validator/registry/gates; один loader/schema; не удалить общие security/deploy/Awareness проверки; существенные runtime/storage границы должны остаться защищены.
- **Проверка реального исхода:** Это процессная боль: БД сама не доказывает удаление gate. Нужны проверка реально исполненного CI job graph и повторный LOC inventory; затем авторитетная живая Party с неизменёнными commits, memory/cause/lore/proposal цепочками подтверждает, что сокращение проверок не заменило функциональность.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Validator cleanup локален, path-aware CI относится к уже выделенному общему треку. Совместить их в один исправительный срез нельзя считать обязательным; последовательность выбирает владелец.

### B-K01 — Критерий 1: один World API, чистая RP DB, отдельный Awareness

Текущий вердикт: **закрыто в source**. Конкретное наблюдение: E3/E10: acceptance schema 8 содержит только clean Parties; production rp_engine.db отсутствует. Внешняя Awareness-приёмка отдельно не повторялась.

- **Симптом и цена:** Пользователь production получает прежний RP-контур; наличие удаления/замены в main ещё не является его новым игровым поведением.
- **Причина и механизм:** Дефект нового алгоритма этим вердиктом не установлен. Граница запуска: `G/app/main.py:80`, `roles/apps/templates/rp-stack.compose.yml.j2:55`, `roles/apps/templates/rp-stack.env.j2:6`. Production image содержит main.py от 2ad61019, тогда как проверенный acceptance app соответствует c2e2653 (E10).
- **Сохраняемые инварианты:** Раздельные source/apply/activation/live; чистая RP DB; сохранность legacy SQLite/state/backups; без compatibility и переноса старых Parties.
- **Проверка реального исхода:** После отдельно разрешённой поставки: сопоставить image и исходники; прочитать схему и Parties новой production DB mode=ro; пройти относящийся к этой строке API/GUI путь и связать его с committed turn/request. Для K01 отдельно требуется evidence внешнего Awareness gate.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Изменение исходников по этой строке не обосновано; нужна отдельная поставка и приёмка по решению владельца. Категория radius относится к границе запуска, не к разрешению менять весь продукт.

### B-K02 — Критерий 2: preset и принципиально иной free Scenario

Текущий вердикт: **не доказано**. Конкретное наблюдение: E3/E10: текущие три Parties — presets; сохранённого free flow с наблюдением различий нет.

- **Симптом и цена:** Нет доказательства, что свободный wizard даёт самостоятельную игру, а не ещё один preset с переименованным заголовком.
- **Причина и механизм:** `G/app/main.py:620` передаёт free Scenario в `G/app/rp/content.py:310–343`; текущая DB содержит только preset_id. Причина вердикта — отсутствующий наблюдённый обмен, не найденная поломка materializer.
- **Сохраняемые инварианты:** Одна materialization boundary для preset/free; игрок/старт/стиль принадлежат Scenario; общий World hash; без source World mutation.
- **Проверка реального исхода:** После разрешённого создания сравнить preset/free DB snapshots, scenario hashes, player/opening/initial state и первый provider prompt/turn; вручную подтвердить принципиальное различие игры.
- **Blast radius:** общий контракт World-Scenario
- **Один срез или решение владельца:** Нужен один приёмочный flow, предварительная реализация не обоснована.

### B-K03 — Критерий 3: человек читает 12 blind A/B, строгий prose verdict

Текущий вердикт: **не доказано**. Конкретное наблюдение: E11: human A/B принят (8/1/3), narrator.py неизменен; residual 10–12 остаются, строгий итог без нарушений по всем 12 не подтверждён этим аудитом.

- **Симптом и цена:** Предпочтение A/B не доказывает отсутствие всех запрещённых prose исходов; остаточные recap, пассивность боя и неясный адресат могут ухудшать агентность.
- **Причина и механизм:** `G/app/rp/narrator.py:125–156` задаёт универсальные правила; `provider.py:74` вызывает того же narrator. E11 фиксирует принятый человеком кандидат и residual pairs 10–12, но не заменяет строгую полную повторную оценку. Изменения narrator.py после принятого 89bcc7f отсутствуют.
- **Сохраняемые инварианты:** Не чинить прозу новым validator/repair/substring фильтром; сохранить модель/параметры/12 anchors для сопоставимого blind A/B; RAW не менять.
- **Проверка реального исхода:** Человек оценивает сохранённые реальные prompt/response пары всех 12 anchors по агентности, причинности, voice, pace, style, leakage; связывает с exact source/model/settings. Приёмочный вердикт принадлежит человеку.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Нужна оценка владельца/читателя; заранее выбранное переписывание prompt не обосновано.

### B-K04 — Критерий 4: ручные первые 20 ходов + контрастный второй старт

Текущий вердикт: **не доказано**. Конкретное наблюдение: E5/E11: ручная выборка причин не равна полной 20-turn continuity приёмке; контрастный второй живой старт не предъявлен.

- **Симптом и цена:** По выборочным сценам нельзя утверждать, что 20 ходов сохраняют причинность и различают второй сценарий.
- **Причина и механизм:** `G/app/rp/narrator.py:360` собирает Party историю и calls; E5 проверяет выбранные причины, а не каждую continuity связь 20-turn трассы. Причина пробела — объём наблюдения, не установленный алгоритмический дефект.
- **Сохраняемые инварианты:** Человеческая приёмка прозы; не засевать memory/jobs, не создавать semantic gate; фиксировать model/settings/source.
- **Проверка реального исхода:** Полностью прочитать первые 20 player/scene пар живой Party и короткий контрастный второй старт; сверить события и знания с авторитетными snapshots/RAW/производными фактами.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Нужна отдельная человеческая приёмка, не новый код и не новый ADR.

### B-K05 — Критерий 5: настоящая W+2A партия, RAW/budget/coverage/cache

Текущий вердикт: **не закрыто**. Конкретное наблюдение: E6/E7: W=50 A=8; две актуальные настоящие Parties v60; новая memory застыла на17 и дала13truncation failures. Второй anchor66 не достигнут.

- **Симптом и цена:** Служебное покрытие останавливается, дорогие повторные полные ответы заканчиваются обрезанием, последовательный worker задерживает также relations/lore. Игрок продолжает ходить, но играет с устаревшими производными данными.
- **Причина и механизм:** `G/app/rp/provider.py:346` собирает полную структурную память; :405–413 даёт max_tokens=16384; `provider.py:632` отвергает length. `runner.py:91–137` обслуживает общий Atomic worker, terminal rejection не обновляет coverage. На активном v55:10length, safe17, lag22; итоговый v60:13truncation и24pending каждого типа после внешнего отключения роли (E6), не зависание worker.
- **Сохраняемые инварианты:** Не обрезать uncovered RAW; safe coverage=min sections, monotonic; immutable snapshots/RAW; exact provider, без fallback; bounded non-RAW и hard input. HTTP200 не считать семантическим успехом.
- **Проверка реального исхода:** После отдельного разрешённого изменения прочитать живую настоящую партию до W+2A и после дренирования очереди: section coverage, actual payload/finish_reason, failed/succeeded/pending, длительности, source_version lag; сопоставить каждый RAW и оба anchor shifts. Не засевать snapshots/jobs и не выдавать seeded run за настоящую партию.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Первый объект расследования локален provider memory boundary; изменение модели, формы памяти либо политики общей очереди — решение владельца. Данные не доказывают, что одного повышения лимита достаточно.

### B-K06 — Критерий 6: все три цепочки §6 целиком

Текущий вердикт: **не закрыто**. Конкретное наблюдение: E5: одна runtime-Lore цепочка предъявлена; relationship causal quality не закрыта, admin chain не доказана.

- **Симптом и цена:** Нельзя принять сохраняемые механики суммарно: верная Lore-доставка не компенсирует неверного владельца relationship и отсутствие admin применения.
- **Причина и механизм:** `G/app/rp/mechanics.py:293–310` / `G/app/rp/turn_engine.py:2308` отвечают за два незакрытых края; narrator projection :232–262 доставляет только существующие производные данные. Причинные детали — E5, B-G01 и B-G03.
- **Сохраняемые инварианты:** Каждая цепочка предъявляется отдельно; provenance/owner/allowlist/idempotency; ни одна роль не переписывает RAW/World.
- **Проверка реального исхода:** Три независимых живых обмена с join RAW→job→result→projection→prompt и ручным последующим исходом; для admin также UI decision и unchanged gameplay version.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Это агрегат нескольких границ; одно исправление без решения владельца не обещается.

### B-K07 — Критерий 7: provider fail без commit, поле/retry, Atomic off не блокирует

Текущий вердикт: **не проверено на живом**. Конкретное наблюдение: E4: DB retry доказан; DOM сохранения поля и Atomic-disabled режим в текущем живом контуре не переключались.

- **Симптом и цена:** DB защищена от двойного turn, но сохранность ввода в браузере и продолжение игры с выключенным Atomic в текущем UI не доказаны.
- **Причина и механизм:** `G/app/rp/narrator.py:437–469` отделяет commit/failure и early return при disabled; `U/app.js` message/retry flow сохраняет pending operation. E4 доказывает same-key retry по БД, не состояние DOM.
- **Сохраняемые инварианты:** Не терять player text; не коммитить error/fallback; retry с тем же key только один turn; выключенная служебная роль не блокирует narrator.
- **Проверка реального исхода:** В разрешённом живом UI failure/retry проверить поле до/после, request key, versions/count turns и единственный successful commit; отдельно измерить ход при Atomic disabled и отсутствие ожидания.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Сначала отдельная приёмка; изменения продукта не обоснованы. Для переключения role требуется отдельное разрешение, не этот read-only аудит.

### B-K08 — Критерий 8: restart с непустой очередью без потерь/роста attempts

Текущий вердикт: **не проверено на живом**. Конкретное наблюдение: E4: owner runner/source и существующие tests проходят; снимков до/после restart текущего образа нет.

- **Симптом и цена:** Текущий образ не прошёл наблюдаемую проверку рестарта с непустой очередью; source correctness не доказывает recovery после остановки реального процесса.
- **Причина и механизм:** `G/app/rp/runner.py:64–89` владеет recover/start/cancel/await; :113–115/:161–163 освобождает claim; `turn_engine.py:1092–1122` восстанавливает status без attempts. Нет связанного before/after process timeline текущего образа.
- **Сохраняемые инварианты:** Attempts только за фактический failure; at-most-once persistence, отсутствие lost jobs; отдельные handlers; без queue-platform.
- **Проверка реального исхода:** После отдельного разрешения зафиксировать DB pending/running/attempts и container start; выполнить restart; связать recovered job IDs, provider failures, attempts и ровно один persisted result каждого job.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Один recovery experiment, не предварительная реализация. Рестарт запрещён рамками этого аудита.

### B-K09 — Критерий 9: World не изменён, Party hash переживает update World

Текущий вердикт: **не доказано**. Конкретное наблюдение: E3: stored pairs/hash согласованы, immutable triggers есть; события обновления source World между двумя DB наблюдениями нет.

- **Симптом и цена:** Согласованные snapshots не доказывают, что Party пережила реальное обновление исходного World. Ошибочный вывод мог бы скрыть утечку живого source в начатую игру.
- **Причина и механизм:** `G/app/rp/schema.py:371–380` защищает Party snapshots/binding; `content.py:231` materializes World, narrator читает stored snapshot. Наблюдений source update между двумя DB снимками нет.
- **Сохраняемые инварианты:** Две независимые пары; RAW и World не переписываются runtime ролями; старые Parties не мигрировать.
- **Проверка реального исхода:** Сохранить Party world_hash/snapshot, отдельно разрешённо обновить источник, снова прочитать Party и следующий prompt; новая Party получает новый hash, старая — прежний. Проверить сохранность source от runtime ролей.
- **Blast radius:** общий контракт World-Scenario
- **Один срез или решение владельца:** Один ограниченный acceptance experiment после решения владельца; schema change не обоснован.

### B-K10 — Критерий 10: модель/status/success/error/last_error/kill switch трёх ролей

Текущий вердикт: **не закрыто**. Конкретное наблюдение: E8: Atomic status ошибочно сообщает local Gemma; GUI source не выводит success_count/error_count.

- **Симптом и цена:** Игроку/оператору показывается неверная модель Atomic, а чисел успехов/ошибок в карточке роли нет; completed calls могут скрывать failed mechanics.
- **Причина и механизм:** `G/app/main.py:1157–1162` собирает Atomic status как local/settings.local_llm_model_alias; реальный `provider.py:109–111` фиксирует OpenRouter DeepSeek. `main.py:359–373` отдаёт counters, `U/app.js:183–187` их игнорирует.
- **Сохраняемые инварианты:** Одна существующая status surface, никаких новых telemetry services; модель должна отражать фактическую роль; не путать HTTP и job outcome; сохранить kill switches.
- **Проверка реального исхода:** На реальной Party сопоставить GET supervisor payload, rendered GUI и service_call_log provider/model; вызвать наблюдённые success/failure и проверить counters/last_error/disabled. Секреты и prompts в status не раскрывать.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Два небольших существующих API/UI места допускают один функциональный срез; новая абстракция не нужна. Категория означает локальную поверхность наблюдаемости, без World/schema изменения.

### B-L03 — §4–5: local_overrides читается при исполнении

Текущий вердикт: **закрыто в source**. Конкретное наблюдение: E3: 13 мест поля; narrator, Lore API, budget validation, draft context используют scenario lore; гипотеза «только хранится» неверна.

- **Симптом и цена:** Действующий source умеет Scenario Lore, но во всех трёх живых Parties этот список пуст; игрок не получил наблюдённого эффекта local override.
- **Причина и механизм:** `G/app/rp/narrator.py:232–239`, `main.py:862`, `mechanics.py:331,365`, `provider.py:284` действительно читают поле; `content.py:75–83` ограничивает override typed lore cards. Это не storage-only заглушка.
- **Сохраняемые инварианты:** Origin world/scenario/runtime; локальное отклонение не пишет обратно World; immutable Scenario; bounded Lore prompt.
- **Проверка реального исхода:** Создать после отдельного разрешения Scenario с отличимым lore override; проверить stored Scenario, Lore API origin=scenario и сохранённый следующий narrator prompt/сцену при неизменном World hash.
- **Blast radius:** общий контракт World-Scenario
- **Один срез или решение владельца:** Один acceptance flow. Расширять local_overrides произвольными полями без нового use case не обосновано.

### B-L04 — §4: Lore origin world/scenario/runtime и Scenario → prompt

Текущий вердикт: **закрыто в source**. Конкретное наблюдение: E3: три origin поддержаны; в текущих Parties scenario cards=0, поэтому live Scenario → prompt не доказан.

- **Симптом и цена:** Действующий source умеет Scenario Lore, но во всех трёх живых Parties этот список пуст; игрок не получил наблюдённого эффекта local override.
- **Причина и механизм:** `G/app/rp/narrator.py:232–239`, `main.py:862`, `mechanics.py:331,365`, `provider.py:284` действительно читают поле; `content.py:75–83` ограничивает override typed lore cards. Это не storage-only заглушка.
- **Сохраняемые инварианты:** Origin world/scenario/runtime; локальное отклонение не пишет обратно World; immutable Scenario; bounded Lore prompt.
- **Проверка реального исхода:** Создать после отдельного разрешения Scenario с отличимым lore override; проверить stored Scenario, Lore API origin=scenario и сохранённый следующий narrator prompt/сцену при неизменном World hash.
- **Blast radius:** общий контракт World-Scenario
- **Один срез или решение владельца:** Один acceptance flow. Расширять local_overrides произвольными полями без нового use case не обосновано.

### B-L05 — §4: независимое принятие relationship candidates

Текущий вердикт: **не закрыто**. Конкретное наблюдение: E5: domain-invalid candidate изолируется в loop 604–685; shape-invalid candidate отвергает весь strict result ещё до loop.

- **Симптом и цена:** Один malformed element лишает корректного соседа применения, несмотря на независимый loop для доменных отказов.
- **Причина и механизм:** `G/app/rp/mechanics.py:46–54` типизирует весь tuple; :299 и `provider.py:632` валидируют пакет до :604. Pure JSON probe: один valid принимается, valid+missing direction даёт ValidationError candidates[1].direction и не доходит до loop.
- **Сохраняемые инварианты:** Strict внешняя schema и независимость кандидатов должны иметь явную границу; нельзя принимать неизвестные ID/spans, править RAW или превращать parser в semantic regex.
- **Проверка реального исхода:** Наблюдённый реальный ответ с одним malformed и одним grounded valid candidate: прочитать rejected reason отдельно и ровно один applied cause/next prompt для valid. Если strict provider не допускает такой ответ, владелец должен явно определить область обещания.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Возможен один boundary срез, но согласование строгого envelope и обещания независимости требует решения владельца; изменение World/Scenario schema не доказано необходимым.

### B-L06 — §3: атомарный pending → running с status в UPDATE

Текущий вердикт: **закрыто в source**. Конкретное наблюдение: E4: оба claim выполняются BEGIN IMMEDIATE + outer AND status='pending'; локальная конкурентная проверка существует.

- **Симптом и цена:** Source имеет атомарный SQL, но текущая live очередь не наблюдалась под двумя конкурирующими claimers.
- **Причина и механизм:** `G/app/rp/turn_engine.py:1140–1155` и :1176–1191 используют BEGIN IMMEDIATE и status predicate в outer UPDATE. Однострочный grep не находит многострочный SQL; локальная проверка подтверждает boundary.
- **Сохраняемые инварианты:** Один владелец claim, attempts не растёт при захвате; separate admin queue; no lost/duplicate persisted results.
- **Проверка реального исхода:** При отдельно разрешённой нагрузке прочитать IDs/status/attempts и process timeline двух workers; каждый job должен получить один результат и failure-based attempts.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Сначала ограниченная live проверка; менять SQL без найденного дефекта не требуется.

### B-L07 — §3: startup/shutdown/cancel/await у runner

Текущий вердикт: **закрыто в source**. Конкретное наблюдение: E4: runner.py:64–89, отдельные worker tasks и CancelledError release.

- **Симптом и цена:** Текущий образ не прошёл наблюдаемую проверку рестарта с непустой очередью; source correctness не доказывает recovery после остановки реального процесса.
- **Причина и механизм:** `G/app/rp/runner.py:64–89` владеет recover/start/cancel/await; :113–115/:161–163 освобождает claim; `turn_engine.py:1092–1122` восстанавливает status без attempts. Нет связанного before/after process timeline текущего образа.
- **Сохраняемые инварианты:** Attempts только за фактический failure; at-most-once persistence, отсутствие lost jobs; отдельные handlers; без queue-platform.
- **Проверка реального исхода:** После отдельного разрешения зафиксировать DB pending/running/attempts и container start; выполнить restart; связать recovered job IDs, provider failures, attempts и ровно один persisted result каждого job.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Один recovery experiment, не предварительная реализация. Рестарт запрещён рамками этого аудита.

### B-L09 — §2/§5: suggest accept/reject и безопасный PlayerCorrection

Текущий вердикт: **закрыто в source**. Конкретное наблюдение: E4/E8: owner-scoped API/GUI, allowlist, immutable target/before, versioned guidance; текущие proposal/correction таблицы пусты.

- **Симптом и цена:** Администратор наблюдается как роль и отвечает, но без сохранённого proposal и ручного decision его полезное воздействие на партию не доказано.
- **Причина и механизм:** `G/app/main.py:1174–1209` экспонирует list/decision; `turn_engine.py:2308–2435` делает reject либо новую guidance revision; `narrator.py:247–262` проецирует guidance; GUI buttons `U/app.js:195–199`. В DB 0 proposals/guidance/corrections.
- **Сохраняемые инварианты:** Suggest по умолчанию, owner check, allowlist/expiry/before/idempotency; revision guidance отдельно от gameplay current_version; RAW/World immutable; classifier не открывает correction.
- **Проверка реального исхода:** На живом RAW получить proposal с provenance, вручную reject один и accept другой; прочитать decision, новую guidance revision, unchanged Party version и следующий saved prompt. Для PlayerCorrection отдельно подтвердить explicit initiation и immutable target.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Нужен один приёмочный обмен; если модель не предлагает полезного, это отдельный model-boundary вопрос владельца, а не повод засевать proposal.

### B-F01 — Удалённый flag: внешние docs/skills не должны обещать переключатель

Текущий вердикт: **не закрыто**. Конкретное наблюдение: E1: 11 flag hits в семи внешних поверхностях описывают удалённый source/IaC механизм.

- **Симптом и цена:** Автор и оператор получают неверное обещание выбора clean/legacy через флаг; особенно опасно считать, что следующий apply остаётся на legacy только из-за false.
- **Причина и механизм:** `G/app/main.py:80` больше не выбирает старую ветку; `roles/apps/templates/rp-stack.env.j2:6` содержит только clean RP DB binding, флаг удалён. Полный перечень неверных мест E1.
- **Сохраняемые инварианты:** Документировать только внешний контракт, не добавлять зеркало правил/новый gate; не смешивать текущий source с фактически старым production образом.
- **Проверка реального исхода:** Сам текст документа проверяется по source и фактическому config/API после разрешённой поставки; live DB/schema и image должны показывать именно описанный механизм. Отсутствие мутаций старой DB проверять отдельно.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Один документационный срез на существующих внешних поверхностях возможен; обновление Delivery-статуса Decision отдельно решает владелец.

### B-F03 — Provider: immutable exact Party binding/BYOK endpoint/no fallback

Текущий вердикт: **закрыто в source**. Конкретное наблюдение: E3/E8: stored exact binding и immutable trigger, preflight route/BYOK checks; ошибочный endpoint живым запросом не посылался.

- **Симптом и цена:** Exact binding защищён source, но отказ BYOK для чужого endpoint в текущем живом пользовательском обмене не наблюдался.
- **Причина и механизм:** `G/app/main.py:303–310` валидирует binding, :673–700 ограничивает BYOK endpoint; `G/app/rp/schema.py:371–380` делает профиль/provider/base/model/settings immutable; `provider.py:524–535,617–629` закрывает route/fallback.
- **Сохраняемые инварианты:** Ключ только exact endpoint; не наследовать retired provider/fallback; secrets не печатать; сохранённые данные не мигрировать.
- **Проверка реального исхода:** В разрешённом live flow проверить сохранённый exact tuple и отказ несовместимого endpoint до provider call/DB write, затем корректный вызов с тем же binding; только scalar доказательства без ключа.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Отдельная проверка boundary; новая реализация или смена public API не требуется по текущим данным.

### B-F04 — Atomic route replacement решает длинную очередь

Текущий вердикт: **не закрыто**. Конкретное наблюдение: E6: relationships/lore быстрее, memory13/36завершённых jobs failed;24pending каждого типа после внешнего отключения Atomic. HTTP200 не равен succeeded.

- **Симптом и цена:** Служебное покрытие останавливается, дорогие повторные полные ответы заканчиваются обрезанием, последовательный worker задерживает также relations/lore. Игрок продолжает ходить, но играет с устаревшими производными данными.
- **Причина и механизм:** `G/app/rp/provider.py:346` собирает полную структурную память; :405–413 даёт max_tokens=16384; `provider.py:632` отвергает length. `runner.py:91–137` обслуживает общий Atomic worker, terminal rejection не обновляет coverage. На активном v55:10length, safe17, lag22; итоговый v60:13truncation и24pending каждого типа после внешнего отключения роли (E6), не зависание worker.
- **Сохраняемые инварианты:** Не обрезать uncovered RAW; safe coverage=min sections, monotonic; immutable snapshots/RAW; exact provider, без fallback; bounded non-RAW и hard input. HTTP200 не считать семантическим успехом.
- **Проверка реального исхода:** После отдельного разрешённого изменения прочитать живую настоящую партию до W+2A и после дренирования очереди: section coverage, actual payload/finish_reason, failed/succeeded/pending, длительности, source_version lag; сопоставить каждый RAW и оба anchor shifts. Не засевать snapshots/jobs и не выдавать seeded run за настоящую партию.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Первый объект расследования локален provider memory boundary; изменение модели, формы памяти либо политики общей очереди — решение владельца. Данные не доказывают, что одного повышения лимита достаточно.

### B-F05 — Runtime Lore сохраняет attribution/grounding и полезную релевантность

Текущий вердикт: **не закрыто**. Конкретное наблюдение: E5/E6: cards7/15 требуют фактов вне selected spans; newest-fit projection и задержка23/24→27; keyword budget failure source34.

- **Симптом и цена:** В Lore попадают утверждения шире выбранного доказательства, а задержка общей очереди лишает ближайшие сцены свежей карты; заполнение Lore recaps снижает место для полезных давних фактов.
- **Причина и механизм:** `G/app/rp/provider.py:192` просит grounded draft, но `mechanics.py` проверяет typed result/provenance, не истинность каждого утверждения; :791–807 выбирает самые новые помещающиеся runtime cards. E5 вручную показывает cards 7/15 и задержку 23/24. Отдельный source34 result превышает keyword budget `mechanics.py:114–115`, это реальный schema refusal.
- **Сохраняемые инварианты:** LLM разрешает смысл и релевантность; код не становится substring-судьёй; uncertainty/attribution/provenance сохраняются; bounded Lore и неизменный source World.
- **Проверка реального исхода:** Вручную сопоставить полный content каждой выбранной карты со всеми selected spans; затем saved prompt на релевантной следующей сцене и отсутствие неверных canon/identity claims. Измерить source-version lag и вытеснение релевантной старой карты.
- **Blast radius:** локально в одном модуле
- **Один срез или решение владельца:** Grounding/model boundary может быть локальным срезом. Новая retrieval система или новая schema не обоснованы этим аудитом; разделение latency и semantic quality решает владелец.

## Измерения и доказательства

### E1. Внешние контракты и удалённый flag

Результат поиска — 12 упоминаний RP_REBUILD_ENABLED: 11 в семи внешних поверхностях (шесть Wiki-файлов и authoring reference), одно в source-string GUI test. **Семь пользовательских/операционных поверхностей описывают неверный source-контракт выбора**; это число файлов-поверхностей, не семь независимых переключателей. В running production env false ещё существует, потому что образ старый; это не возвращает флаг в текущий source/IaC.

Полная классификация 19 hits расширенного docs/skills поиска:

| Файл:строка | Классификация | Что означает |
| --- | --- | --- |
| codex-skills/rp-world-pack-builder/references/rp-stack-paths.md:69 | описывает удалённый механизм | false как source gate |
| codex-skills/training-world-pack-builder/SKILL.md:14 | актуально | retention legacy data, не runtime compatibility |
| docs/wiki/01-architecture.md:63 | описывает удалённый механизм | flag выбирает движок |
| docs/wiki/01-architecture.md:68 | описывает удалённый механизм | Mermaid flag fork |
| docs/wiki/01-architecture.md:74 | описывает удалённый механизм | legacy Adjudicator branch |
| docs/wiki/01-architecture.md:86 | описывает удалённый механизм | условные legacy endpoints; retention-часть отдельно остаётся верной |
| docs/wiki/02-interfaces.md:20 | описывает удалённый механизм | clean API зависит от flag |
| docs/wiki/02-interfaces.md:45 | описывает удалённый механизм | старые handlers 410/filter описаны как действующие; в source удалены |
| docs/wiki/02-interfaces.md:151 | описывает удалённый механизм | legacy public Lore за false |
| docs/wiki/04-worldpacks-and-modes.md:6 | описывает удалённый механизм | active World за flag |
| docs/wiki/04-worldpacks-and-modes.md:54 | актуально | сохранение legacy SQLite/state |
| docs/wiki/06-models-and-providers.md:17 | описывает удалённый механизм | три роли условно за flag |
| docs/wiki/06-models-and-providers.md:365 | актуально | явно исторический legacy updater revisions 0..7; :368 отделяет новый Atomic |
| docs/wiki/08-data-and-security.md:293 | актуально | training не читает старую RP DB |
| docs/wiki/09-operations-and-repository.md:426 | описывает удалённый механизм | доставка flag IaC |
| docs/wiki/09-operations-and-repository.md:428 | описывает удалённый механизм | false в committed inventory якобы предотвращает активацию |
| docs/wiki/09-operations-and-repository.md:434 | актуально | требование отсутствия legacy RP в новом пути, не наличие compatibility |
| docs/wiki/README.md:12 | описывает удалённый механизм | clean behind flag |
| docs/wiki/README.md:208 | описывает удалённый механизм | wiring behind flag |

GUI `U/rp-clean-flow.test.js:29` действительно проверяет отсутствие флага, но способом, запрещённым процессным инвариантом 3 (E9). Файлы Wiki в этой таблице действительно находятся в root `docs/wiki`; они отделены от Decision evidence под `roles/apps/files/rp-stack/docs/decisions`.

### E2. Удалённый runtime и оставшиеся revision hits

Чистые `app/rp/*` не наследуют Adjudicator. Отсутствуют `adjudicator.py, party_store.py, intent_parser.py, narrative.py, services/memory.py, context_estimator.py, prompt_tools.py`, старый evals и RPSceneState/RuleEngine call path. Legacy memory/state/world/check/auto/gm_intent/route_required маршруты не зарегистрированы новым `G/app/main.py`. Каталог `/api/worldpacks` и его detail :522/:555 — законные оставшиеся routes. В `U/styles.css:4` последовательность d20 — часть цвета CSS, к D20 не относится.

Поиск contract_revision/CONTRACT_SOURCE_MAX_REVISION/numeric revision вернул **7 строк**, ни одна не является пиннингом gameplay-контракта: schema.py:117,135 — memory revision/base chain; :314 — PlayerCorrection revision; :335,357 — Administrator guidance revisions; tests/test_rp_mechanics.py:897,898 — проверка двух guidance revisions при одной gameplay version. Полный поиск revision в schema даёт 9 locations (приложение). `user_version=8`, `application_id=1380992334` и fail-closed schema bootstrap (:480–489) — версия SQLite schema, не возвращение ревизионных веток 0–11.

Во всех 12 таблицах актуальной clean БД отсутствует contract_revision; `PRAGMA quick_check=ok`, foreign_key_check возвращает 0 строк, 13 immutable/no-delete triggers. В production legacy таблице parties такой столбец сохраняется — это retention, не схема clean Party.

### E3. World / Scenario / Party и остатки контента

`G/app/rp/content.py:56–57` задаёт extra=forbid/frozen, WorldDefinition :85–109 не содержит Scenario-only полей. Pure probe в текущем acceptance контейнере независимо добавил player_role, openings, presets, state_seed, rp_supervisor — **пять ValidationError**. Существующий parametrized boundary test — `G/tests/test_rp_world_scenario.py:76–86`.

Production loader в проверочном образе материализует **один exact World day-watch-moscow-v2, 12 presets, четыре старта и три разных system текста стилей**. World hash:
`cb32e65c02ee59101b4270a6a350ce72061ca23a68ce55b2a7d8169d7e8d086e`.
Scenario action-independent:
`ec9c3b7b13a119c0cccb9e727971060abf6e9ac6f564517fa61489eebd41c5a4`;
action-night-trainee:
`7f001c5084230d4ff1db8b13a35f36075f094b6eba83a7cf6f5ebe04aefefb58`.

В каждой из трёх текущих Parties отдельно пересчитаны canonical SHA256 WorldSnapshot и ScenarioSnapshot — обе пары совпали с stored hashes. `schema.py:371–380` защищает snapshots и exact narrator binding. Это свидетельство независимой фиксации; **реального source World update между двумя наблюдениями Party не было**, поэтому K09 не закрыт.

local_overrides имеет 13 locations. API сериализует/материализует его (`main.py:580,620`), Lore API читает scenario cards (:862), content хранит/копирует (:125,166,302,320,343), request schema (:69), mechanics учитывает их при bounded Lore (:331,365), narrator читает и включает в Lore (:232–239), provider получает их как draft context (:284). `ScenarioLocalOverrides:75–83` — typed lore_cards, не произвольный patch World. Origin scenario материализуется formatter в `mechanics.py:846`; narrator не обязан содержать литерал origin. В актуальных Party scenario_lore_cards=0: **живого непустого Scenario Lore → prompt примера нет**.

Оставшиеся full seeds принадлежат **Scenario**, несмотря на каталог внутри W. Они не мёртвые файлы: `content.py:278` читает initial_state из preset references; все 12 presets :14 указывают на четыре `prompts/openings/{day-witch,independent,inquisition-observer,night-trainee}/state-seed.json`. Размеры 44232/44157/44018/44167 bytes. В stored Scenario остаются active_threads, characters, completed_threads, factions, last_turn, locations, meta, player, relationships, resources, timeline, uncertain_facts, world_constraints.

50 hits PlayerCharacter/state_seed/state-seed: 32 строки в четырёх full seeds; 12 references presets; четыре opening-scene.md:3; `rules/checks.md:31` и `world-info/index.md:11`. Последние два **World-owned** и читаются через world.json; первые четыре opening — Scenario-owned. PlayerCharacter обнаружен в World и Scenario snapshots всех трёх Parties. Пятый root seed и прежние root aliases/GM copies/SillyTavern export удалены. Удаление всех пяти full seeds поэтому не завершено.

### E4. Narrator failure/retry, forbidden_claims и runner

Narrator — один plain-text вызов `provider.py:74–95`, commit только после usable response `narrator.py:437–448`, failure claim :449–455. Truncation/empty response — transport/envelope пригодность, не семантическая оценка прозы. Fallback search даёт ровно три строки: narrator.py:117 docstring, provider.py:628 allow_fallbacks=false, provider_catalog.py:87 no-fallback tag.

Реальный сохранённый обмен на `party_f9490fa8937d`:
- request `codex-v60-party_f9490fa8937d-v46`, expected_version=45;
- call **105**, 2026-09-04T12:17:36Z, ConnectError / All connection attempts failed;
- call **112**, 2026-09-04T12:19:59Z, completed HTTP200, тот же request;
- в immutable turns ровно один request turn, committed_version=46; до failure существовали 45 turns;
- текущая request row id46 succeeded/turn_id46; player_text и idempotency key сохранены;
- SHA256 player_text `d6920eba4e2a4ef267f21f6ab820f61f82abae977533cc36409040af15cc276d`;
  SHA256 key `40ef15bbd59bebc58f471edb1ba11d1d9d82de42e99cde0c702d7434899d74cd`.

Request row перезаписала status failed после успеха; история ошибки подтверждается service_call_log, а не текущим last_error. Самостоятельного исторического снимка Party.current_version сразу после failure нет; неизменяемые turn rows и timestamps не содержат дополнительного failed commit. **DOM поля и клика Retry этим join не доказаны**. `narrator.py:464–469` пропускает ожидание при Atomic disabled; live flag в этом аудите не переключался.

forbidden_claims: main.py:280 — API projection; mechanics.py:140,151,160,174,176 — strict shape/length/uniqueness; :1053–1061 — только rule target и serialized patch; provider.py:323,325,326 — явная PlayerCorrection инструкция; schema.py:290,291 и turn_engine.py:253,1913,1927,2114,2949,2950,2991 — immutable proposal/overlay persistence/projection. После accept **весь overlay JSON** включается narrator.py:247–262: поле может влиять на сцену как инструкция модели, но не как substring predicate над ответом. Ни один classifier не инициирует correction самостоятельно.

Claims: `turn_engine.py:1140–1155` и :1176–1191 используют BEGIN IMMEDIATE, UPDATE с внешним `AND status='pending'` и RETURNING. Однострочный grep UPDATE...WHERE дал 0, потому что SQL многострочный; он был прочитан целиком. **Только два инкремента attempts**: :1250 failure Atomic, :1282 failure Administrator. Упоминания `attempts+1` :1252/:1284 выбирают terminal status того же failure UPDATE, не ещё один increment. Claim/recovery/release не увеличивают attempts.

Runner.py:64–74 — recover/start двух tasks, :76–89 — cancel+gather/await, :91–137 — Atomic handler, :139–185 — Administrator handler; CancelledError освобождает claim, generic provider failure retryable, RPModelOutputRejected terminal. Нет нового queue framework. E6 показывает failure-based attempts; **текущего restart с непустой очередью не выполнялось**.

### E5. Три причинные цепочки: ручная проверка

Идентификаторы, spans и характер участия проверены чтением полного выбранного RAW и тела конкретного model request. Поиск строк ниже используется только для доставки уже идентифицированного persisted cause/card в точный prompt, не как семантический классификатор.

Самые длинные обнаруженные archived clean Parties имеют v66. Для детального чтения выбрана последняя из них `party_a3a1c666c679`, `decision043-acceptance-run8/data` (schema7, старый образ). У неё 66 completed relationship calls, **3 candidates, 3 accepted, 3 inserted, 0 rejected, 3 non-seed causes**. Запрошенной выборки 5–10 в этой партии нет: прочитаны все три, без создания искусственных causes. Дополнительно прочитаны все три новых causes текущей `party_b887b6c8dfc2` — всего шесть ручных сопоставлений.

| Party / cause / source | Выбранные spans и ручной вывод | Последующий prompt / сцена |
| --- | --- | --- |
| a3… /85/v3, igor-teplov, kept_agreement +10 | span3 подтверждает будущее соглашение, ещё не его исполнение | cause присутствует в prompts v4/v5; событие исполнения из RAW не следует |
| a3… /86/v59, igor-teplov, kept_agreement +10 | spans2,3,5,7,9: Игорь остаётся снаружи и говорит о правилах; actor узнаваем, но это не доказательство выполненного игроком обязательства и роста отношения Игоря к игроку | prompts v60/v61 содержат cause; причинность направления не подтверждена |
| a3… /87/v61, igor-teplov, shared_risk +15 | spans3,7: реплика о риске для обоих; общего рискованного действия из выбранного evidence не следует | v62/call1354, v63/call1357 получают cause; сцены продолжают разговор о фактах/протоколе, отдельный эффект отношения не установлен |
| b887… /1/v15, edgar, honest_warning +8, job224/call306 | spans13,15 — предупреждение мужчины с зажигалкой милиционеру; его canonical identity и основание увеличить loyalty к игроку не установлены | v16/call308 и v17/call309 содержат cause; сцена продолжает физическое противостояние, отдельный эффект доверия не доказан |
| b887… /2/v23, edgar, honest_warning +8, job248/call336 | spans5,7 — тот же неназванный мужчина показывает удостоверение милиционеру и описывает опасность; имя из evidence не следует | v24/call320 и v25/call327 cause ещё не получили из-за очереди |
| b887… /3/v31, anton-gorodetsky, honest_warning +10, job272/call389 | span12 — тот же мужчина с зажигалкой говорит, что удар придётся на игрока. В полном source turn он не назван Антоном. Теперь неопознанного участника привязали к другому canonical NPC | prompts v32–v36 ещё без source31; v32/v33 продолжают борьбу у рамы. Изменение отношения Антона не предъявлено |

В текущей партии обработанные relationship jobs сначала дают корректную форму (3 applied/non-seed), но это не закрывает исход §6.1. Для archived выборки character_id Игоря узнаваем, однако selected event/direction не выводится из указанного поступка. В новой выборке есть ещё более определённый дефект идентичности. Ни одна из этих причин не принимается за доказанный полезный downstream эффект.

Независимость кандидатов частична: `mechanics.py:604–685` ловит ValueError отдельно для каждого уже typed candidate (неизвестный ID/event/span и т.п.). Но `RPRelationshipResult:53–54` сначала валидирует весь tuple. Pure JSON probe в текущем контейнере: один valid candidate принят; valid + второй без direction → `ValidationError ('candidates',1,'direction')`. До независимого loop не доходит весь пакет. Это подтверждает schema boundary, не факт такого live model ответа.

Runtime Lore на новом route на итоговом v60:35persisted runtime cards,33authoring_kind=event,2=character. Ручная выборка снята раньше и не экстраполируется на остальные карты. Выборка пяти:
- card1/source v1/call263, spans3,4,5,6,7,10: событие с тенью и неназванными участниками соответствует RAW. **Следующий narrator v2/call264 уже содержит content карты**, и сцена продолжает то же происшествие. Это предъявленная релевантная цепочка §6.2; уникальный контрфактический вклад карты отдельно от RAW не заявляется.
- card7/v7/call281, spans12–15: утверждение об удостоверении принадлежит анонимному участнику; часть content о зажигалке требует иных spans. v8/call282 содержит карту. Сама карта оставляет личность неизвестной, что противоречит позднему необоснованному присвоению edgar.
- card15/v15/call307, spans6,7,14,15: content разрешает местоимения и включает описание, для которого выбранных spans без соседнего контекста недостаточно; v16/call308 и v17/call309 получают карту.
- card23/v23/call337, span10: утверждение об отсутствии дыхания grounded; первое найденное включение v27/call341, затем v28/call342, а v24–v26 без карты.
- card24/v24/call340, spans11–13: event grounded; первое включение также v27/call341.

`mechanics.py:791–807` отбирает newest fitting runtime cards, не выполняет семантический retrieval. World Lore занимает 11834 из 16000 chars. Наличие provenance не доказывает grounding каждой фразы; пять вручную прочитанных карт не экстраполируются на все35.

Administrator: в актуальной DB у обеих длинных Parties **0 proposals, 0 accepted/rejected, 0 guidance**, поэтому RAW→proposal→manual decision→revision→prompt **не сделано, потому что соответствующего обмена в авторитетных данных нет**, а порождать его запрещено рамками аудита. Реальные administrator calls и succeeded cadence jobs имеются (E6), но не подменяют применение proposal.

### E6. Новая Atomic-модель против Gemma: полный доступный срез

Общий итоговый cutoff **2026-09-06T14:39:02Z**: обе настоящие Parties v60 — старая `party_f9490fa8937d` и новая `party_b887b6c8dfc2`. Memory/prompt read отдельно 14:39:08Z, версии те же. Ранний активный срез14:21 имел v55, 10 memory failures и по22 незавершённых Atomic jobs.

**Внешнее изменение контура:** в14:31:17Z прежний acceptance container заменён другим с тем же image и DB mount, но `RP_ATOMIC_SERVICE_ENABLED=false`. Это сделал не аудит. Поэтому итоговые pending24 на каждый тип — недренированная очередь отключённой роли, **не доказательство зависания работающего runner**. Последние completed Atomic calls относятся к14:28, до отключения. Настоящая v60 Party не была продолжена аудитором до66.

| Party / role | succeeded jobs | failed jobs | pending/running | attempts завершённых jobs |
| --- | ---: | ---: | --- | --- |
| Gemma v60 / relationships | 60 | 0 | 0/0 | 60×0; но 0 candidates/causes |
| Gemma v60 / runtime_lore | 0 | 60 | 0/0 | 60×1: 37 truncated,23 invalid strict JSON/result |
| Gemma v60 / story_memory | 39 | 21 | 0/0 | succeeded38×0 +1×1; failed21×3, ReadTimeout |
| Gemma v60 / administrator | 60 | 0 | 0/0 | 58×0 +2×1; skipped cadence тоже succeeded |
| DeepSeek v60 / relationships | 36 | 0 | 24/0 | 36×0 |
| DeepSeek v60 / runtime_lore | 35 | 1 | 24/0 | 35×0 +1×1, keyword budget exceeded на source34 |
| DeepSeek v60 / story_memory | 23 | 13 | 24/0 | 23×0;13×1, truncated |
| DeepSeek v60 / administrator | 60 | 0 | 0/0 | 60×0;8 фактических calls |

Нулевых/единичных/двойных attempts недостаточно для всей картины: старые memory failures имеют **3**, это не опущено. Новая Atomic:108 завершённых jobs,94succeeded/14failed,72pending. Memory отдельно: **13/36 завершённых jobs failed**;23succeeded включают no-op/skipped, реальных successful memory snapshots всего **2**. Поэтому succeeded jobs не равны успешным model updates. Итоговая доля после обработки24pending **не сделана, потому что роль отключена внешним процессом**, и включать её аудит не вправе.

| Party / provider role | completed calls | avg / max,s | error calls | error avg / max,s |
| --- | ---: | --- | ---: | --- |
| Gemma / relationships | 60 | 16.377 /66.620 | 0 | — |
| Gemma / runtime_lore | 60 | 76.796 /110.415 | 0 | — |
| Gemma / story_memory | 4 | 130.733 /136.709 | 64 ReadTimeout | 150.015 /150.019 |
| Gemma / administrator | 8 | 62.481 /119.273 | 2 ReadTimeout | 150.016 /150.016 |
| old Party narrator | 60 | 28.434 /50.696 | 1 ConnectError | 4.017 /4.017 |
| DeepSeek / relationships | 36 | 2.791 /3.859 | 0 | — |
| DeepSeek / runtime_lore | 36 | 5.281 /8.992 | 0 | — |
| DeepSeek / story_memory | 15 | 173.294 /189.838 | 0 | — |
| new Party administrator (Gemma) | 8 | 45.652 /57.812 | 0 | — |
| new Party narrator | 60 | 26.329 /43.941 | 0 | — |

**Деградация воспроизводится в другой форме:** вместо64ReadTimeout новая memory дала13HTTP200/truncated и только2snapshots (coverage8,17). В bounded metadata выборке первых13memory calls успешные285 (6378completion tokens,75.147s),321 (12141tokens,137.396s) имели finish_reason=stop;314,345,354,361,370,377,385,388,391,396,401 — length и ровно16384completion tokens. Для последних двух итоговых calls raw metadata отдельно не печаталась; их terminal truncation подтверждён authoritative job.last_error. Источник ограничения — provider.py:405–413, max_tokens=16384.

Реальный upstream metadata provider=**Baidu**, model `deepseek/deepseek-v4-pro`; pure payload фиксирует `only/order=["baidu/fp8"],allow_fallbacks=false`. Model attempt timeout150s — read/inactivity timeout, не обязательный wall-clock deadline: completed189.838s не объявляется нарушением строгого deadline. Domain failure и остановка coverage остаются фактами.

Relationships ускорились примерно в5.9раза, Lore — в14.5раза по средней длительности completed call. Но при активной роли на14:21 общий Atomic worker ожидал медленную память и отставал на22версии. Позднее отключение роли не отменяет уже измеренных failures. Единственная новая Lore failure — `mechanics.py:114–115` отвергла keyword budget на source34; HTTP200 не равен валидной карте. Полностью успешной замену по короткому canary объявить нельзя.

### E7. Память и фактически сохранённые prompts

Из чистого config/prompt limits текущего контейнера: **W=50, A=8, W+2A=66**. Main использует эти defaults без отдельного override RPPromptLimits. Safe coverage — minimum пяти sections, а не maximum observed_through. Старые snapshots имеют coverage8,16,24,32; новые — 8,17. Они монотонны, но новая память перестала продвигаться.

Сохранённый prompt_text — упорядоченный JSON messages. Он не хранит block IDs/safe_coverage/stable_hash metadata. Поэтому они реконструированы по **точному совпадению сохранённого RAW текста с immutable turns** и точному renderer memory snapshot; это проверка доставки данных, не семантический regex. Во всех 120 completed narrator calls обеих партий: **0 неизвестных RAW, 0 несовпадений формулы** `start=min(floor(max(n-W,0)/A)*A, safe_coverage)` (n — число предшествующих committed turns). Warmup до W отдельно не считается нарушением диапазона W..W+A-1.

| Prompt для committed version | old v60 Party RAW / safe | new v60 Party RAW / safe |
| --- | --- | --- |
| 25 | 1..24 /8 | 1..24 /17 |
| 51 | 1..50 /24 | 1..50 /17 |
| 55 | в пределах проверенной формулы | 1..54 (54 units) /17 |
| 57 | 1..56 (56 units) /24 | 1..56 (56 units) /17 |
| 58 | 1..57 (57 units) /32 | 1..57 (57 units) /17 |
| 59 | 9..58 (50 units) /32 | 9..58 (50 units) /17 |
| 60 | 9..59 (51 units) /32 | 9..59 (51 units) /17 |

В old prompt25 safe=8 (snapshot1); snapshot16 начинает использоваться позднее, к prompt32. Текущая последняя memory row не подставлялась ретроспективно во все prompts.

Сдвиг после 58 committed units виден в **prompt следующего turn59**, off-by-one в отчёте не подменяет формулу. До второго сдвига после66 текущие настоящие Party на cutoff не дошли; archived v66 не заменяет текущую genuine long acceptance, и seeded данные не выдаются за неё.

Максимумы не-RAW слоёв, chars (отдельные maxima не складываются как один реально существовавший prompt):

| Слой | budget | Gemma Party max | новая Party max |
| --- | ---: | ---: | ---: |
| Gateway rules | 1000 | 990 | 990 |
| World | 40000 | 29179 | 29179 |
| Scenario experience | 8000 | 266 | 266 |
| Player | 4000 | 149 | 158 |
| World rules | 8000 | 4190 | 4190 |
| Relationships | 12000 | 2723 | 3129 |
| Lore | 16000 | 11834 | 15991 |
| Narrator note | 1500 | 656 | 656 |
| Story Memory | 24000 | 4366 | 19158 |
| PlayerCorrection overlay | 4000 | отсутствовал | отсутствовал |
| Administrator guidance | 4000 | отсутствовал | отсутствовал |
| Opening | 4000 | не выделен как отдельный non-RAW блок в сохранённом messages | аналогично |

Реальные input chars min/max: old **51281/184876**, new **51051/195321**, hard limit **400000**. Коррекций/admin guidance в live prompt не было, значит их ненулевой бюджет проверен только source/tests, не фактическим наполнением. Opening first input включает материал старта; точное отдельное измерение opening-layer из сохранённых messages не предъявлено и не придумано.

Первые пять статических messages дают по одному неизменному hash на Party. Stable prefix включает ещё первые W RAW units (`narrator.py:292–310`), поэтому hash закономерно меняется в warmup и при смене anchor. Для одного и того же anchored RAW-prefix он неизменен: old v51–58 `366be93d06322798`, v59–60 `405c05d2ff38fe1e`; new v51–58 `bcf7f30d019351f3`, v59–60 `7e76394a58f5465a`. Это сокращённые SHA256 от canonical messages, не утверждение равенства с несохранённым runtime metadata.

cached_tokens провайдер реально отдаёт: old **60/60**, все >0, диапазон28713..115599; new **60/60**, все >0,28662..126816. Наличие cache не доказывает полезность stalled memory.

### E8. Три роли, права, provider policy и UI

Одна поверхность статуса `GET /api/parties/{id}/supervisor`, `G/app/main.py:1131`; `role_status:359–373` отдаёт state, success_count, error_count, last_error и kill_switch. `U/app.js:183–187` рисует role cards.

| Роль | Реальная модель | Status/GUI | Kill switch |
| --- | --- | --- | --- |
| Narrator | OpenRouter openai/gpt-5.6-luna-pro | provider/model из Party, state/error показываются; counters GUI не выводит | RP_NARRATOR_ENABLED, config.py:33 |
| Atomic | OpenRouter deepseek/deepseek-v4-pro, Baidu | **main.py:1157–1162 ложно отдаёт local/Gemma**; GUI использует это поле и игнорирует counters | RP_ATOMIC_SERVICE_ENABLED, :34 |
| Administrator | local gemma-4-26b-a4b-it-rp-q4 | отдельные jobs/state/last_error; counters GUI не выводит | RP_ADMINISTRATOR_ENABLED, :35 |

Kill switches — server configuration и read-only indicator в GUI, не обещание интерактивного переключателя. Supervisor assembled status исследован по точному source текущего контейнера + фактическим calls; полноценная визуальная проверка current DOM не выполнялась. Acceptance GUI имеет другой app.js hash (E10), поэтому его parity с новым UI source не утверждается.

Suggest ручной flow: main.py:1174 list proposals, :1187 decision; turn_engine.py:2308 owner-scoped decision, :2345 reject, :2375–2435 append guidance revision при accept; `U/app.js:195–199` выводит Accept/Reject. Guidance revision независима от Party gameplay version; локальный `test_rp_mechanics.py:834–898` проверяет две guidance revisions при том же игровом состоянии. **Живого принятого proposal в текущих данных нет**.

Provider catalog содержит один playable narrator profile. `provider.py:524–535` отвергает неподдерживаемого provider/unsafe model до client call. Pure container probe отклонил openrouter/auto, openrouter/free, OpenRouter nvidia/model, provider nvidia и nvidia-openai-compatible. Не создавался provider client, не делался платный вызов. `provider_catalog.py:15–16` сохраняет нормализацию legacy NVIDIA имени; это не active catalog и не fallback.

Atomic constructor :109–111 имеет собственную exact binding; Administrator :457–459 остаётся local; `_exact_openrouter_provider:617–629` only/order provider, allow_fallbacks=false. Structured payload :561/:607 требует parameters, reasoning.enabled=false; memory output limit :405–413. Все шесть inventory auto/free hits :250,251,253,256–258 принадлежат **awareness_showroom_*** (catalog/fallback/narrative/intent/validator отдельного training проекта), а не RP. Это действующие настройки другой системы, не обход RP policy; их изменение в аудит не входит. Полный location list в приложении.

Все три Parties имеют exact narrator tuple:
`openrouter-openai-gpt-5-6-luna-pro | openrouter | https://openrouter.ai/api/v1 | openai/gpt-5.6-luna-pro | {}`.
Settings `{}` — реально сохранённое значение, а не выдуманный набор defaults. Main.py:303–310 проверяет binding, :673–700 разрешает BYOK только exact endpoint; ключи не читались/не публиковались. Trigger защищает tuple после создания. Runtime role не наследует запрещённый provider из LLM_PROVIDER.

### E9. Проверки, LOC и время

Запрошенный `powershell.exe -NoProfile -File scripts/ci.ps1` сначала остановлен execution policy; процессный `-ExecutionPolicy Bypass` прошёл дальше, но отсутствующий pytest прервал bootstrap. Обе неудачи зафиксированы, не объявлены зелёным прогоном. Финальный запуск переиспользовал уже существующий declared test-deps соседнего Atomic worktree через PYTHONPATH, ничего не устанавливал:

```powershell
$env:PYTHONPATH = 'C:\Users\Адександр\Documents\Tavern\codex-worktrees\decision043-atomic-model\roles\apps\files\rp-stack\rp-gateway\.test-deps'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/ci.ps1
```

pytest9.1.1 / pydantic2.13.4 / fastapi0.141.1. Итог: **100 passed, 2 warnings in8.34s**, скрипт напечатал **Local Gateway full-suite 8.8s /60s**. Отдельного timed focused теста не было; полный локальный прогон также меньше30s, но это не переименовано в focused measurement. Repository/skill-sync/devkit/JS checks прошли; git status после них чист.

Напечатано **5028 LOC, debt28/5000**. Независимый физический подсчёт:
`test_rp_gateway_integration.py697 + lifecycle186 + mechanics1053 + narrator_memory982 + provider778 + runner629 + turn_engine470 + world_scenario233 =5028`.
Ещё `U/rp-clean-flow.test.js=46` →5074/debt74.
Четыре автоматически вызванные RP validator functions =122+219+115+202=**658** → нижняя RP-only граница5732/debt732.
Но Decision требует считать mixed файл целиком до физического разделения: `scripts/validate-repository.py=1405`, поэтому консервативный **общий total6479/debt1479**. 658 повторно не прибавляется к1405. Учитываются все восемь test files вместе с их helpers. Отдельного evals нет. Исключены production loader/schema, общие security/deploy checks/orchestration, отдельный Awareness, ручные/live scripts и ровно12 blind anchors; общий validator посчитан целиком по правилу mixed.

На текущем GitHub runner для PR135 с теми же итоговыми runtime/test файлами:
[run34034925059 / gateway job101491133569](https://github.com/abykovwww-byte/ubuntu_ansible_palybooks/actions/runs/34034925059/job/101491133569) — **100 passed,2 warnings in3.87s; time docker run real5.084s**. Лимит60s выполнен, временной долг0. Это не время build всего job и не local замена GitHub измерения. Main-push run34036182117 проверен как success; он не выполняет gateway из-за event condition. `gh run view --log` потребовал обычный локальный gh cache; после доступа к cache лог прочитан.

Полный перечень выявленных запрещённых фиксаций:
| Проверка | Locations | Что фиксирует |
| --- | --- | --- |
| test_raw_anchor_safe_coverage_stable_prefix_and_source_ownership | G/tests/test_rp_narrator_memory.py:635–647 | пять exact русских narrator phrases вместо видимого prose outcome |
| test_atomic_and_administrator_use_separate_exact_routes | G/tests/test_rp_provider.py:447–453 | буквальная английская instruction про source_turn_versions_overflow |
| тот же test | G/tests/test_rp_provider.py:407 | наличие внутреннего prompt marker OUTPUT_SCHEMA= в первом messages element; это привязка к сборке инструкции, сверх проверки внешнего response_format/schema |
| top-level GUI source test | U/rp-clean-flow.test.js:9–20 | девять endpoint literals в app.js |
| тот же | :23–28 | исходные выражения idempotency key, expected_version, current_version, source_turn_ids, always_on и provider |
| тот же | :29–30 | отсутствие flag/data-legacy/auto/NVIDIA/D20/... через source regex |
| тот же | :32–44 | девять HTML id literals; нет браузерного обмена |

Остальные 157 broad grep hits полностью перечислены в приложении и классифицированы как imports/types/fixtures или проверки границ данных. **Не объявлены запрещёнными**: относительное stable_prefix equality/inequality :608–609 без golden digest; RAW/current-player exact text сохранность :375/:615; source ownership sentinel checks :630–634/:648; реальные schema/route payload JSON; saved overlay/Lore/relationship projection; test_rp_provider.py:701 возврат canned provider response без подмены. Это storage/provider/data projection риск, не prose quality gate. Golden MD5/SHA256 констант, function-location checks, retired-world/revision matrix и tests синхронизации docs prose↔skills prose не обнаружено. Skill-sync проверяет доставку копии skill, а Wiki validator — ссылки, не семантическое дублирование правил.

100 world/scenario grep hits в validator классифицированы полностью в приложении: старые manifest-based Lore/clocks/supervisor/narrative validators; общие AGENTS/deploy проверки; отдельный Awareness; main calls. В source нет ни одного tracked WorldPack `*/manifest.json`, поэтому старые RP loops работают по пустому набору. Новый `world.json`, `rp-world.v1`, `scenario-presets` production format validator не описывает. **Нет второго владельца нового формата, но остались осиротевшие legacy branches**.

CI сейчас имеет четыре jobs: repository-contracts, gateway, browser-clients, ansible-syntax. Для PR нет path filters; Gateway строится и полностью тестируется даже при docs-only. В самом workflow один Gateway test image/run, повторного полного прогона во втором образе нет. Это частично закрывает исходную боль, но не процессный инвариант6.

### E10. Что реально запущено и какие БД существуют

Срез inventory 2026-09-06T13:48:36Z:
| Граница | Наблюдение |
| --- | --- |
| fetched origin/main | c2e26536e3e35ce70c7e795ccdb0e0f76f20eda2 |
| server /opt/ubuntu_ansible_palybooks HEAD | 2ad61019fcad7693ce620d1f158bcb3353b6eb1b |
| production Gateway container | 683e68b83dc36c596ee2e92cf1a1e42fd486fb68b280a9f53a875b2145acb018 |
| production exact image | sha256:9321777d9db87da6ac5b2b23c4c085a5d28a51199a90b2ec16d922b4b85295c4 |
| production start | 2026-09-02T12:38:09.600314415Z, restarts0, healthy |
| acceptance Gateway container | c6d0bf8ec2f59a3c5924489cd65457b115a47f77139234cc5162f88b6027ed80 |
| acceptance exact image | sha256:3ba784b4f36ec6520ec04fa8c59cc72e1c2bf58d213f9c238a14be58f3d957f1 |
| acceptance start | 2026-09-06T13:31:43.306473144Z, restarts0 |
| acceptance DB mount | /srv/app-data/rp-stack/decision043-acceptance/step6-89bcc7f → /acceptance; app bind mount отсутствует |

Production `/app/app/main.py` SHA256 LF-normalized =
`af06442c02b1ea5b17fd1d5fdd7f613035c410b55f9a3e1824df94eb3857decc`,
точно совпадает с `git show 2ad61019:.../app/main.py`. Это подтверждение старого deployed entrypoint плюс exact image; полного immutable attestation всех файлов production к этому SHA не найдено, поэтому checkout HEAD не выдан за доказательство каждого байта образа. Текущий source main SHA256 `6172503151812fc05abbbf5e47ef6920fa36c848fcf61caf8d66aac97ef232aa` другой.

**Все22 Python файла acceptance app совпали с c2e2653** после LF normalization, лишних app/*.py нет. Это stronger runtime-source evidence для clean image; полный hash manifest ниже. Сам по себе image tag не использовался как SHA исходников.

Acceptance GUI image `sha256:a1c4ddd3485a69c01645d9c8b59afadf88258cc4fb5b6856ea2c25e0be2ebab2` запущен 09-03. Его app.js SHA256 `f5400923108ad33bf9b6c69d15befaecdb1368fa5e2bd38dd86f91b34809572e` **не совпадает** с текущим source `7ce3a0496ecbf768aeacd5caeb0527563d3e04b4718ae1948ca8fc4bcdb475d7`; UI/source parity не заявляется. Production GUI exact image `sha256:ac13a24684cd96ed39975b92d94ea11365f42ee5841459533aaf63db119c264f`.

Без sudo: systemctl show Result=success, ExecMainStatus0, inactive, timestamp fields пусты; доступный journal содержит recap **2026-09-02 12:59:26 MSK**, ok74 changed8 unreachable0 failed0 skipped40, успешно завершённую ansible-local-apply.service. Это последний доступный apply log, **не свидетельство применения c2e2653**. Запрошенный `docker compose images` завершился ошибкой из-за отсутствующего image остановленного candidate; exact image выяснен независимым docker inspect, никакого rebuild/restart не выполнялось.

Production env allowlist: RP_REBUILD_ENABLED=false, RP_DATABASE_URL=sqlite:////data/rp_engine.db; mount /srv/app-data/rp-stack/gateway. **Production rp_engine.db отсутствует.** `rp_gateway.db` имеет45 таблиц и78 legacy parties; существующие state и девять backup archives сохранены. Названия и IDs, а не prompts/secrets, приведены ниже.

Повторный inspect на14:40 подтвердил неизменный production container/image. Acceptance уже заменён внешним процессом: container `887a19607486e99f686e498139cb20a982431d293719710336464abbf3251921`, тот же exact image3ba784b… и тот же /acceptance mount, start `2026-09-06T14:31:17.544974388Z`, restarts0, Narrator/Admin=true, **Atomic=false**. Это объясняет недренированную финальную очередь. После отключения нет нового committed turn, поэтому Atomic-off gameplay K07 всё ещё не доказан; согласованного recovery experiment с работающим worker K08 тоже нет.

Acceptance `/acceptance/rp_engine.db`: schema8,12таблиц,13triggers; shared `rp_gateway.db`:5таблиц. Parties:
- party_5c0cafdff2d9 — v0, action-independent;
- party_f9490fa8937d — v60, action-night-trainee, старая Atomic Gemma;
- party_b887b6c8dfc2 — v60 на итоговом cutoff14:39 (v55 на14:21), action-night-trainee, новая Atomic DeepSeek.

Обнаруженные архивы с clean DB: decision043-acceptance/runner-probe/data (schema7,v1); seeded-run/seeded-run-2 (v51); seeded-run-3/seeded-run-4 (v66); step6-dd912cd (schema8,пустая); decision043-acceptance-run7/run8/run9/run10/run11/data (schema7,клоны предыдущих партий). Максимумы66: party_2e0fba1e9f4d, party_fd6842c34c33 (seeded); party_70d55c1c3f86, party_6398e2598135, party_a3a1c666c679 в архивных run-копиях. Клонированная Party не посчитана новой независимой приёмкой.

### E11. Принятая человеком проза и граница её доказательства

[043-narrator-human-ab-2026-09-03.md](043-narrator-human-ab-2026-09-03.md) фиксирует принятое человеком универсальное поведение candidate `89bcc7f4409155c28601fa03a10265af468bbaaa`:12пар,8wins/1loss/3tie, одинаковый exact narrator route; прежнее clean-vs-legacy сравнение11/0/1. Это именно human preference evidence, не Delivery-статус ADR.

`git diff 89bcc7f c2e2653 -- G/app/rp/narrator.py` пуст, current container file parity проверен. Однако там же остаются residual pair10 recap,11 пассивный бой/спасение,12 неясный адресат. Этот аудит не перечитал весь blind набор с независимым строгим человеческим verdict «нет puppeteering/contradiction/meta/service вставок» и не выдаёт 8 побед за такой verdict. Архивная 17-turn Party в evidence не равна требуемым20+контрастному старту. Новая модель Atomic меняет производные данные, поэтому неизменность narrator.py сама по себе также не гарантирует прежнего игрового качества.

## Приложение: полные location lists поисков

Все номера относятся к base SHA. Поиск выполнялся от repo root; exit1 без строк означает отсутствие совпадений, не недочитанный результат. Строки объединены по одинаковой классификации, ни один найденный location не отброшен. Контекст каждой группы описан в E1–E9.

### revision_gates (7 hits)

Команда: `git grep -nE contract_revision\|CONTRACT_SOURCE_MAX_REVISION\|revision *(>=\|<=\|==\|<\|>) *[0-9] -- roles/apps/files/rp-stack/rp-gateway` (pattern передавался отдельным argv, без shell pipeline).

| Файл | Все строки | Классификация |
| --- | --- | --- |
| `G/app/rp/schema.py` | 117, 135, 314, 335, 357 | Версии memory/correction/guidance либо их тест; не gameplay revision gate |
| `G/tests/test_rp_mechanics.py` | 897, 898 | Версии memory/correction/guidance либо их тест; не gameplay revision gate |

### schema_revision (9 hits)

Команда: `git grep -n revision -- roles/apps/files/rp-stack/rp-gateway/app/rp/schema.py` (pattern передавался отдельным argv, без shell pipeline).

| Файл | Все строки | Классификация |
| --- | --- | --- |
| `G/app/rp/schema.py` | 117, 134, 135, 138 | Memory revision/base chain/uniqueness |
| `G/app/rp/schema.py` | 314, 320, 335, 357, 361 | PlayerCorrection или Administrator version/uniqueness |

### world_forbidden (5 hits)

Команда: `git grep -nE player_role\|openings\|state_seed\|rp_supervisor -- roles/apps/files/rp-stack/rp-gateway/app/rp/content.py` (pattern передавался отдельным argv, без shell pipeline).

| Файл | Все строки | Классификация |
| --- | --- | --- |
| `G/app/rp/content.py` | 115, 155, 291, 310, 332 | Scenario field/materialization; не WorldDefinition |

### local_overrides (13 hits)

Команда: `git grep -n local_overrides -- roles/apps/files/rp-stack/rp-gateway/app roles/apps/files/rp-stack/rp-light-gui` (pattern передавался отдельным argv, без shell pipeline).

| Файл | Все строки | Классификация |
| --- | --- | --- |
| `G/app/main.py` | 580, 620 | Request/schema/materialization/projection |
| `G/app/main.py` | 862 | Исполняемый Lore API |
| `G/app/models/schemas.py` | 69 | Request/schema/materialization/projection |
| `G/app/rp/content.py` | 125, 166, 302, 320, 343 | Request/schema/materialization/projection |
| `G/app/rp/mechanics.py` | 331, 365 | Исполняемая проверка Lore budget |
| `G/app/rp/narrator.py` | 232 | Исполняемое включение Scenario Lore в prompt |
| `G/app/rp/provider.py` | 284 | Исполняемый draft context |

### lore_origin (4 hits)

Команда: `git grep -n origin -- roles/apps/files/rp-stack/rp-gateway/app/rp/narrator.py roles/apps/files/rp-stack/rp-gateway/app/rp/turn_engine.py` (pattern передавался отдельным argv, без shell pipeline).

| Файл | Все строки | Классификация |
| --- | --- | --- |
| `G/app/rp/turn_engine.py` | 201, 1664, 1851, 2884 | Runtime Lore dataclass/persist/read; Scenario renderer дополнительно mechanics.py:846 |

### fallback (3 hits)

Команда: `git grep -nEi fallback\|repair\|safe_scene\|NON_CANONICAL -- roles/apps/files/rp-stack/rp-gateway/app` (pattern передавался отдельным argv, без shell pipeline).

| Файл | Все строки | Классификация |
| --- | --- | --- |
| `G/app/rp/narrator.py` | 117 | Docstring о границе ответственности |
| `G/app/rp/provider.py` | 628 | Явный запрет fallback |
| `G/app/services/provider_catalog.py` | 87 | Описание exact catalog route |

### forbidden_claims (21 hits)

Команда: `git grep -n forbidden_claims -- roles/apps/files/rp-stack/rp-gateway/app` (pattern передавался отдельным argv, без shell pipeline).

| Файл | Все строки | Классификация |
| --- | --- | --- |
| `G/app/main.py` | 280 | Typed PlayerCorrection rule-only schema/prompt/persistence; полный lifecycle E4, не narrator substring predicate |
| `G/app/rp/mechanics.py` | 140, 151, 160, 174, 176, 1053, 1054, 1061 | Typed PlayerCorrection rule-only schema/prompt/persistence; полный lifecycle E4, не narrator substring predicate |
| `G/app/rp/provider.py` | 323, 325, 326 | Typed PlayerCorrection rule-only schema/prompt/persistence; полный lifecycle E4, не narrator substring predicate |
| `G/app/rp/schema.py` | 290, 291 | Typed PlayerCorrection rule-only schema/prompt/persistence; полный lifecycle E4, не narrator substring predicate |
| `G/app/rp/turn_engine.py` | 253, 1913, 1927, 2114, 2949, 2950, 2991 | Typed PlayerCorrection rule-only schema/prompt/persistence; полный lifecycle E4, не narrator substring predicate |

### attempts (24 hits)

Команда: `git grep -n attempts -- roles/apps/files/rp-stack/rp-gateway/app/rp` (pattern передавался отдельным argv, без shell pipeline).

| Файл | Все строки | Классификация |
| --- | --- | --- |
| `G/app/rp/schema.py` | 188, 189, 195, 215, 216, 222 | Schema/type/insert/read, не increment |
| `G/app/rp/turn_engine.py` | 145, 146, 166, 167, 1367, 1368, 2604, 2643, 2814, 2815, 2845, 2846 | Schema/type/insert/read, не increment |
| `G/app/rp/turn_engine.py` | 1145, 1182 | Eligibility claim; без изменения attempts |
| `G/app/rp/turn_engine.py` | 1250, 1282 | Единственные failure increments |
| `G/app/rp/turn_engine.py` | 1252, 1284 | Terminal status expression того же failure UPDATE |

### update_predicate (0 hits)

Команда: `git grep -nE UPDATE .*(status\|state).*WHERE -- roles/apps/files/rp-stack/rp-gateway/app/rp` (pattern передавался отдельным argv, без shell pipeline).

Совпадений нет; outer UPDATE predicate прочитан в turn_engine.py:1140–1191.

### RP_REBUILD_ENABLED (12 hits)

| Файл | Все строки | Классификация |
| --- | --- | --- |
| `codex-skills/rp-world-pack-builder/references/rp-stack-paths.md` | 69 | Docs/skills removed mechanism; GUI source-string gate — E1 |
| `docs/wiki/01-architecture.md` | 63, 68 | Docs/skills removed mechanism; GUI source-string gate — E1 |
| `docs/wiki/02-interfaces.md` | 20, 151 | Docs/skills removed mechanism; GUI source-string gate — E1 |
| `docs/wiki/04-worldpacks-and-modes.md` | 6 | Docs/skills removed mechanism; GUI source-string gate — E1 |
| `docs/wiki/06-models-and-providers.md` | 17 | Docs/skills removed mechanism; GUI source-string gate — E1 |
| `docs/wiki/09-operations-and-repository.md` | 426, 428 | Docs/skills removed mechanism; GUI source-string gate — E1 |
| `docs/wiki/README.md` | 12, 208 | Docs/skills removed mechanism; GUI source-string gate — E1 |
| `U/rp-clean-flow.test.js` | 29 | Docs/skills removed mechanism; GUI source-string gate — E1 |

### docs/skills drift (19 hits)

| Файл | Все строки | Классификация |
| --- | --- | --- |
| `codex-skills/rp-world-pack-builder/references/rp-stack-paths.md` | 69 | Описывает удалённый механизм |
| `codex-skills/training-world-pack-builder/SKILL.md` | 14 | Актуально (retention/явный исторический контекст/целевой отказ legacy) |
| `docs/wiki/01-architecture.md` | 63, 68, 74, 86 | Описывает удалённый механизм |
| `docs/wiki/02-interfaces.md` | 20, 45, 151 | Описывает удалённый механизм |
| `docs/wiki/04-worldpacks-and-modes.md` | 6 | Описывает удалённый механизм |
| `docs/wiki/04-worldpacks-and-modes.md` | 54 | Актуально (retention/явный исторический контекст/целевой отказ legacy) |
| `docs/wiki/06-models-and-providers.md` | 17 | Описывает удалённый механизм |
| `docs/wiki/06-models-and-providers.md` | 365 | Актуально (retention/явный исторический контекст/целевой отказ legacy) |
| `docs/wiki/08-data-and-security.md` | 293 | Актуально (retention/явный исторический контекст/целевой отказ legacy) |
| `docs/wiki/09-operations-and-repository.md` | 426, 428 | Описывает удалённый механизм |
| `docs/wiki/09-operations-and-repository.md` | 434 | Актуально (retention/явный исторический контекст/целевой отказ legacy) |
| `docs/wiki/README.md` | 12, 208 | Описывает удалённый механизм |

### World references (50 hits)

| Файл | Все строки | Классификация |
| --- | --- | --- |
| `W/prompts/openings/day-witch/opening-scene.md` | 3 | Scenario opening prose, loader читает |
| `W/prompts/openings/day-witch/state-seed.json` | 324, 325, 326, 327, 328, 329, 330, 331 | Scenario-owned full seed, loader читает |
| `W/prompts/openings/independent/opening-scene.md` | 3 | Scenario opening prose, loader читает |
| `W/prompts/openings/independent/state-seed.json` | 324, 325, 326, 327, 328, 329, 330, 331 | Scenario-owned full seed, loader читает |
| `W/prompts/openings/inquisition-observer/opening-scene.md` | 3 | Scenario opening prose, loader читает |
| `W/prompts/openings/inquisition-observer/state-seed.json` | 324, 325, 326, 327, 328, 329, 330, 331 | Scenario-owned full seed, loader читает |
| `W/prompts/openings/night-trainee/opening-scene.md` | 3 | Scenario opening prose, loader читает |
| `W/prompts/openings/night-trainee/state-seed.json` | 324, 325, 326, 327, 328, 329, 330, 331 | Scenario-owned full seed, loader читает |
| `W/rules/checks.md` | 31 | World-owned prose, loader читает; PlayerCharacter leak |
| `W/scenario-presets/action-day-witch.json` | 14 | Живая preset reference initial_state |
| `W/scenario-presets/action-independent.json` | 14 | Живая preset reference initial_state |
| `W/scenario-presets/action-inquisition-observer.json` | 14 | Живая preset reference initial_state |
| `W/scenario-presets/action-night-trainee.json` | 14 | Живая preset reference initial_state |
| `W/scenario-presets/book-day-witch.json` | 14 | Живая preset reference initial_state |
| `W/scenario-presets/book-independent.json` | 14 | Живая preset reference initial_state |
| `W/scenario-presets/book-inquisition-observer.json` | 14 | Живая preset reference initial_state |
| `W/scenario-presets/book-night-trainee.json` | 14 | Живая preset reference initial_state |
| `W/scenario-presets/strategic-day-witch.json` | 14 | Живая preset reference initial_state |
| `W/scenario-presets/strategic-independent.json` | 14 | Живая preset reference initial_state |
| `W/scenario-presets/strategic-inquisition-observer.json` | 14 | Живая preset reference initial_state |
| `W/scenario-presets/strategic-night-trainee.json` | 14 | Живая preset reference initial_state |
| `W/world-info/index.md` | 11 | World-owned prose, loader читает; PlayerCharacter leak |

### policy (12 hits)

| Файл | Все строки | Классификация |
| --- | --- | --- |
| `inventories/local/group_vars/server.yml` | 250, 251, 253, 256, 257, 258 | Действующая отдельная Awareness policy; не RP |
| `G/app/rp/provider.py` | 561, 607, 628 | Exact structured provider boundary/no fallback |
| `G/app/services/provider_catalog.py` | 15, 16 | Legacy name normalization; не active catalog |
| `U/rp-clean-flow.test.js` | 29 | Запрещённый способ source-string assertion |

### roles (4 hits)

| Файл | Все строки | Классификация |
| --- | --- | --- |
| `G/app/main.py` | 365 | Status/kill-switch indicator/manual accept/reject UI; реальные counters/source gap E8 |
| `U/app.js` | 186, 195, 199 | Status/kill-switch indicator/manual accept/reject UI; реальные counters/source gap E8 |

### removed symbols (7 hits)

| Файл | Все строки | Классификация |
| --- | --- | --- |
| `G/app/main.py` | 522, 555 | Разрешённый /api/worldpacks catalog |
| `U/README.md` | 8 | Документация действующего каталога |
| `U/app.js` | 110 | Разрешённый /api/worldpacks catalog |
| `U/rp-clean-flow.test.js` | 10, 30 | Source-string gate |
| `U/styles.css` | 4 | d20 как часть hex-цвета |

### test scan (157 hits)

| Файл | Все строки | Классификация |
| --- | --- | --- |
| `G/tests/test_rp_gateway_integration.py` | 25, 138, 140, 248, 249, 688, 689 | Imports/types/snapshot fixtures или storage/provider/projection checks; не golden prose. Дополнительные запрещённые multiline phrases E9 |
| `G/tests/test_rp_gateway_lifecycle.py` | 12, 13, 14, 15, 39, 40, 41, 52, 53, 76 | Imports/types/snapshot fixtures или storage/provider/projection checks; не golden prose. Дополнительные запрещённые multiline phrases E9 |
| `G/tests/test_rp_mechanics.py` | 10, 11, 12, 13, 31, 44, 45, 46, 64, 65, 100, 189, 192, 198, 410, 411, 419, 420, 619, 620, 631, 632 | Imports/types/snapshot fixtures или storage/provider/projection checks; не golden prose. Дополнительные запрещённые multiline phrases E9 |
| `G/tests/test_rp_narrator_memory.py` | 10, 11, 12, 13, 23, 70, 71, 72, 83, 84, 107, 116, 121, 143, 215, 220, 222, 254, 369, 392, 405, 407, 409, 413, 415, 418, 419, 432, 434, 440, 442, 448, 450, 456, 458, 460, 467, 469, 471, 489, 491, 497, 502, 529, 547, 549, 592, 598, 604, 676, 678, 711, 731, 733, 738, 740, 757, 759, 760, 840, 842, 870, 981 | Imports/types/snapshot fixtures или storage/provider/projection checks; не golden prose. Дополнительные запрещённые multiline phrases E9 |
| `G/tests/test_rp_narrator_memory.py` | 648 | Data source ownership sentinel, разрешённый риск |
| `G/tests/test_rp_provider.py` | 15, 16, 17, 18, 80, 81, 114, 115, 147, 149, 527, 701 | Imports/types/snapshot fixtures или storage/provider/projection checks; не golden prose. Дополнительные запрещённые multiline phrases E9 |
| `G/tests/test_rp_runner.py` | 12, 13, 14, 15, 30, 31, 32, 43, 44, 67 | Imports/types/snapshot fixtures или storage/provider/projection checks; не golden prose. Дополнительные запрещённые multiline phrases E9 |
| `G/tests/test_rp_turn_engine.py` | 11, 12, 13, 14, 25, 26, 27, 38, 39, 62, 95, 306 | Imports/types/snapshot fixtures или storage/provider/projection checks; не golden prose. Дополнительные запрещённые multiline phrases E9 |
| `G/tests/test_rp_world_scenario.py` | 16, 18, 72, 112, 175, 188, 189, 200, 201, 203, 204, 208, 209, 215, 216, 222, 223, 229 | Imports/types/snapshot fixtures или storage/provider/projection checks; не golden prose. Дополнительные запрещённые multiline phrases E9 |
| `U/rp-clean-flow.test.js` | 29, 30 | Запрещённые source-string gates E9 |

### validator World/Scenario (100 hits)

| Файл | Все строки | Классификация |
| --- | --- | --- |
| `scripts/validate-repository.py` | 38, 39, 40, 79 | Legacy manifest Lore validator,0current inputs |
| `scripts/validate-repository.py` | 162, 163, 196, 203, 204, 210, 214, 218, 221, 228, 232, 238, 243, 246, 251, 261, 277, 282, 297, 303, 307, 312, 321, 336, 339, 343, 345, 351, 355, 356, 366, 368, 377 | Legacy manifest clocks validator,0current inputs |
| `scripts/validate-repository.py` | 382, 384, 388, 410 | Legacy manifest supervisor validator,0current inputs |
| `scripts/validate-repository.py` | 498, 499, 537, 562, 573, 575, 576, 577, 582, 583, 584, 585, 588, 589, 591, 593, 595, 596, 597, 599, 601, 602, 603, 604, 609, 617, 631, 632, 633, 639, 641, 642, 643, 665, 667, 668, 674, 688, 689, 698, 699 | Legacy manifest narrative validator,0current inputs |
| `scripts/validate-repository.py` | 737, 751, 752, 753, 766 | Общие AGENTS/security/deploy source guards |
| `scripts/validate-repository.py` | 1076, 1100, 1117, 1118, 1137, 1153, 1154, 1212, 1213 | Отдельный Awareness deployment, не новый RP format |
| `scripts/validate-repository.py` | 1386, 1387, 1389, 1400 | Main orchestration calls |

Дополнительный просмотр multiline assertions (не все они ловятся предложенным grep): narrator_memory.py:375,608,609,615,630–648; provider.py test:407,410,427,446–456,701; mechanics test:295–333,401–403,770; integration test:521,589–598,644. Из них запрещённые prose/marker assertions перечислены E9; прочие сохраняют RAW/typed projection/источник данных/реальную provider schema. Для строгой внешней schema допустима проверка role/body формы; отдельное закрепление внутреннего OUTPUT_SCHEMA marker не требуется этим контрактом.

## Приложение: app hash manifest и retained data inventory

LF-normalized SHA256 каждого файла acceptance image3ba784b… совпал с base source; app bind mount отсутствует. Python files вне списка не найдены.

| Файл под G | SHA256 |
| --- | --- |
| app/__init__.py | `a1db7a1f9c004a99c8ed8a38f33229aa6403f14dc3106a2126b52c7402b1dc23` |
| app/api/__init__.py | `c5e79b75137811726e30acef2fa5cfc649514fce1c86da771535b368f43238c0` |
| app/core/__init__.py | `b0d83f6ece4c51256461cfcfd460661a45ad126f375d3d258858b8ff22800c12` |
| app/core/config.py | `43b8248da4d4012e714a906f6a7f8dc39e847a437b85efe65816d469110e7c5d` |
| app/main.py | `6172503151812fc05abbbf5e47ef6920fa36c848fcf61caf8d66aac97ef232aa` |
| app/models/__init__.py | `8ccb410ba17759bccc7a274fcfdc6b5f4349472b2f7d76e76c63f79e2b981b77` |
| app/models/schemas.py | `e597e533b535e5848dc2faa3e495ea38728944290b62b00fdc4b3a922d1c8620` |
| app/rp/__init__.py | `058cbce9be7028d41b5874d88f15b84ea0dd8073eaf51cc67d0ab15f5664778b` |
| app/rp/content.py | `25aebdf7367dd77af98db585ee6166d9f17f3475b6003165dc66abed030f1654` |
| app/rp/mechanics.py | `7c48a977a3f9e263dccbaf3c890fc4bfd15d14c87212bf66d16f64a0c41aebd0` |
| app/rp/memory.py | `ac0efe761b929c2af5f65696b4d9959e80e0a5d9af1d231792b4bd2b42a2b38b` |
| app/rp/narrator.py | `449991175d61be2c59273687001681d49981da41cb980d01168b8b8640091348` |
| app/rp/provider.py | `45dcedad06219e1d66dde876e60be268f587e0804f2578adfea861eb04b102ae` |
| app/rp/runner.py | `faaf774091d855178044e0bd931006791402b24ee439bb6eadea3e0ec657e611` |
| app/rp/schema.py | `f5ea3a67e2d688290f3758f2b6f94c2156563b9106564cda4f218882aec49e7c` |
| app/rp/turn_engine.py | `49e6974fe61720e0831a67a8334dae9dbc634dd094a231688dd4c7d499cf8c6a` |
| app/services/__init__.py | `954a57e6961f9cf4eaab070cdd969c46c3c48ebbdd71a29647814cdcb6ddd1ba` |
| app/services/auth_store.py | `be0ecab81742d1327bc6e1727e155798de1c34980ab6dff44ed41e752a1167c5` |
| app/services/provider_auth.py | `8b27f5973b9e9924649a85c448708706c9ef30acb975e92ae305b97fcc7d2575` |
| app/services/provider_catalog.py | `842847ab1447c84bb63f693b15c27d0c8ef52c0170bac10bfe7995031bffb786` |
| app/services/service_model_client.py | `0fa48457419121a0b046ef031e643caa2e3491d794212667d9de5f543f04e58e` |
| app/services/trace_redaction.py | `3a820cfcc11764d2e85af01a6dedfe7310251935551948330cce0491735ca45b` |

Все78retained production legacy Party IDs (`/srv/app-data/rp-stack/gateway/rp_gateway.db`, таблица parties; это не clean RP database):

```text
party_03d29eda3d3e party_092d16017d55 party_0ca15122e82e party_115be745aeb1 party_145e6e1ddae1 party_16c210a8a099
party_16f68f4f2ba3 party_17c8aabd677e party_1b8e5700b0dc party_1bc1a1204dde party_26492f1680ea party_29028d28eacc
party_29a0a0226f0f party_2c9e988bbba6 party_30fd9d3cc6ef party_357da5f1e80e party_39f2d3cd6307 party_3a33367b6b2b
party_3cba51811098 party_3e09b9092765 party_431dca4dd929 party_44593653ec2f party_44fab0887614 party_48fd541fdb8d
party_4a07c4ad0613 party_4a237721e621 party_4b405a7e3b17 party_50c80bf8a5aa party_517a98233313 party_5300ca2718f8
party_56b358768466 party_62aa19a07646 party_65bc51712c6a party_6881559a7c8b party_6e4fcb895acb party_6e71f4da91fe
party_6f72d7aa5647 party_70eb0fcb59d0 party_7148a5e64893 party_75ad06823b5a party_7928b20be697 party_798e274ff66c
party_7be5c54ae2ba party_7c4a50ff520f party_7cd4cb0229a0 party_7e45dad47ae0 party_8378208ab83c party_8526185371da
party_8768606a3ed7 party_8c6eb217892a party_9a2c91fd5c21 party_a2c5a0bc62f3 party_aa312033929a party_ad201794ce31
party_ad6f220943cf party_ae2716f24a7e party_b286ed285388 party_b29041e7749d party_b2deed7af40e party_bb3fb309f411
party_bd0783b805e3 party_bd5f07116f62 party_c82153b0c2da party_ca9423dfc6be party_cac70558b50a party_d112eb69c583
party_d95a89a9c280 party_db2d22f79978 party_e01b6fac37cf party_e0679c3f9492 party_e0987d5935a1 party_e54006c0b441
party_e877366934dc party_ea305d96aec4 party_ecc67f19fd72 party_ed3451c230d0 party_f5f47884b912 party_f8b248950f93
```

Существующие backup archives в /srv/backups/rp-stack (наличие/размер прочитаны; распаковка/restore и проверка содержимого каждого архива не выполнялись):

- `decision043-browser-proof-20260902T112743Z.tar.gz` — 29331812 bytes.
- `decision043-acceptance-run3-20260830T182144Z.tar.gz` — 1865412 bytes.
- `decision043-acceptance-run4-20260831T062632Z.tar.gz` — 1682229 bytes.
- `decision043-acceptance-run8-20260901T092919Z.tar.gz` — 29906010 bytes.
- `decision043-acceptance-run10-20260901T094026Z.tar.gz` — 29954113 bytes.
- `decision043-acceptance-run7-20260901T073326Z.tar.gz` — 25840894 bytes.
- `rp-stack-20260830T162830Z.tar.gz` — 13606112139 bytes.
- `decision043-acceptance-run11-20260901T112423Z.tar.gz` — 29977927 bytes.
- `decision043-acceptance-run9-20260901T093552Z.tar.gz` — 29929639 bytes.

## Воспроизводимость чтения

Локальный script передавался stdin через PowerShell here-string; на сервере файл не создавался. Для существующего container:

```powershell
$auditProbe = @'
# Python: schema-first, далее SELECT; примеры ниже
'@
$auditProbe | ssh -i "$env:USERPROFILE/.ssh/id_ed25519_codex_abykovserv" -o BatchMode=yes abykov@192.168.1.88 'docker exec -i decision043-acceptance-gateway python -'
```

Минимальный полный агрегирующий probe (только текущие несекретные таблицы):

```python
import sqlite3, json, datetime

def connect(path):
    db = sqlite3.connect('file:' + path + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('BEGIN')
    return db

e = connect('/acceptance/rp_engine.db')
l = connect('/acceptance/rp_gateway.db')
print('UTC', datetime.datetime.now(datetime.timezone.utc).isoformat())
for db, names in ((e, ('rp_parties', 'rp_service_jobs', 'rp_story_memory_snapshots')),
                  (l, ('service_call_log',))):
    for name in names:
        print(name, [r[1] for r in db.execute('PRAGMA table_info(' + name + ')')])
for p in e.execute('SELECT id,current_version FROM rp_parties ORDER BY created_at'):
    pid = p['id']
    print('PARTY', dict(p))
    for r in e.execute('SELECT job_type,status,attempts,count(*) n FROM rp_service_jobs WHERE party_id=? GROUP BY job_type,status,attempts', (pid,)):
        print('JOBS', dict(r))
    for r in l.execute('SELECT role,provider,model,status,count(*) n,avg(latency_ms)/1000 avg_s,max(latency_ms)/1000 max_s FROM service_call_log WHERE party_id=? GROUP BY role,provider,model,status', (pid,)):
        print('CALLS', dict(r))
    for r in e.execute('SELECT revision,observed_through_version,situation_coverage,threads_coverage,characters_coverage,assets_and_rules_coverage,chronology_and_hooks_coverage FROM rp_story_memory_snapshots WHERE party_id=? ORDER BY revision', (pid,)):
        print('MEMORY', dict(r))
```

Для source/image parity: local `git show <base>:<file>` bytes и container `Path('/app/app').rglob('*.py')`, SHA256 с CRLF→LF; проверен полный manifest выше. Для длинной памяти применялся следующий полный read-only алгоритм; это воспроизводимый листинг внутри evidence, не новый автоматически запускаемый test/helper:

```python
import sqlite3,json,hashlib,collections,datetime
from app.rp.memory import RPStoryMemorySnapshot,memory_prompt_text
from app.rp.narrator import RPPromptLimits
e=sqlite3.connect("file:/acceptance/rp_engine.db?mode=ro",uri=True);e.row_factory=sqlite3.Row;e.execute("BEGIN")
l=sqlite3.connect("file:/acceptance/rp_gateway.db?mode=ro",uri=True);l.row_factory=sqlite3.Row;l.execute("BEGIN")
limits=RPPromptLimits()
def h(obj):return hashlib.sha256(json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()
print("UTC",datetime.datetime.now(datetime.timezone.utc).isoformat())
for p in e.execute("select id,current_version,world_snapshot_json,world_hash,scenario_snapshot_json,scenario_hash from rp_parties where current_version>0 order by created_at"):
 pid=p["id"];turns=list(e.execute("select committed_version,narrator_text,player_text,request_id from rp_turns where party_id=? order by committed_version",(pid,)))
 reqv={t["request_id"]:t["committed_version"] for t in turns}
 rawlookup={t["narrator_text"]:t["committed_version"] for t in turns}
 memories={}
 coverage=[]
 for s in e.execute("select revision,snapshot_json from rp_story_memory_snapshots where party_id=? order by revision",(pid,)):
  m=RPStoryMemorySnapshot.model_validate_json(s["snapshot_json"])
  memories[memory_prompt_text(m)]=(s["revision"],m.safe_coverage);coverage.append(m.safe_coverage)
 rows=[];layermax=collections.defaultdict(int);static=set();hash_by_window=collections.defaultdict(set);cache=[];unknown=[];raw_mismatches=[]
 for c in l.execute("select id,request_id,prompt_text,usage_json from service_call_log where party_id=? and role='rp_narrator' and status='completed' order by id",(pid,)):
  msgs=json.loads(c["prompt_text"]);v=reqv.get(c["request_id"]);raw=[];i=5
  while i<len(msgs)-1 and msgs[i]["role"]!="system":
   if msgs[i]["role"]=="assistant":
    rv=rawlookup.get(msgs[i]["content"])
    if rv is not None:raw.append(rv)
    else:unknown.append([c["id"],i])
   i+=1
  layers=[("gateway",msgs[0]),("world",msgs[1]),("scenario",msgs[2]),("player",msgs[3]),("world_rules",msgs[4])]
  layers += [(m["content"].split("\n",1)[0],m) for m in msgs[i:-1]]
  for k,m in layers:layermax[k]=max(layermax[k],len(m["content"]))
  mem=next((memories[m["content"]] for m in msgs if m["content"] in memories),(None,0))
  static.add(h(msgs[:5]))
  stab=msgs[:5];seen=0
  for m in msgs[5:i]:
   if seen>=limits.raw_window_turns:break
   stab.append(m)
   if m["role"]=="assistant":seen+=1
  hp=h(stab);hash_by_window[tuple(raw[:50])].add(hp)
  if v:
   n=v-1;start=min(((max(n-50,0))//8)*8,mem[1]);expected=list(range(start+1,v))
   if raw!=expected:raw_mismatches.append({"v":v,"raw":raw,"expected":expected,"coverage":mem[1]})
  usage=json.loads(c["usage_json"] or "{}");cached=usage.get("prompt_tokens_details",{}).get("cached_tokens")
  if cached is not None:cache.append(cached)
  rows.append({"v":v,"call":c["id"],"raw_min":min(raw) if raw else None,"raw_max":max(raw) if raw else None,"raw_n":len(raw),"memory_revision":mem[0],"safe":mem[1],"chars":sum(len(m["content"]) for m in msgs),"stable":hp[:16],"cached":cached})
 w=json.loads(p["world_snapshot_json"]);s=json.loads(p["scenario_snapshot_json"])
 print("PARTY",pid,p["current_version"],"hash_valid",h(w)==p["world_hash"],h(s)==p["scenario_hash"],"source_world_hash",p["world_hash"],"initial_state_keys",list(s["initial_state"]),"coverage",coverage,"static_hashes",len(static),"stable_in_same_window",all(len(x)==1 for x in hash_by_window.values()),"unknown_raw",unknown,"raw_mismatches",raw_mismatches)
 print("MAXLAYERS",json.dumps(dict(layermax),ensure_ascii=False),"input_min_max",min(x["chars"] for x in rows),max(x["chars"] for x in rows),"CACHE",{"reported":len(cache),"positive":sum(x>0 for x in cache),"min":min(cache) if cache else None,"max":max(cache) if cache else None})
 print("ANCHORS",json.dumps([r for r in rows if r["v"] in (1,8,9,16,17,24,25,32,33,40,48,50,51,57,58,59,60,64,65,66,67) or r==rows[-1]]))

```

## Исполнение всех14шагов и сознательно не выполненная работа

| Шаг | Исход | Доказательство |
| --- | --- | --- |
| 0 | Выполнено: свежий fetch, отдельный clean worktree, base c2e2653. | Первая строка отчёта, git status/rev-parse. |
| 1 | Выполнено: 19 drift hits классифицированы, 11 flag hits в семи внешних поверхностях. | E1 и полные location lists. |
| 2 | Выполнено: 0 clean gameplay revision gates, 0 contract_revision columns; остальные7/9hits объяснены. | E2; schema-first mode=ro, container source. |
| 3 | Частично: запрет5полей, snapshot/hash, loader и13read sites подтверждены. **Не сделано, потому что** нет source World update и непустого scenario Lore/free flow в актуальной БД. | E3; L01–L04,K02,K09. |
| 4 | Частично: реальный same-key retry дал ровно turn46; fallback/substring нет. **Не сделано, потому что** нет наблюдённого DOM failure/retry и turn после Atomic-off. | E4; calls105/112, request/turn46; K07. |
| 5 | Выполнен source/data audit claim/attempts/lifecycle/roles. **Не сделано, потому что** нет контролируемого live restart с работающим worker и before/after timeline; внешний replacement был с Atomic=false. | E4,E6,E10; K08. |
| 6 | Выполнено: все3causes longest archived v66 и все3новые сопоставлены вручную; выявлены неверные identity/event/direction. **Не сделано, потому что** в longest Party всего3, а не5–10causes; полезный scene effect не предъявлен. | E5; G01,L05. |
| 7 | Частично: 120saved prompts, RAW formula, budgets, coverage/hash/cache измерены. **Не сделано, потому что** обе актуальные настоящие Party остановились на60<66; отдельных ненулевых admin/correction и отдельного opening-layer metadata нет. | E7; K05. |
| 8 | Частично: API/GUI controls в source найдены, Atomic model/status ошибочен, counters скрыты. **Не сделано, потому что** нет live proposals/guidance/decisions и полной DOM проверки; GUI hash отличается. | E8; G03,K10. |
| 9 | Выполнено: route preflight и actual calls; новая память13failed,2snapshots,173.294s avg. **Не сделано, потому что** итоговая очередь не дренирована: Atomic отключён внешним процессом, включение запрещено аудитом. | E6,E8; F02–F04. |
| 10 | Выполнено: scripts/ci.ps1,100passed; полный LOC6479/debt1479; GitHub full5.084s; запрещённые assertions и orphan validators перечислены. **Не сделано, потому что** отдельный focused timer не запускался; local full8.8s отдельно указан. | E9 и полный search appendix. |
| 11 | Выполнено: production image+старый main hash, acceptance image+22file parity, DB/78legacy Parties/state/9backups. **Не сделано, потому что** immutable attestation всех production files к SHA отсутствует; compose images заменён успешным inspect после ошибки. | E10; actual/apply/source границы разнесены. |
| 12 | Выполнено предъявление трёх цепочек: Lore проходит, relationships имеют дефект, admin не доказан. **Не сделано, потому что** нет manual admin exchange, строгой полной human A/B/20+contrast приёмки и полезного relationship scene outcome. | E5,E11; G01–G03,K03,K04,K06. |
| 13 | Выполнено: один новый report, две части, все незакрытые пункты имеют B-карточки; doc-only PR без merge/apply. | git diff --stat относительно c2e2653: только этот новый файл. |

Итог по 57 строкам требований (повторы одного механизма в разных разделах Decision не являются разными дефектами): **закрыто 11; не закрыто 17; не доказано 5; закрыто в source 22; не проверено на живом 2.**

Сознательно не реализованы исправления memory/extraction, отображение counters/model, удаление seeds/validator branches, переписывание prose tests и docs/skills. Не добавлены зависимости, abstraction, gates, registry, Wiki или ADR. Не создано новых live Parties/calls/proposals; не выполнялись deploy/apply/sudo/restart/SQL write. Внешние изменения acceptance-контейнера отражены как наблюдения, не действия этого аудита. Приоритет исправлений и изменение статуса Decision — решение владельца.
