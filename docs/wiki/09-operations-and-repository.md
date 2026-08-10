# Эксплуатация и карта репозитория

[← Данные и безопасность](08-data-and-security.md) · [Главная](README.md)

## Модель доставки

RP Stack развёртывается pull-based. GitHub не подключается к домашнему серверу.

```mermaid
flowchart LR
    W["Windows workspace"] -->|"commit + push branch"| BR["GitHub codex/*"]
    BR -->|"non-draft PR + green CI + merge"| GH["GitHub main"]
    GH -->|"read-only deploy key + git pull --ff-only"| CO["/opt/ubuntu_ansible_palybooks"]
    CO -->|"Ansible localhost"| APP["/srv/apps/rp-stack"]
    APP -->|"docker compose up"| RT["Running containers"]
    DATA["/srv/app-data/rp-stack"] --> RT
```

Репозиторий приватный. Сервер читает его по SSH с отдельным read-only deploy key;
приватная часть ключа хранится только на `abykovserv`, не попадает в Git и не
используется для push.

Обычный цикл:

1. изменить source/IaC в `codex/`-ветке или изолированном worktree;
2. выполнить статические и focused checks;
3. сделать commit и push только рабочей ветки;
4. открыть non-draft PR, дождаться зелёного CI и смержить его в `main`;
5. пользователь интерактивно запускает на `abykovserv`
   `sudo systemctl start ansible-local-apply.service`;
6. проверить journal, containers, pytest и HTTP/UI.

Прямой push в `main` запрещён; готовую к merge работу нельзя оставлять в ветке.

Команда Ansible прогоняет весь `site.yml`, но роли идемпотентны. Apps role отслеживает изменённые app artifacts, а Docker Compose не должен пересоздавать неизменный сервис без изменения image/config.

## Статусы доставки

Это разные утверждения, которые нельзя сворачивать в одно:

| Статус | Что доказано |
|---|---|
| Локально готово | Файлы изменены и проверены в workspace |
| Закоммичено | Есть Git commit |
| Запушено | Commit доступен в GitHub |
| Ansible применён | Пользователь ввёл sudo-пароль интерактивно, сервер подтянул revision и apply завершился |
| Container/HTTP/browser verified | Соответствующий runtime-уровень проверен отдельно |

Push не означает deploy, а healthy containers не доказывают корректный пользовательский сценарий.

## Codex devkit, worktrees и CI

Репозиторий содержит собственный Codex-контур:

```text
AGENTS.md                                      repository authority и delivery rules
.codex/config.toml                            project hooks
.codex/hooks.json                             PreToolUse policy
.agents/plugins/marketplace.json              repo-scoped plugin catalog
plugins/rp-stack-devkit/                      skill, read-only MCP/CLI, hooks, checklist
plugins/rp-stack-devkit/.mcp.json             объявление read-only MCP rp-stack-ops
scripts/ci.ps1                                единый локальный deterministic gate
scripts/run-rp-stack-evals.ps1                offline/provider/browser eval entrypoint
.github/workflows/ci.yml                      GitHub Actions parity gate
docs/repository-work-standard.md              короткий проверяемый контракт окружения
scripts/sync-codex-skills.ps1                 repo -> installed skills check/install
```

`.codex/config.toml` содержит только `[features] hooks = true`; MCP объявлен
плагином через `plugins/rp-stack-devkit/.mcp.json`, а не через project
`[mcp_servers]`. Канонические standalone-скиллы находятся в `codex-skills/`;
копии в `%USERPROFILE%\.codex\skills\` применяются командой
`powershell.exe -File scripts/sync-codex-skills.ps1 -Mode Apply` и не
редактируются отдельно. После синхронизации или обновления плагина нужна новая
задача Codex.

На проверенной Windows-машине SSH использует явный ключ
`~/.ssh/id_ed25519_codex_abykovserv`; `ssh-agent` остановлен и отключён. Devkit
передаёт ключ через `-i`. `sudo -n` на сервере не проходит, поэтому Codex
останавливается на статусе `merged`, а apply запускает пользователь
интерактивно без передачи пароля в задачу.

Каждая независимая задача выполняется в `codex/` branch или отдельном worktree.
Codex пушит только рабочую ветку, открывает non-draft PR, дожидается зелёного CI
и сам мержит PR в `main`; прямой push в `main` запрещён, а готовая работа не
остаётся лежать в ветке.
Scheduled проверки также получают отдельный worktree и работают report-only:
они не merge/push/deploy/restore и не меняют живые Party без нового явного
запроса.

Локальный `scripts/ci.ps1` проверяет JSON, Wiki links/fences, AGENTS/hooks/plugin,
state и training contracts, workflow scripts, Python syntax, JS syntax/tests и
полный Gateway pytest. GitHub Actions повторяет эти контракты на чистом runner и
добавляет `ansible-playbook --syntax-check`. Dependabot раз в неделю проверяет
GitHub Actions и Gateway Python dependencies.

`rp-stack-ops` — read-only интерфейс диагностики, а не альтернативный deploy
path. Публикация и apply остаются намеренно отдельными действиями. Для ручного
CLI:

```powershell
powershell.exe -File scripts/rp-stack-ops.ps1 -Action local_revision
powershell.exe -File scripts/rp-stack-ops.ps1 -Action compose_status
powershell.exe -File scripts/rp-stack-ops.ps1 -Action gateway_test -Scope training
```

Provider canary запускается только с `-ConfirmProviderRun`, использует Gateway
autotest branch и проверяет, что source history/state не изменились. Полные
правила трёх уровней находятся в
[RP Stack evals](../../roles/apps/files/rp-stack/evals/README.md).

## Серверные команды

```bash
sudo systemctl start ansible-local-apply.service
sudo systemctl status ansible-local-apply.service --no-pager -l
sudo journalctl -u ansible-local-apply.service -n 100 --no-pager
```

Проверка RP Stack:

```bash
cd /srv/apps/rp-stack
docker compose ps
docker compose logs --tail=100 rp-gateway rp-light-gui rp-showcase-gui
docker compose run --rm rp-gateway pytest
curl -fsS http://192.168.1.88:8010/health
curl -fsS http://192.168.1.88:8010/api/worldpacks
curl -fsS http://192.168.1.88:8011/health
```

Для UI-изменений дополнительно проверяются authenticated DOM, фактические API responses и применённая server revision.

## Live verification интерактивных training artifacts

Snapshot от 31 июля 2026 года для revision `8b8a8fe`:

| Проверка | Результат |
|---|---|
| Server checkout | `/opt/ubuntu_ansible_palybooks` указывает на `8b8a8fecdef857ed0d0acbcd183a742aa09c2227` |
| Ansible | `Result=success`; recap: `ok=65`, `changed=6`, `unreachable=0`, `failed=0` |
| Контейнеры | Gateway, Light GUI, Showroom и Local LLM в состоянии `healthy` |
| HTTP/API | Light GUI и Showroom вернули `200`; публичный список Showroom-сценариев вернул `200`; защищённый `/api/worldpacks` без сессии ожидаемо вернул `401` |
| Gateway tests | Полный контейнерный прогон: `123 passed`, `3 failed`; focused artifacts: `5 passed` |
| Static UI assets | Shared JS/CSS вернули корректные MIME types; CSP запрещает внешние формы, frames и objects |
| Browser | Showroom и авторизованный Light GUI загрузились без console errors; Showroom отобразил письмо, artifact trigger и DOM-собранный credential-form |
| Privacy | Синтетические значения полей не появились в Gateway DB; сохранились только field IDs |
| Scoring | `link_opened`, `credentials_submitted` и `site_closed` потреблены следующим ходом и отражены в canonical evidence/counters |

Три падения полной suite воспроизводят существующий baseline вокруг Awareness safe fallback и счётчика autotest fallback; focused suite новой функции чистая. Во время браузерного прогона один narrator-вызов OpenRouter вернул `403`, после чего training safe fallback сохранил HTTP `200`, authored surface и дальнейшее deterministic scoring. Это operational warning провайдера, а не потеря artifact event.

Live-прогон использовал существующую тестовую Showroom-партию и продвинул её до пятого authored хода; это намеренная тестовая запись в persistent history.

## Карта репозитория

```text
ubuntu_ansible_palybooks/
├── inventories/local/group_vars/server.yml
├── playbooks/site.yml
├── roles/apps/
│   ├── tasks/main.yml
│   ├── templates/rp-stack.compose.yml.j2
│   ├── templates/rp-stack.env.j2
│   └── files/rp-stack/
│       ├── rp-gateway/
│       ├── rp-light-gui/
│       ├── rp-showcase-gui/
│       ├── ui-shared/
│       ├── worldpacks/
│       ├── state/schema.json
│       ├── docs/decisions/
│       └── scripts/
├── codex-skills/
│   ├── abykovserv-iac-deploy/
│   ├── rp-world-pack-builder/
│   └── training-world-pack-builder/
├── plugins/rp-stack-devkit/
├── .codex/
├── .github/workflows/ci.yml
├── AGENTS.md
├── scripts/ci.ps1
├── scripts/rp-stack-ops.ps1
├── scripts/run-rp-stack-evals.ps1
└── docs/wiki/
```

## Gateway service map

| Файл | Ответственность |
|---|---|
| `app/main.py` | FastAPI composition, auth guards, public/party/admin endpoints |
| `services/party_store.py` | WorldPack registry, characters, profiles, parties, branches, autotests, dataset labels |
| `services/state_store.py` | State versions, turns, requests, checks, memory, journal, lore, audit, patches |
| `services/adjudicator.py` | Транзакционный pipeline хода и service jobs |
| `services/rule_engine.py` | Детерминированные исходы для режимов |
| `services/narrative.py` | Provider calls, prompt assembly, cache controls, model fallback |
| `services/validator.py` | Проверка narration и training debrief |
| `services/memory.py` | Immutable episodic chapters |
| `services/rp_story_memory.py` | RP-only cumulative living-memory snapshots и service-model update |
| `services/character_retrieval.py` | Выбор релевантных NPC без embeddings |
| `services/world_instructor.py` | Draft/preview/apply контракт изменения мира |
| `services/auth_store.py` | Users, sessions, provider keys, global settings |
| `services/showroom.py` | Scenarios, visitors, runs, portal snapshots, leaderboard |
| `services/training_artifacts.py` | Blueprint validation, party snapshots, idempotent events и public views |
| `services/autotest.py` | Ограниченный auto-player client |
| `services/service_models.py` | Глобальный service-model catalog/runtime |

## Где менять типовые функции

| Задача | Основные места |
|---|---|
| Новый endpoint | `rp-gateway/app/main.py`, schemas и tests |
| Изменить обработку хода | `adjudicator.py`, `rule_engine.py`, `validator.py` |
| Изменить prompt/memory | `narrative.py`, `memory.py`, `rp_story_memory.py`, `context_budget.py`, `state_store.py` |
| Изменить Light GUI | `rp-light-gui/index.html`, `app.js`, `styles.css` |
| Изменить Showroom | `rp-showcase-gui/` и `showroom.py` |
| Изменить training artifacts | `training_artifacts.py`, `ui-shared/`, оба UI и WorldPack contract |
| Новый RP/novel мир | `worldpacks/<slug>/` и `rp-world-pack-builder` |
| Новый training мир | `worldpacks/<slug>/` и `training-world-pack-builder` |
| Runtime/env/ports | `server.yml`, Compose/env templates |

### Зависимость training workspace

Decision 015 не добавляет новый контейнер, но runtime-файлы доставляются обычным
Ansible apply. Реализация двух capability-флагов затрагивает Gateway schemas/ShowroomStore,
snapshot run, `TrainingArtifactService`, логический
`TrainingWorkspaceService`, StateStore, Showroom UI, shared safe renderers,
training builder contract и четыре комбинации тестов.

Новый контейнер не нужен для обычных JSON-файлов. Отдельный asynchronous worker
и новые persistent data paths допускаются только если будет одобрена загрузка и
предварительная конвертация реальных Office/PDF-ресурсов; это отдельное IaC
изменение, а не часть latency path открытия файла.

- [Decision 015](../../roles/apps/files/rp-stack/docs/decisions/015-training-scenario-interaction-capabilities.md)
- [Plan 015](../../roles/apps/files/rp-stack/docs/plans/015-training-scenario-interaction-capabilities.md)

## Проверки

Минимальный локальный набор для documentation-only change:

```powershell
git diff --check
```

Для кода Gateway:

```bash
python -m compileall rp-gateway/app
pytest
```

Единый local parity gate из корня репозитория:

```powershell
powershell.exe -File scripts/ci.ps1
```

Для статических UI:

```bash
node --check app.js
node character-editor.test.js
node structured-content.test.js
node training-artifacts.test.js
```

Windows может не иметь runtime dependencies. Авторитетный Python test run — внутри rebuilt `rp-gateway` container на сервере. Локальный `compileall` или JS syntax check не является live proof.

Для WorldPack обязательны parse всех JSON и schema validation:

```powershell
python scripts\validate-state.py --state worldpacks\<slug>\state-seed.json --schema state\schema.json
```

## Отдельное приложение USE Framework

USE Framework не входит в RP Stack и не меняет authority Gateway. Он описан отдельным элементом `docker_apps` в `inventories/local/group_vars/server.yml`, клонируется из закрытого GitHub-репозитория на полном commit SHA и обслуживается собственным Compose из application repository.

По решению владельца сервис публикуется как `0.0.0.0:8765`, включая LAN `192.168.1.88` и Tailscale `100.117.52.16`, без nginx и DNS. Постоянные данные находятся в `/srv/app-data/use-framework`, backup — в `/srv/backups/use-framework`. Приватный исходный реестр НС1 хранится только как `/srv/app-data/use-framework/import/ns1-assets.xlsx`; Ansible preflight запрещает запуск без него, а application bootstrap отклоняет тестовые `example.test` snapshots. Токен операций записи генерируется серверным Ansible в `/etc/ansible/use-framework-api-token` и попадает только в runtime `.env`. Credential `use_framework_github_token` обязателен только в `/etc/ansible/local-overrides.yml`; роль не сохраняет его в remote URL или `.env` приложения.

Полный runbook, включая health, backup/restore и rollback: [USE Framework на abykovserv](../use-framework.md).

## Secrets и local overrides

Host-specific и secret values находятся в:

```text
/etc/ansible/local-overrides.yml
```

Файл не коммитится. Не нужно переносить постоянные исправления напрямую в `/srv/apps/rp-stack`: следующий IaC apply может их заменить. Emergency hotfix должен быть немедленно отражён в Git.

Repair-лимиты разделены: `MAX_REPAIR_ATTEMPTS` сохраняет прежнее поведение
`rp`/`novel`, а `TRAINING_REPAIR_ATTEMPTS` (IaC:
`rp_stack_gateway_training_repair_attempts`, default `1`) разрешает не более
одной коррекции только для мягкого нарушения `training_runtime`. Значение `0`
возвращает training к немедленному authored fallback; hard violations и
provider failures repair не получают при любом значении.

## Rollback

Код откатывается новым revert/fix commit и повторным Ansible apply. Игровые данные восстанавливаются отдельно из `/srv/backups/rp-stack` после остановки контейнеров и проверки target paths.

## Базовые документы

- [Deployment skill](../../codex-skills/abykovserv-iac-deploy/SKILL.md)
- [Compose](../../roles/apps/templates/rp-stack.compose.yml.j2)
- [Operations](../../roles/apps/files/rp-stack/docs/operations.md)
- [Gateway tests](../../roles/apps/files/rp-stack/rp-gateway/tests)
