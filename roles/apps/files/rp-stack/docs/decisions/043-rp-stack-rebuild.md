# Decision 043: Ребилд RP-контура на слоях World / Scenario / Party

**Дата:** 2026-08-28

## Status

**Decision status: Accepted.** RP-контур пересобирается на изолированном движке и
чистом хранилище. Совместимость с существующими RP-партиями, мирами и ревизиями
контракта 0–11 прекращается.

**Delivery status:** clean-only шаг 4 merged PR #131 в `main` как
`d61e8f78ef3be9e45e48b99355fccbbd225d7db1`. Шаг 5 собрал на abykovserv
из этого merge exact image
`sha256:fe3a8568b2e1aac2d04824e9438952cb27a94ed4d8cf42007703cd7d130034fd`
без Ansible apply и `/app` bind mount. Внутри image full suite дал
`97 passed in 2.36s`; реальный Uvicorn process на isolated data и
Docker-assigned `127.0.0.1` port вернул healthy и создал preset/free Party.
Обе candidate SQLite прошли integrity/FK, source/image hashes совпали,
а normalized production container fingerprints до и после probe одинаковы.
Механические source и exact-image gate закрыты; human quality acceptance,
production apply, activation и live verification не выполнялись. Evidence:
[clean-only budget](evidence/043-clean-only-budget-2026-09-02.md) и
[mechanical candidate](evidence/043-mechanical-candidate-2026-09-02.md).

**Исторический applied baseline до clean-only candidate.**
Последний IaC apply `2ad61019fcad7693ce620d1f158bcb3353b6eb1b` завершился
успешно. Production RP Gateway сохраняет exact image
`sha256:9321777d9db87da6ac5b2b23c4c085a5d28a51199a90b2ec16d922b4b85295c4`;
его `runner.py`, `provider.py` и `mechanics.py` byte-identical current source при
LF-normalизации. Финальный seeded canary и browser proof прошли на этом image без
bind mount исходников; production RP остаётся инертным с
`RP_REBUILD_ENABLED=false`. В source clean RP schema v8, Narrator,
persisted role jobs и runner подключены к `main.py`, существующим Party API,
реальному provider client и Light GUI за серверным флагом
`RP_REBUILD_ENABLED`. В source флаг включает один World `day-watch-moscow-v2`,
создание Party из preset или полного `free_scenario_seed`, opening/ходы с exact
idempotency и optimistic version, отдельные Relationship/Lore/Memory job и
ручной Administrator `accept/reject`. При включении clean-флага ordinary legacy
RP fail-closed получает `410`. Zero-window C1/O2 оставил production RP Gateway
и Light GUI RP-only, а Training/Showroom передал standalone project на целевой
LAN-only `192.168.1.88:8011`. Acceptance-срез 8 закрепляет public standalone commit
`3804d483452e6082eb2079790cf10d3dcc02107f` и анонимный HTTPS checkout без
GitHub token.

Party неизменяемо связывает Narrator с exact `(profile, provider, base_url,
model, settings)`. Party-scoped BYOK принимается только для этого provider и
endpoint; custom endpoint без exact Party key закрывается до provider call.
Narrator делает один вызов без provider fallback, repair и raw-обёртки. Скрытый,
короткоконтекстный, batch или выведенный профиль нельзя выбрать прямым API.

Runner владеет recovery, startup, cancel, await и shutdown двух отдельных worker
loops. Claims сериализуются SQLite-предикатом; restart и shutdown возвращают
незавершённые job в `pending` без расхода attempts, попытка растёт только после
фактического отказа, а включённая роль с недоступной моделью блокирует startup.

Состав срезов 6–8:

- **добавлено:** clean Party HTTP path, concrete provider adapters, lifecycle
  runner, supervisor трёх ролей, exact Party endpoint/BYOK binding, source/API
  seed свободного Scenario и Light GUI для preset/free Party, ручного retry и
  решений Administrator;
- **изолировано:** ordinary legacy RP API, автотесты, datasets и traces; C1
  удаляет Training/Showroom routes и source из RP-контура, а legacy SQLite,
  state и backups сохраняет как неисполняемые data/forensic artifacts;
- **не добавлено:** универсальная queue-платформа, новый сервис, fallback/repair,
  compatibility adapter, автоматический replay пользовательского текста или
  multi-replica lease;
- **проверено в срезе 8:** applied-image seeded run без bind mount с реальными
  provider/runner, exact duplicate без второго provider call, memory anchors и
  ручное принятие Administrator proposal до следующего prompt. Четыре исхода §3
  прямо предъявил отдельный container probe; применённый image прошёл полный
  suite, а его runner/provider/service-model файлы byte-identical probe-файлам.
  Изолированный browser proof предъявил сохранение failed-текста, same-key retry,
   три role status/model/error/kill-switch карточки и ручные `accept/reject`;
- **clean-only candidate шага 4:** legacy executable поверхность, флаг и
  обе ветки выбора удалены; принятые Party/Auth/BYOK/storage контракты,
  legacy SQLite, state и backups сохранены;
- **исторический disabled source-срез §4–§5:** единый
  `ScenarioSnapshot.player_role`, закрытый `local_overrides.lore_cards`, ровно
  три Lore origin, typed player Lore с `authoring_kind` и явная
  `PlayerCorrection` на существующем service runner; focused набор —
  `63 passed`, полный локальный CI — `674 passed`;
- **ещё не сделано:** exact-image механическая приёмка, blind A/B,
  ручные первые 20 ходов и короткий контрастный старт, настоящая длинная
  RP-партия и полные причинные цепочки Relationships/Lore до последующей сцены.
  Автоматический длинный canary на безопасном fixed-model route пройден на
  изолированном предыдущем candidate. Механические LOC/time gate нового candidate
  закрыты; merge, apply, activation и production live verification ещё не
  выполнены. Полная standalone training-приёмка остаётся внешним gate
  Plan 018.

### Brief среза 8: четыре проверяемых исхода §3

Brief среза 8 обязан проверить и прямо предъявить все четыре исхода:

1. переход pending → running атомарным UPDATE с предикатом статуса;
2. попытка считается по фактическому отказу, а не по захвату;
3. startup/shutdown принадлежат runner'у вместе с cancel и await;
4. у администратора и атомарной служебной модели разные роли и обработчики.

Отдельный removal gate временного ограничения среза 4 также проверен: concurrent
exact duplicates выполняют ровно один provider call и возвращают один committed
turn.

Универсальная queue-платформа не создаётся. Wiki и skills в срезе 6 и каждом
следующем срезе меняются только при изменении внешнего пользовательского,
операционного или авторского контракта. Внутренняя перестановка или ещё не
подключённый компонент сами по себе не являются основанием для документационного
зеркала.

Это решение — единственный анкер ребилда. Отдельные ADR на слои, роли и приёмку
не заводятся: дробление на срезы было одной из причин накопления легаси.

**Границы с уже принятыми решениями.** Training-контур выносится в отдельный
проект по [Plan 018](../plans/018-awareness-showroom-project-split.md); это
решение его не переопределяет и не дублирует. Контракты typed Lore и безопасной
явной correction берутся из
[Decision 042](042-rp-explicit-gm-and-typed-lore-drafts.md) — здесь они не
переписываются, а наследуются (см. раздел «Наследование Decision 042»).

**База проверки.** Исходники и baseline среза 1 привязаны к
`origin/main @ 865a2ef` (`865a2ef66c709d02c3326ca7cf48fa617fba74da`). После
прежнего анкера `ebc0219` изменились зависимости Gateway (`pytest 9.1.1`,
`fastapi 0.141.1`, `pydantic 2.13.4`) и repository validator (1 398 физических
строк). Прежние замеры LOC и времени к этой базе не переносятся: новый baseline
и способ его получения фиксируются в теле PR среза 1. Live-счётчики раздела
Context остаются историческими read-only наблюдениями на применённом
`e069670` 28 августа 2026 года, а не проверкой нового движка.
Срез 3 начат от `origin/main @ 5c87f2b`
(`5c87f2b5621c67d4bcbc77646d1e7e4be882c4f3`); merge, apply и live-проверка
этого среза фиксируются как разные состояния.
Срез 4 начат от `origin/main @ 8c38bbd`
(`8c38bbdf6fa9a0ab41e5324adac2b4a583cf256b`) с тем же разделением source,
merge, apply и live-проверки.

Локальное evidence среза 3: production loader материализует committed V2 World
и все 12 Scenario (четыре старта × три стиля), focused boundary — `20 passed`,
полный Gateway suite — `686 passed`, остальные локальные repository gates —
PASS. В отдельной временной SQLite подтверждены независимые hashes, сохранение
старого snapshot после изменения копии source и fail-closed перепривязка party
ID. Это offline-доказательство нового source/storage boundary, не runtime proof.

Локальное evidence среза 4: focused narrator/memory/source/storage boundary —
`37 passed`, полный Gateway suite — `703 passed`, repository gates и сохранённый
Semantic Acceptance — PASS. Production loader и временная чистая SQLite провели
партию через 66 полных RAW-единиц: при safe coverage 58 хвост равен 9…58, на 59
остаётся 9…59, на 66 сдвигается к 17…66; отказ narrator не изменил 66 сохранённых
единиц и version партии. Две memory revisions в этой проверке применены вручную,
а provider был offline boundary double. Поэтому это доказательство сборки prompt,
fail-closed commit и хранения, но не seeded acceptance шага 9, не реальный
provider/runner и не live UX.

Срез 5 начат от `origin/main @ 56b02c2`
(`56b02c2aec47b7e9dca19218895652ed0c86667e`) и перед публикацией
ребазирован на `origin/main @ bf704a5`
(`bf704a5429295bb83270bce826c416ef9d0622ff`); промежуточные commits меняли
только Awareness-контур. Локальное evidence:
`test_claims_are_atomic_role_specific_and_do_not_spend_attempts` проверяет
атомарный role-specific claim без расхода attempts;
`test_restart_recovery_is_free_and_only_actual_failures_spend_attempts` —
бесплатное восстановление после restart и инкремент только при фактическом
отказе; `test_runner_stop_cancels_awaits_and_requeues_claimed_work` — владение
shutdown, cancel и await самим runner'ом;
`test_administrator_uses_separate_handler_and_manual_owner_scoped_decisions` —
отдельные роль, очередь и handler Administrator;
`test_concurrent_exact_retry_returns_the_single_committed_turn` — ровно один
provider call и один committed turn для concurrent exact duplicates. Focused
source/storage/runner boundary — `54 passed`; полный Gateway suite —
`720 passed`; repository gates и сохранённый Semantic Acceptance — PASS. Это
offline
evidence на boundary doubles: `main.py`, API, реальный provider, Light GUI,
apply и live runtime этим срезом не подключены и не проверены; Wiki и skills не
изменялись.

Срез 6 начат от merged `origin/main @ d8d05b0`
(`d8d05b03245e0b63f288e6f07fa3d5d61edb707d`). Clean source теперь подключён к
`main.py`, Party HTTP API, concrete `ServiceModelClient` adapters и FastAPI
lifespan. Focused source/storage/provider/lifecycle boundary — `84 passed`;
полный Gateway suite после endpoint/BYOK и тогдашних shared-runtime Training
regression fixes —
`750 passed`.
Интеграционные тесты подтверждают preset/free creation только через API, один
provider call/turn для exact duplicate, fail-closed provider error без partial
RAW, owner/version isolation, role-specific supervisor, recovery без расхода
attempts, exact custom endpoint key и две Administrator guidance revisions на
одной gameplay version. Network provider в тестах подменён boundary fake;
реальный outbound call, качество прозы, Light GUI, apply, activation и live
Party не проверены. Wiki обновлена, потому что изменился внешний API и
операционный cutover-контракт; skills не менялись. Server inventory оставляет
`RP_REBUILD_ENABLED=false`.

Срез 7 начат от merged `origin/main @ def3daf`
(`def3daf9730c5bbed50e8eb5b5594d8c9d4701b6`) и merged PR 114 как
`ead598ce039be58c36097f455a0ed4c119ebe31b`. Light GUI использует clean API для
preset/free Party и оставляет Training на retained-контракте. Реальный локальный
smoke на текущем Gateway, runner и чистой SQLite подтвердил opening/turn,
provider failure и ручной same-key retry, stale `409` без replay, три отдельные
роли и решение Administrator владельцем Party. Focused clean RP boundary —
`91 passed`, retained Awareness/Training gate — `9 passed`, все 14 Light GUI
test-файлов и пять GitHub checks — PASS. Это source/local evidence: apply,
production activation и live Party ещё не выполнены. Wiki изменена только из-за
нового внешнего Light GUI/API-контракта; skills не менялись.
Последующий O2 удалил retained Training path из применённого RP Light
GUI/Gateway: training UX принадлежит standalone Showroom на `:8011`. Shape,
HTTP и browser smoke подтверждены; полная training-приёмка остаётся отдельным
gate Plan 018.

Срез 8 начат от merged `origin/main @ bb2562a`
(`bb2562acbbd8526492a6b7f5d045e21428106303`). Этот baseline применён Ansible с
`failed=0`: production-контейнеры healthy, Gateway image прошёл `749 passed, 1
skipped`, Awareness — `150 passed`; `RP_REBUILD_ENABLED=false`, clean RP-БД на
live volume не создана, прежние HTTP/UI-маршруты остались доступны. Это applied
proof инертной поставки, а не активация нового RP и не proof следующей
source-коррекции.

Изолированный candidate использовал ровно applied Gateway image с bind-mounted
diff `provider.py`, отдельные SQLite/data каталоги и loopback port. Из архива
`party_16c210a8a099` в новую Party перенесены только первые 50 committed RAW;
версии 51…66 прошли через настоящий API, runner и provider. Созданы две разные
Party одного World — preset и free Scenario. На corrected route реальные
Relationship, Runtime Lore и Story Memory jobs завершились успешно; memory safe
coverage продвинулся до 53, затем 61. Administrator прошёл своим handler и
отдельной локальной моделью на версиях 56 и 64. Concurrent exact duplicate на
версии 52 дал один committed turn и один Narrator provider call. Отдельный
container probe предъявил все четыре исхода §3: атомарный status-predicate claim,
нулевой расход attempts при claim/restart, runner-owned graceful/SIGKILL recovery
и разные role/handler/call для атомарной модели и Administrator.

До коррекции реальный Qwen route возвращал HTTP 200 с неверными обёртками для
трёх атомарных операций; strict parser отклонил их без нормализации. В candidate
контракт результата и исполняемая Pydantic schema переданы модели явно, а
OpenRouter требует поддержку параметров маршрута. После финального ревью current
Qwen route дал валидные Relationship, Lore и Memory, а non-reasoning Qwen3 30B —
валидный Relationship без несовместимого reasoning-параметра. Полный Gateway
suite на applied-image patched candidate до C1 — `753 passed, 1 skipped`. После
rebase на `origin/main @ 209d312` весь current Gateway source в том же dependency
image прошёл `657 passed, 1 skipped`; focused
provider/service-client/runner/mechanics boundary — `47 passed`. Обе candidate
БД прошли `integrity_check=ok` и foreign-key check; production container, флаг и
данные не менялись. Backup остановленного candidate:
`/srv/backups/rp-stack/decision043-acceptance-run3-20260830T182144Z.tar.gz`,
SHA-256 `9369c8a9744a8f142e5030af52fcd728e92cc9528b150e3ab67baebf561ccd89`.
Поскольку функциональный прогон использовал bind-mounted source diff, он
доказывает реальную границу provider/runner, но не заменяет apply собранного
образа и финальный canary без mount. Wiki и skills не менялись: внешнего
пользовательского или операционного контракта этот срез не изменяет.

Финальный apply `83a90eda9a2465567028e7e58446378e0b10ccc2` завершился
`ok=86 changed=15 failed=0`. Exact production Gateway image
`sha256:a76184880d696df46e654cbefb98c31e1a944ddfa6544e1b384db1a275d98506`
прошёл `657 passed, 1 skipped`, standalone Awareness image — `150 passed`.
RP Light GUI `:8010` остался RP-only, standalone Showroom `:8011` показывает
пять сценариев; оба browser smoke прошли без console errors. Старый Showroom,
три training source path и listener `:18011` отсутствуют. Обе live SQLite имеют
`integrity_check=ok` и ноль foreign-key violations; legacy RP data сохранены,
clean production `rp_engine.db` не создан.

Final canary `seeded-run-4` использовал этот exact Gateway image, отдельные
data/state и стандартные read-only worldpacks/scripts mounts; `/app` не
монтировался. В чистую Party перенесены только первые 50 committed RAW, версии
51…66 прошли через реальный API, runner и provider. Concurrent exact duplicate
v52 дал один turn и один Narrator call. На первой v57 provider ответил HTTP 200,
но Gateway отклонил невалидный strict output с `502` и ничего не закоммитил;
same-key retry закоммитил v57. Все 48 Relationship/Lore/Memory jobs завершились
с `attempts=0`; две memory revisions дали safe coverage 51 и 59. Prompt v66
содержит RAW 17…66, занимает 146698 из 400000 символов hard limit и имеет
`cached_tokens=47104` по provider metric. Atomic roles шли через OpenRouter/Qwen,
Administrator — через local Gemma; реальный proposal принят вручную как guidance
revision 1 и попал в следующий prompt без изменения gameplay v66. Обе canary
SQLite прошли integrity/FK. Остановленный evidence сохранён в
`/srv/backups/rp-stack/decision043-acceptance-run4-20260831T062632Z.tar.gz`,
SHA-256 `e7e9d37069b5d4c6cc2ba0913b5b61cfad177ef94a02a034e069fed13d7de274`.
После canary production container ID/start time и флаг `false` не изменились.
Это закрывает artifact parity и seeded-механику, но не заменяет human blind A/B,
ручные первые 20 ходов, настоящую длинную Party и полную semantic/later-scene
проверку причинных цепочек.

Последующий изолированный `run22` проверил непрерывную механику от opening до
version 66 на новой Party `party_6398e2598135`: 66 уникальных request дали 66
committed turn без пропусков и дублей. Все 198 атомарных job
(Relationship/Lore/Memory, по три на version) и 66 Administrator job завершены;
63 Administrator job сохранили `attempts=0`, а три получили `attempts=1` только
после реального отказа. Memory snapshots построили непрерывную base-chain на
версиях 8, 16, 24, 32, 40, 48, 56 и 64; максимальный сериализованный memory
prompt — 5 836 символов. На v66 Narrator получил memory coverage 64 и точный RAW
tail 9…65; действие игрока присутствовало один раз. Обе SQLite прошли
`integrity_check=ok` и foreign-key check. На отдельном `run20` остановка вернула
running job в `pending` с `attempts=0`, а после restart тот же job был захвачен
с тем же счётчиком; только последующий фактический timeout поднял attempts до 1.
Administrator proposal был принят на gameplay version 8 без её изменения и
вошёл во все следующие Narrator prompts.

Этот прогон выявил отдельный блокирующий дефект маршрутизации: активный
`openrouter/free` выбирал модель случайно и в реальных trace встречались
NVIDIA-authored model id. Поэтому `run22` доказывает механику 65+ ходов, но не
доказывает соблюдение provider policy. Cutover-gate source скрывает
`openrouter/auto`, `openrouter/free` и `nvidia/*` из активного каталога, отвергает
их до provider call, добавляет OpenRouter `provider.ignore=["nvidia"]` и заменяет
операционные defaults на exact DeepSeek/Qwen routes; исторические profile и log
rows не мигрируют и остаются читаемыми. Это изменение внешнего модельного и
операционного контракта, поэтому соответствующие Wiki и deploy-skill обновлены.

Первые fixed-route probes не выданы за приёмку: Qwen и DeepSeek вернули billing
`402`, Gemma free — quota `429`; Dots free успешно дошёл до version 8, затем на
v9 вернул `finish_reason=length` и 353 527 символов, которые Gateway отклонил без
commit. После этого Dots получил только поддержанный reasoning mode `none` и
явный лимит ответа.

Последующий изолированный `run28` провёл новую Party `party_a3a1c666c679` от
opening до version 66 через exact narrator route
`dots-studio/dots-3-note-preview:free`: 66 request/idempotency key дали 66
committed turn без пропусков и дублей. В trace 67 Narrator call: 66 успешных и
один фактический upstream HTTP 400 на v51; Party осталась на v50, а разрешённый
same-key retry после runtime-отказа создал ровно один v51. Неизменённый
отклонённый semantic output не повторялся. Все 66 Relationship и 66 Runtime Lore
job завершились с `attempts=0`; из 66 Story Memory job только один завершился с
`attempts=1`; у Administrator распределение — 63 с `attempts=0`, два с
`attempts=1` и один с `attempts=2`. Каждый счётчик вырос только после фактической
ошибки модели. Memory построила непрерывную base-chain на версиях 8, 16, 24, 32,
40, 48, 56 и 64; coverage не продвигался при отказе v64 и дошёл до 64 только
после успешного retry. Обе candidate SQLite прошли integrity/FK. Архив run8:
`/srv/backups/rp-stack/decision043-acceptance-run8-20260901T092919Z.tar.gz`,
SHA-256 `e36a2eb40b0e559e023f9da3be8e43bca70676443a200f82b3c47ceb3f609ab7`.

Семантический review этого прогона сначала не прошёл gate: v3 ошибочно создал
`kept_agreement` из подтверждения будущей границы, а Runtime Lore повторял уже
сохранённые события и включал тезисы вне выбранных evidence spans. Исправление
осталось модельным, без regex/substrings как предикатов истины: для
`kept_agreement` модель обязана различить ранее существовавшее обязательство и
его фактическое исполнение сейчас; Runtime Lore получает существующие runtime
cards и обязана опираться только на выбранные spans. Реальный `run10` на local
Gemma с текущим source с первого выполнения вернул для v3 `candidates=[]`, для
v1 — одну полностью подтверждённую выбранным span карточку, для повторного v57 —
`no_candidate`. Архив run10:
`/srv/backups/rp-stack/decision043-acceptance-run10-20260901T094026Z.tar.gz`,
SHA-256 `edd01c3b8e90f031caffaa08d749b3028b155f8a54a026308b925d3cd72d2867`.
Full Gateway suite текущего bundle — `665 passed, 1 skipped` за
`66.38s`; repository/skill/schema/UI gates — PASS. Тот же набор из 666 тестов в
PR 126 занял `95.02s` на current GitHub runner. Текущий verification budget —
`28 273 / 5 000 LOC`, debt `23 273`, поэтому LOC и time gates остаются открыты.
Production во всех probes сохранял тот же container/image, restart count `0` и
`RP_REBUILD_ENABLED=false`; candidate data production не использует. Длинный
run28 доказывает автоматическую механику fixed route, но не заменяет human
приёмку и applied-image parity последних source-коррекций.

Read-only causal audit `run28` подтвердил проводку обоих производных слоёв:
полное содержимое Lore card из source version 56 вошло в narrator prompt v57 и
его факты появились в сцене; Relationship cause `shared_risk` из v61 вошла в
prompt v62. Но сцена v62 повторила прежний шаблон и не предъявила отдельного
последствия, а сам `run28` уже отклонён семантическим review. Исправленные
Gemma-probes `run10` выполнены после последнего narrator turn, поэтому они не
дают последующей сцены. Полные причинные gates Relationships/Lore остаются
открытыми.

Последующий изолированный browser proof поднял exact production Gateway image и
Light GUI на временном LAN-порту с сохранённой candidate DB `run11`; production
контейнер, БД и `RP_REBUILD_ENABLED=false` не менялись. На Party
`party_8a986fa0efd2` реальный DeepSeek request `ui_mtk0c8jy_f91s1fin` дважды
получил upstream `402`: оба provider call использовали один `request_id`, Party
осталась на version 8, число turns осталось 8, а текст с SHA-256
`38783e41791130df3f240f21668d38211d95ee3cfbf8a0316af01882991a2509`
остался в поле для ручного retry. GUI показал отдельные Narrator, atomic service
и Administrator model/status/success/error/last-error/kill-switch карточки и
`Принять`/`Отклонить` для pending Administrator proposal; console errors — 0.
После проверки временные GUI, сеть и listener удалены, candidate остановлен.
Архив `/srv/backups/rp-stack/decision043-browser-proof-20260902T112743Z.tar.gz`,
SHA-256 `dc4f482a5704fb9fb73ea3f6873db6532aecf90a679a6e80ad16ab9bd4498847`,
успешно восстановлен во временный каталог: обе SQLite `integrity_check=ok`,
foreign-key violations — 0. Полный applied-image Gateway suite после последнего
Ansible: `665 passed, 1 skipped` за `66.29s`; функционально зелёный, time gate
`≤60s` всё ещё открыт.

### Незакрытый clean contract §4–§5 на baseline `66f0808`

Перед human acceptance требуется ещё один disabled функциональный срез. Read-only
аудит current `main` выявил пять разрывов между принятым контрактом и clean path:

1. **Нет операции `PlayerCorrection`.** В `app/rp/**` нет ни одного упоминания
   `PlayerCorrection`; clean API в `app/main.py:1335-1617` обслуживает Party,
   opening, turn и роли, но не объявляет correction endpoints. Существующий
   `/api/parties/{party_id}/gm-corrections/decide` в
   `app/main.py:3658-3694` остаётся legacy `PartyStore`/`RPGMService` path и не
   является clean implementation.
2. **Нет `authoring_kind` в clean audit.** Clean schema и `app/rp/**` не содержат
   это поле. Сохраняющий `lore_card_created` legacy handler
   (`app/main.py:2078-2093`) записывает card ID, title, source turns и confirm,
   но не kind; на clean Party эти draft/create handlers не переключаются на
   `RPTurnEngine` (`app/main.py:2000-2094`).
3. **Нет Lore origin `scenario`.** Clean read собирает только World cards с
   принудительным `origin=world` и `derived.runtime_lore_cards`
   (`app/main.py:1961-1993`). Storage создаёт производную карту только с
   `origin='runtime'` (`app/rp/turn_engine.py:1508-1530`), а prompt объединяет
   только World и runtime (`app/rp/narrator.py:217-231`).
4. **`local_overrides` сохраняется, но не исполняется.** Поле остаётся открытым
   `dict` в `ScenarioPresetDefinition` и `ScenarioSnapshot`
   (`app/rp/content.py:84-99,121-138`), копируется при preset/free materialization
   (`app/rp/content.py:255-272,275-313`) и принимается clean API
   (`app/main.py:1231-1245,1352-1368`; `app/models/schemas.py:375-391`). Других
   чтений поля в production clean path нет.
5. **V2-пресеты ссылаются на удаляемый `PlayerCharacter`.** Все три пары
   `worldpacks/day-watch-moscow-v2/presets/{book,action,strategic}/gm-system.md:7-8`
   и `authors-note.md:3` ставят эту legacy-сущность выше художественного текста.
   Clean preset creation при этом берёт неизменяемый `player_role` напрямую из
   preset (`app/main.py:1370-1373`), в отличие от free Scenario, где поле можно
   передать (`app/main.py:1352-1368`).

Исправление следует плану
[Plan 029](../plans/029-decision-043-completion.md): единый
`ScenarioSnapshot.player_role`, закрытый `local_overrides.lore_cards`, три Lore
origin, typed player Lore и явная `PlayerCorrection` на существующей atomic
service role/runner. Production остаётся за `RP_REBUILD_ENABLED=false` до
закрытия всех ворот.

**Результат source-кандидата 2026-09-02:** все пять разрывов закрыты за прежним
флагом. Lore draft использует один committed turn, заранее известный
`character|event|location`, immutable job input и отдельный редактируемый
confirm; immutable runtime record сохраняет `authoring_kind`. Correction
сохраняет полный catalog для confirm, передаёт модели только ranked `8 / 4 000`,
поддерживает broad/exact RAW hint, повторно проверяет owner/version/hash/target и
проецирует accepted overlay только в следующую Party version. Focused тесты:
`63 passed`; полный локальный repository CI: `674 passed`. Apply, activation и
live proof не выполнялись; LOC/time gates остаются шагу 4.

### Разбор `run28` / `run10` и порядок следующей приёмки

Причины разделяются, чтобы не исправлять механику вместо harness или наоборот:

| Слой | Что доказано или сломано |
| --- | --- |
| Механика | `run28` дошёл до version 66 на exact route, провёл jobs, retries, memory chain и передал runtime Lore из v56 в prompt/scene v57, а Relationship cause v61 — в prompt v62. Значит storage/runner/prompt wiring существует. |
| Качество кандидатов | `run28` отклонён: v3 ложно создал `kept_agreement`, Runtime Lore повторял сохранённое и выходил за evidence spans; сцена v62 не дала отдельного последствия Relationship. |
| Порядок harness | Исправленные `run10` probes вернули ожидаемые `candidates=[]`, grounded Lore и `no_candidate`, но были запущены после последнего narrator turn. Следующего prompt и последующей сцены уже не существовало, поэтому причинная цепочка не могла закрыться независимо от качества output. |

Следующий acceptance harness обязан выполнить relationship/runtime-Lore probe и
correction **внутри** ещё продолжающейся Party, дождаться deterministic apply,
провести следующий narrator turn и затем отдельный последующий turn для видимого
следствия. Administrator accept/reject, `PlayerCorrection` и player Lore также
выполняются до последней сцены. Post-final probes не засчитываются в §6.3.

Актуальный публичный snapshot цен и endpoint-провайдеров сохранён отдельно:
[043-model-pricing-2026-09-02.md](evidence/043-model-pricing-2026-09-02.md).

## Context

- Партия навсегда закреплена за `rp_contract_revision` и не мигрирует.
  Наблюдаемая ревизия прошла 6 → 7 → 8 → 10 → 11, каждый шаг добавлял ветку, не
  убирая предыдущую. 123 гейта с числовым литералом: 34 недостижимы при ревизии
  11, 89 всегда истинны. По-настоящему неиспользуемых функций во всём `app/` —
  27 строк. Удалять нечего, пока действует обещание совместимости.
- Восемь RP-миров на четырёх ревизиях (6, 7, 10, 11). На живой — один.
- Слой отношений на 17-ходовой партии `day-watch-moscow-v2`: 16 job'ов — 9
  `succeeded`, 7 в терминальном `stale`; 46 завершённых extraction-вызовов дали
  37 непустых event proposals, но **0 применённых extracted causes/events**;
  все 11 `relationship_causes` — seeds.
- Учёт попыток сломан: `mark_service_job_running` (`state_store.py:1020`)
  инкрементирует `attempts` при захвате job'а, а `recover_interrupted_work`
  (`state_store.py:893`) возвращает `running → pending` без уменьшения счётчика.
  **Перезапуски незаконно расходуют бюджет отказов** и могут сделать терминальной
  первую же настоящую ошибку. Терминальный статус при этом присваивается только в
  обработчике исключения (`adjudicator.py:1580`), поэтому одни перезапуски job не
  убивают. `stale` назначается исключительно `relationship_extraction`, у
  которой бюджет по умолчанию 5; `rp_story_memory` завершается как `failed`.
  У семи `stale` сохранены причины: пять `mention_not_in_evidence` и две
  `event_evidence_mismatch`. Сколько из пяти attempts каждого job пришлось на
  перезапуски, без process timeline не установлено.
- Провал семантического валидатора при ревизии ≥7 даёт `RuntimeError` и теряет
  ход целиком. Ошибка транспорта, наоборот, коммитит шаблонные две фразы как ход,
  а в RAW-хвост следующего промта уходит литерал `NON_CANONICAL_SAFE_FALLBACK`
  (`state_store.py:2178`).
- Валидатор отклоняет обычную прозу: слово «я» в любой не-диалоговой строке,
  строки, начинающиеся с «Анализ:», «Рекомендация:», «Диагностика:», и
  подстроки из давнего бага про короля.
- В промт уходит шесть system-сообщений, не 21 (21 — размер реестра). Объём
  растёт не числом блоков, а историей: с 10 до 37 тысяч символов за 17 ходов.
- Три «нарративных пресета» V2 используют байт-идентичный системный промт
  (один md5 на все три плюс легаси-копию); различается только authors-note.
- Проверочный контур рос храповиком. После появления 5 августа 2026 года
  связки Devkit / repository policy / полного CI чистый прирост Python-тестов
  Gateway составил 18 086 строк при 16 507 строках приложения против 5 262 и
  16 551 до неё. Это временная корреляция, а не доказательство вины одного
  hook'а; в поздний период входит и Awareness. Механизм подтверждается
  контрактами репозитория: изменение формата требовало размножить одно правило
  по tests, repository validator, registry, Wiki и skills, а полный Gateway
  pytest запускался независимо от затронутого пути и затем повторялся в образе.
  В текущем дереве десять revision-named test-файлов занимают 5 290 строк;
  `validate-repository.py` вырос со 137 до 1 379 строк. Отдельные проверки
  удалялись, но ни один Python test-файл не был снят целиком. Поэтому запрет
  обратного храповика является частью самого ребилда, а глобальная переделка
  Devkit и CI — отдельной работой.

## Decision

### 1. Три слоя контента

| Слой | Содержит | Изменяемость |
| --- | --- | --- |
| **World** | законы сеттинга, фракции, места, базовые NPC, канон, онтология отношений, seed lore cards | статический источник |
| **Scenario** | игрок, способности, старт, начальное состояние, активный состав NPC, стартовые отношения, стиль, формат, сложность, opening, локальные отклонения | собирается при создании партии, свободно или из пресета |
| **Party** | RAW-ходы, story memory, состояние, отношения, динамический lore, решения администратора | изменяется во время игры |

Раскладка источника:

- `world.json` → `WorldDefinition`;
- `scenario-presets/*.json` → отдельные `ScenarioPresetDefinition`, лежат рядом
  с миром, но **не внутри** `WorldDefinition`.

`WorldDefinition` не принимает `player_role`, `openings`, `presets`,
`state_seed` и `rp_supervisor`. Их наличие — ошибка сборки мира, а не замечание
в инструкции.

При создании партии сохраняются **две независимые пары**: immutable
`world_snapshot` + `world_hash` и `scenario_snapshot` + `scenario_hash`. Без
раздельной фиксации обновление исходного World незаметно изменит уже начатую
партию. Отдельная таблица `scenarios` не заводится. Свободная сборка и пресет
проходят одну и ту же материализацию. Party-local дрейф разрешён; запись в
исходный World запрещена любой роли.

### 2. Три модельные роли и права записи

| Роль | Синхронность | Может писать |
| --- | --- | --- |
| **Нарратор** | синхронно, один вызов на ход | ничего; возвращает только текст сцены |
| **Служебная модель** | асинхронно, атомарные задания | производные структуры через детерминированное применение |
| **Администратор партии** | асинхронно, отдельная модель и очередь | versioned proposals; применение только по allowlist типов |

RAW-ходы, реплики игрока и исходный World не переписывает никто и никогда.

Нарратор не возвращает JSON-бандл. Repair-петля, семантический валидатор прозы и
шаблонная сцена шлюза удаляются: ошибка провайдера ничего не коммитит и
возвращает игроку понятную ошибку с retry.

Администратор стартует в режиме `suggest`. **Режим `suggest` обязан включать
ручные `accept` и `reject` в API и в GUI** — без них роль анализирует, но партию
не корректирует, и функциональные ворота (раздел 6) непроходимы.
`apply_allowlisted` включается по типам правок отдельно и только после
наблюдения на реальной партии. Первый пробный запуск и каданс — **настройки
пилота, а не архитектурный инвариант**; значения выбираются до приёмки так, чтобы
роль была наблюдаема на первой же реальной партии, но в контракт не вшиваются.

Полезная инфраструктура нынешнего `RPSupervisorService` переиспользуется: window
hash, evidence turn ids, идемпотентность, expiry, fail-open. Контракт
переписывается. Наблюдаемость расширяет существующую поверхность
(`rp_supervisor_evaluations`, `GET /api/parties/{id}/supervisor`, экран в GUI) до
трёх ролей; новая платформа телеметрии не строится.

### 3. Изоляция от старого движка

`RPTurnEngine` не наследует `Adjudicator`, его поля уровня класса и его базу.

Требования к новому runner'у:

- переход `pending → running` атомарен, с предикатом статуса в самом `UPDATE`;
- попытка считается **по фактическому отказу**, а не по захвату: перезапуск
  процесса не расходует бюджет;
- startup и shutdown принадлежат runner'у, включая cancel и await;
- у администратора и у атомарной служебной модели разные роли и обработчики;
- универсальная queue-платформа не создаётся.

### 4. Разрешённое содержимое

Единственный RP-мир — точный slug `day-watch-moscow-v2`. Уникальный материал
переносится целиком: канон, персонажи, lore cards, четыре старта, три стиля.
Существующие партии удаляются; из партии сохраняется только приёмочный материал
(шаг 2).

Динамические lore cards получают три происхождения: `world`, `scenario`,
`runtime`. Runtime-карта никогда не записывается обратно в World.

Извлечение отношений переводится на стабильные `character_id` и нумерованные
evidence spans с независимой проверкой каждого кандидата: один плохой кандидат
больше не уничтожает пакет хода. Постановка job'ов чинится отдельно.

### 5. Наследование Decision 042 (typed Lore и безопасная маршрутизация GM)

Контракты не дублируются — берутся из Decision 042 и действуют в шагах 6–8:

- `kind` обязателен (`character` / `event` / `location`);
- плоские strict-схемы с дискриминатором `result`;
- `reasoning.enabled=false` плюс pre-flight canary;
- квота и рейтинг только в `patch_payload`; каталог `confirm` — полный;
- симметричная ветка `broad-hint`;
- audit `lore_card_created` и `authoring_kind`.

Дополнительно сохраняется принцип Decision 042: модель выбирает только bounded
slot, а immutable target и `before` принадлежат Gateway; никакой classifier не
может сам открыть мутирующий correction flow.

Отличия от 042: staged rollout по revisions 8–12, `channel=auto`, `gm_intent` и
`route_required` не наследуются; явная операция называется `PlayerCorrection` и
реализуется сразу в новом движке без compatibility-ветки. `detail_level` и другие
настройки формата становятся частью `ScenarioSnapshot`, а не отдельной
настройкой старой партии. Происхождение Lore расширяется до трёх значений (раздел
4), и `runtime`-карты создаются служебной моделью автоматически, а не только
через подтверждаемый игроком черновик.

### 6. Функциональные ворота сохраняемых механик

Наличие записей ничего не доказывает. Каждая сохраняемая механика проходит
причинную цепочку целиком:

1. RAW → relationship candidate → применение → следующий prompt → последующая
   сцена;
2. RAW → runtime lore с provenance → релевантный следующий prompt;
3. RAW → proposal администратора → ручной accept/reject → новая версия →
   следующий prompt.

### 7. Что удаляется

Ревизии 0–11 и вся совместимость. RP Scene State. D20, `/check`, RP-вызовы
`IntentParser` и `RuleEngine`. Семантический валидатор прозы, repair-процедуры,
шаблонный fallback. Substring-механизм `forbidden_claims` — твёрдые законы World
остаются положительными фактами в промте нарратора. `channel=auto`, `gm_intent`
и модалка `route_required`; явный канал коррекции игрока сохраняется под именем
PlayerCorrection. Legacy `MemorySummarizer` и параллельные memory-эндпоинты.
Восемь single-campaign эндпоинтов `/api/state*` и `/api/world*`. Старый
supervisor после переноса полезной логики. Пять полных state seed, root-алиасы,
четыре копии GM-промта и SillyTavern export внутри V2. Exact-prompt snapshots,
ревизионная матрица, фикстуры удалённых миров, тесты внутренних процедур.
38 startup-миграций. Mock-код `mock://` из девяти продакшн-модулей.

**Легаси удаляется по мере замены, а не одним слепым срезом:** каждый удаляемый
модуль уходит вместе со своими тестами в том же изменении, которое вводит его
замену.

Компоненты, общие с training-контуром, удаляются из RP-источника zero-window
поставкой по Plan 018, не этим решением. Legacy SQLite rows/tables, state и
backups этот внешний cleanup не удаляет.

`incident-50` не является исключением: он удаляется вместе с остальными старыми
RP-мирами. Формулировка Plan 018 о том, что он остаётся только в RP project,
описывает владельца до финального RP purge, а не бессрочное сохранение мира.

## Процессные инварианты ребилда

Эти правила действуют с **первого implementation PR**, а не после завершения
нового движка.

1. **Замена заканчивается удалением.** Каждый implementation PR сообщает три
   списка: добавлено, удалено, временно оставлено. Если старый модуль уже заменён
   в новом пути, его код, тесты, fixtures и специальные validator-ветки уходят в
   том же PR. Временно оставить старое можно только со ссылкой на конкретный
   последующий шаг этого решения; бессрочного `TODO cleanup` нет.
2. **Legacy-сюита заморожена.** Для старого `Adjudicator`, ревизий 0–11,
   удаляемых миров и старого prompt contract новые тесты и validator-правила не
   добавляются. Новый `RPTurnEngine` не получает revision flag, compatibility
   branch или параллельный endpoint «на время».
3. **Тест допускается только по риску.** Автоматическая проверка защищает одно
   из следующего: видимый игровой исход, потерю или смешение данных, atomicity,
   isolation, idempotency/recovery, безопасное применение proposal либо
   реальную границу provider/storage. Exact prompt text/hash/order, расположение
   функций, наличие строк в исходнике, снятый мир/ревизия и синхронизация prose
   между docs/skills тестами не закрепляются.
4. **У новой RP-сюиты есть жёсткий бюджет.** К cutover весь автоматически
   запускаемый RP-only test/eval/validator code вместе с test helpers занимает
   не более **5 000 физических строк**, а один полный RP-прогон на текущем
   GitHub runner укладывается в **60 секунд**, focused local check — в
   **30 секунд**, и тот же полный прогон не повторяется во втором образе.
   В бюджет не входят Awareness, production loader/schema, общие security/deploy
   checks, ручная/live-приёмка и ровно 12 blind A/B anchors. `scripts/ci.ps1`
   печатает консервативный текущий LOC/debt и время mixed Gateway pytest; общие
   файлы считаются в RP до физического разделения. До cutover это показометр, а
   не блокировка; отдельный LOC-validator для контроля бюджета запрещён.
5. **У формата один исполняемый источник истины.** World/Scenario проверяет
   production loader/schema и его компактные boundary-тесты. World-specific
   маркеры не копируются в `validate-repository.py`. Wiki и skills меняются
   только при изменении внешнего пользовательского или операционного контракта,
   а не после каждой внутренней перестановки.
6. **Гейт соответствует этапу.** В обычном PR запускаются focused checks
   изменённой границы. Полный новый RP-прогон нужен перед merge изменения
   runtime/storage, а container, blind A/B, seeded memory и живая партия — на
   интеграции и cutover. Registry и readiness-словарь могут описывать результат,
   но не создают новый blocking gate. CI никогда не считается доказательством
   качества игры.

Эти инварианты не требуют нового процесса контроля: их проверяет ревью по diff и
приёмочным evidence этого решения. Автоматизировать сами инварианты ещё одним
validator'ом запрещено.

### Оставшийся глобальный трек упрощения

Общее определение готовности, симметричное удаление, один исполняемый владелец
контракта и видимый бюджет действуют постоянно, а не как исключения на время
ребилда. Отдельному глобальному треку остаются path-aware GitHub CI,
дублирование policy-hook и сжатие `validate-repository.py` до границ
репозитория, безопасности и деплоя. Сжатие выполняется после шага 5, когда
появятся production loader/schema: до этого контентные проверки некуда
переносить. Эти пункты не являются основанием добавлять в ребилд зеркальные
tests, guards, registry entries, Wiki-страницы или skills.

## Порядок исполнения

Игры во время переноса не будет; safety patch на старом движке не делается.
Training-контур идёт параллельно по Plan 018 и здесь не планируется.

1. **Начать исполнение в отдельном worktree от актуального `origin/main`.**
   Грязный основной checkout и его локальные untracked-копии не переносятся в
   ветку ребилда и не считаются источником решения.
2. **Снять полный evidence-архив.** Не выборка, а immutable party-closure:
   Party, Campaign, Branch; turns, requests, state versions и state-файлы;
   prompt/response/metadata; service jobs и service calls; audit и Turn Trace;
   relationships; lore; memory snapshots; supervisor evaluations; модель,
   параметры вызовов и source revision; manifest с ID, количеством строк и
   хэшами. Snapshot обязан открываться read-only; на нём воспроизводятся запросы
   из evidence manifest 042. Отдельно фиксируются время экспорта, SHA-256
   snapshot и applied SHA сервера.
   Охват:
   - `party_cac70558b50a` — партия `day-watch-moscow-v2`: первые 17 ходов дают
     baseline прозы V2; полный архив среза 1 сохраняет все 19 текущих ходов
     (состав подтверждён владельцем). Полная typed Lore chain Decision 042:
     call `566` → card `286` →
     повторное попадание в последующие prompt;
   - `party_30fd9d3cc6ef` и `party_3e09b9092765` — остальные evidence-партии
     механизмов Decision 042;
   - `party_16c210a8a099` — длинная партия «Староста»: 168 committed RAW units
     (opening и 167 последующих игровых ходов), probe-цель registry 020/021, но
     не evidence typed Lore или explicit correction. Из этих четырёх партий
     только она пересекла текущие границы памяти `W=50`, `W+A=58` и `W+2A=66`.
     Её committed RAW сохраняется полностью, не курируется и не обрезается; это
     источник для seeded memory run.
   Архив хранится **вне Git**, с ограниченным доступом, и переживает purge. Его
   удаление возможно только после появления replacement evidence сохраняемых
   механик и отдельного решения владельца о сроке хранения. В Git идут ровно 12
   обезличенных anchors для blind A/B — иначе «полный дамп» создаёт ещё одно
   бессрочное хранилище приватных промтов. Для остальных fixtures курируется
   минимальный набор разных failure modes без заранее заданной квоты.
3. **Разобраться с registry перед любым удалением.** `registry/020.yml` и
   `registry/021.yml` пинят `party_16c210a8a099` как probe-цель. Два текущих
   требования уровня `наблюдается` понижаются до `подключено`;
   `probe_command` и `expected` обнуляются, а waiver объясняет замену механизма
   этим решением. На новую партию probe не перепривязывается: Decision 043
   заменяет старый relationship-механизм, а не продолжает его. Отсутствующий
   source Decision 021 искусственно не создаётся. До purge evidence manifest 042
   получает ссылку на immutable archive manifest. Новая причинная цепочка
   отношений получает собственное evidence только после приёмки нового движка.
4. **Зафиксировать тонкий контур проверки до первого изменения продукта.**
   Снять baseline RP-only test/eval/validator LOC и времени полного прогона;
   заморозить legacy-сюиту и применять процессные инварианты выше. Это измерение
   записывается в первый implementation PR и не превращается в новый script,
   registry или gate.
5. **Создать чистую RP-схему и offline `RPTurnEngine`.** Старый RP остаётся на
   месте и неиспользуемым.
6. **Ввести World/Scenario и заменить авторский контракт.** Добавить
   `WorldDefinition`, `ScenarioPresetDefinition`, `WorldSnapshot` и
   `ScenarioSnapshot`; перенести V2 без слоя совместимости со старым форматом
   WorldPack. Исполняемая схема и loader становятся источником истины; напрямую
   используемая авторская инструкция `rp-world-pack-builder` обновляется по
   результату — что является законченным World, Scenario и интересным игровым
   исходом. Старые противоречащие правила удаляются, но их содержимое не
   размножается по repository standard, plugins и repository validator.
   Новый формат World/Scenario не описывается в `validate-repository.py`: его
   единственный исполняемый владелец — production loader/schema и компактные
   boundary-тесты этой границы. `RP_CONTRACT_SOURCE_MAX_REVISION` и все ветки
   ревизий 0–11 остаются легаси до удаления старого монолита на шаге 12. Если
   новый путь сталкивается с revision-проверками, запрещено дописывать в
   валидатор ветку нового формата: новый путь исключается из легаси-проверок.
7. **Собрать narrator path и память.** Сохранить полезную структуру V2: порядок
   промта, секционную память, RAW-tail, cache-stable prefix. Содержательная
   статика поступает только из `WorldSnapshot`, настройки опыта — из
   `ScenarioSnapshot`, изменившаяся история — из Party. Gateway собирает и
   маршрутизирует эти слои, но не добавляет собственные RP-факты, дублирующие
   system-блоки или скрытые правила канона.
8. **Подключить сохраняемые механики разными маршрутами.** Relationships и Lore
   обрабатывает атомарная служебная модель. Administrator использует отдельную
   модель, очередь и versioned proposal-flow с ручным `accept/reject`.
   **Правило brief:** Wiki/skills меняются только при реальном внешнем
   пользовательском/операционном контракте, не при внутренней перестановке.
9. **Провести RP-приёмку** (критерии ниже): seeded-механика, затем настоящая
   RP-партия.
   **Правило brief:** Wiki/skills меняются только при реальном внешнем
   пользовательском/операционном контракте, не при внутренней перестановке.
10. **Закрыть внешний Awareness-gate по Plan 018.** До cutover выполняется только
    shadow smoke отдельного проекта на пустой стартовой БД. Затем один apply
    переключает Showroom на `:8011` с rollback window `0` и удаляет training-код
    и старые source declarations из активного RP source. Полная live-приёмка
    обоих курсов выполняется уже после этого cutover на production endpoint;
    внешний gate закрывается только после неё. Legacy data сохраняются, а
    дальнейший независимый RP cleanup до закрытия gate запрещён.
    **Правило brief:** Wiki/skills меняются только при реальном внешнем
    пользовательском/операционном контракте, не при внутренней перестановке.
11. **Переключить Light GUI на новый RP Gateway.**
    **Правило brief:** Wiki/skills меняются только при реальном внешнем
    пользовательском/операционном контракте, не при внутренней перестановке.
12. **Удалить старый RP-монолит последней операцией.** Удаляются старый RP-код,
    source-only state templates, снятые миры и активная привязка к старому
    смешанному хранилищу. Новая RP-БД остаётся чистой от старых партий. Mutable
    legacy RP SQLite/state/backups и сохранённые Awareness data artifacts не
    мутируются destructive SQL и не удаляются: их retention принадлежит Plan
    018. Ни один предыдущий шаг ничего из этого не удаляет.
    **Правило brief:** Wiki/skills меняются только при реальном внешнем
    пользовательском/операционном контракте, не при внутренней перестановке.

### Прополка после cutover

Владелец — пользователь. Срок — первый понедельник после завершения шага 12.
Метрика результата — снятые осиротевшие правила: каждый удалённый guard или
проверка, у которых больше нет потребителя. Прополка повторяется при превышении
бюджета процессного инварианта 4 и не автоматизируется отдельным validator'ом.

## Критерии готовности

Каждый пункт — проверяемый исход, а не намерение.

1. `GET /api/worldpacks` RP-шлюза возвращает ровно один пакет:
   `day-watch-moscow-v2`. В новой RP-базе нет ни одной партии старого контура.
   Awareness проверяется через отдельный проект/API; его новая SQLite до
   собственных прогонов пуста и не входит в RP-приёмку.
2. Из одного мира создаются две принципиально разные партии: одна из пресета,
   вторая из свободно собранного сценария.
3. **Проза:** blind A/B на 12 anchors — та же модель нарратора, те же параметры,
   читает человек. Критерии: агентность игрока, причинность, голоса NPC,
   конкретность мира, развитие сцены, темп, повторы, соответствие стилю
   сценария. Любой puppeteering, противоречие, мета-утечка или служебная вставка
   в выводе — провал.
4. **Continuity:** ручная проверка первых 20 ходов живой партии плюс короткий
   второй старт с контрастным сценарием.
5. **Память.** Партия продолжается до `W + 2A`, где `W` — окно RAW-хвоста, `A` —
   шаг якоря. При нынешних 50 и 8 это 66 единиц. Порог не фиксируется числом:
   меняется окно — пересчитывается формула. Проверяется не «прекращение роста
   промта» — сцены, lore и отношения имеют разную длину, — а:
   - safe coverage продвигается монотонно;
   - RAW сдвигается ровно на `W + A` и `W + 2A`;
   - RAW остаётся в диапазоне `W … W + A - 1` плюс весь uncovered tail;
   - каждый не-RAW слой укладывается в свой бюджет;
   - собранный промт помещается в hard input budget;
   - stable prefix hash не меняется при неизменной статике;
   - `cached_tokens` — обязательный gate только там, где провайдер эту метрику
     отдаёт.

   В seeded-прогоне засеивается **только committed RAW**. Memory snapshots и
   job'ы не засеиваются; последние `2A` единиц проходят через настоящий API,
   runner и модель. Seeded-прогон — внутренний цикл разработки; настоящую
   длинную партию перед cutover он не заменяет.
6. Каждая из трёх причинных цепочек раздела 6 пройдена целиком и предъявлена.
7. Ход не коммитится при ошибке провайдера; текст игрока остаётся в поле; retry
   доступен. Выключенная служебная модель ход не блокирует.
8. Перезапуск шлюза с непустой очередью не теряет фоновые задачи и **не
   расходует бюджет отказов**.
9. Исходный World не изменён ничем из перечисленного; `world_hash` партии не
   меняется при обновлении источника мира.
10. У каждой из трёх ролей видны модель, состояние, success/error, последняя
    ошибка и kill switch.

Правило для тестов: тест живёт, только если защищает видимое поведение игрока,
потерю данных, безопасность применения правок или реальную границу с провайдером
и хранилищем. Проза проверяется человеком в реальной партии, а не новым
LLM-валидатором. Зелёный pytest и зелёный CI доказательством работоспособности
игры не являются.

## Non-goals

- Не чинить дефекты старого `Adjudicator` перед его удалением.
- Не переопределять и не дублировать Plan 018: перенос training-контура,
  пустая стартовая база нового проекта и его cutover планируются там.
- Не создавать второй Docker-сервис для RP, микросервисы, ORM, event bus и
  repository-фреймворк.
- Не переносить старые партии в новый контракт.
- Не строить универсальный фреймворк миров и отдельный scenario-builder:
  произвольный сценарий собирается в party wizard.
- Не рефакторить `main.py` ради красоты; разбивать только по реальным функциям —
  party setup, turn execution, derived jobs, administrator.
- Не создавать новые skills, сервисы и уровни абстракции без второго реального
  use case.

## Related decisions

Наследует контракты [Decision 042](042-rp-explicit-gm-and-typed-lore-drafts.md)
(typed Lore, безопасная маршрутизация GM). Разделяет границу с
[Plan 018](../plans/018-awareness-showroom-project-split.md): training-контур
уходит в отдельный проект, RP-шлюз становится RP-only.

Вытесняет RP-runtime часть решений 024, 026, 028, 029, 030, 031, 032 и снимает
ревизионные границы, введённые 037, 038, 039, 040, 041 — сохраняя их продуктовые
идеи: авторские и динамические lore cards, явный канал коррекции игрока,
опциональные часы мира как capability сценария, надзор за ведением как отдельную
роль администратора, авторские стили и старты как часть сценария. Решение 019
исполняется в полном объёме: `OutputValidator` и шаблонный fallback уходят из
RP-пути целиком, а не только для ревизии 7. Ceremony решения 022 выводится из
блокирующего пути разработки; словарь готовности остаётся допустимым при
описании поставки.

Registry-файл для этого решения не заводится. Записи `020.yml` и `021.yml`
обрабатываются отдельно на шаге 3.
