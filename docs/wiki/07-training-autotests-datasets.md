# Обучение, автотесты и датасеты

[← Модели и провайдеры](06-models-and-providers.md) · [Главная](README.md) · [Далее: данные и безопасность →](08-data-and-security.md)

## Training — это runtime-контракт

Training WorldPack не является обычным RP-миром с добавленным вопросником. Он описывает детерминированную учебную программу:

- фиксированное число decision surfaces;
- одно явное действие игрока на ход;
- `training/program.json` с authored schedule, surface validation, debrief и fallback;
- `training/assessment.json` с наблюдаемыми detectors, effects и aggregates;
- наблюдаемые score fields в canonical state;
- output templates и validators;
- точку debrief, до которой запрещены hints, correctness и remediation.

Gateway не бросает dice и не делегирует LLM решение
«правильно/неправильно». Модель заново оформляет только активную сцену, а
универсальный `TrainingRuntimeService` интерпретирует program/assessment
WorldPack и обновляет state. Предметные слова, веса и fallback не находятся в
Gateway.

```mermaid
flowchart LR
    Pack["WorldPack runtime snapshot"] --> Surface["Active surface N"]
    Surface --> LLM["Initial narrator call"]
    LLM --> Norm["Canonical normalization"]
    Norm --> Valid["Hard fallback or one soft repair"]
    Valid --> Action["Явное действие игрока"]
    Action --> Rules["Generic detectors + authored rules"]
    Rules --> Score["Canonical score/evidence"]
    Score --> Next["Surface N+1"]
    Next -->|"final gate"| Debrief["Debrief из state"]
```

Для интерактивного surface письмо и разрешённые текстовые slots сайта приходят
в одном bundle: отдельного LLM-вызова для построения сайта нет. Возможный общий
soft-repair чинит тот же bundle целиком. Gateway создаёт snapshot, а `opened` /
`submitted` / `reported` становятся типизированным evidence следующего хода.
Отправка непустой формы считается `fail` только там, где это задаёт authored
policy конкретной surface; содержимое полей не проверяется и не сохраняется.

Runtime-контракт хешируется и сохраняется на party. Branch копирует тот же
snapshot. Обновление файлов мира не переписывает активное обучение. На каждом
ходе prompt содержит только текущую surface, профиль игрока, разрешённый
visible state и включённые interaction contracts; score, future turns и
assessment появляются только в отдельном debrief.

Границы формата тоже принадлежат WorldPack: активный prompt получает точные
`header` и `question`, prose `must_include` и optional `variation_budget`, но не
fallback или raw regex. На обычном ходу модель возвращает только
видимый текст; при включённом interaction contract — один JSON object с полным
текстом в `narrative_text`. Одна provider-added Markdown fence нормализуется
Gateway до строгой schema validation. Gateway подставляет canonical
header/question и no-link marker. Мягкое нарушение полей/профиля допускает один
training-repair; hard shape/identity/URL/attachment/score и повторная ошибка
уходят в authored fallback.

Live acceptance на `awareness-one-day` подтвердил полный путь: authored ход создал письмо и `corporate-sso` snapshot, Showroom открыл credential-form, Gateway принял `link_opened`, `credentials_submitted` и `site_closed`, а следующий ход атомарно пометил события consumed и добавил UI-evidence в canonical scoring. В тестовом fail-пути увеличились `credential-exposure`, `suspicious-artifacts-opened` и `unsafe-actions`; решение принял RuleEngine, не narrator.

В `awareness-one-day` итоговая модель оценки разделена на безопасность, ролевую уместность и деловую коммуникацию. Основание начисления сохраняется по конкретному ходу, а итоговые категории сверяются с canonical state, чтобы narrator не мог придумать красивое, но ложное объяснение баллов.

В authored расписании `awareness-one-day` сайты появляются только на ходах 4,
6 и 9. Ходы 4 и 9 рискованные, ход 6 легитимный; остальные семь сообщений
явно содержат `Ссылки: нет`. Одинаковое UI-affordance не раскрывает
правильность решения, а отключённая links capability использует тот же
WorldPack-authored no-link fallback.

Недельный `awareness` сохраняет прежний `player.resources.awareness-score` и
legacy compatibility resolver до отдельной миграции его программы. Сайты
появляются на ходах 1, 3, 5, 7, 8 и 9. Новую предметную логику в legacy-ветку
не добавляют: новые и мигрированные курсы используют `training_runtime`.

## Showroom и результат

WorldPack сам связывает публичный результат с numeric state path через `manifest.showroom_result`. Showroom scenario может включить leaderboard, но не выбирает, откуда взять score. Это сохраняет ownership оценки у authored training world.

После cutover Decision 018 этот контракт исполняется отдельным training-only
Gateway. Он начинает с новой SQLite: настройки опубликованных сценариев и covers
воссоздаются через admin API, а visitors, runs, parties, turns, feedback,
leaderboard, sessions и BYOK не переносятся. Поэтому `Мои прохождения` и рейтинг
начинаются с нуля; старые результаты остаются только в legacy snapshot/backup RP
Stack. `manifest.showroom_result` остаётся authority и не заменяется полем
миграции.

Corporate portal — только presentational snapshot. Он не содержит schedule, rubric или скрытые ответы.

### Capability dimensions

Каждый Showroom run содержит обе training-only dimensions; leaderboard,
autotest, dataset и analytics consumers должны сохранять их как dimensions:

```json
{
  "interactive_links_enabled": true,
  "interactive_workspace_enabled": false
}
```

Результаты разных комбинаций нельзя молча смешивать: рабочий диск может давать
подсказки и менять сложность. Для фишингового файла public snapshot не содержит
классификацию; server-only policy превращает `file_opened` в score-once evidence.
Поздний `file_reported` добавляет новый факт, но не удаляет уже совершённое
небезопасное открытие. Typed workspace events уже поступают в RuleEngine;
downstream-отчёты не должны смешивать разные комбинации флагов.

## LLM-vs-LLM autotests

Администратор может запустить до 30 автоматических player turns. Auto-player выбирается независимо от narrator и поддерживает OpenRouter или Local Gemma.

### Изоляция через checkpoint branch

```mermaid
gitGraph
    commit id: "Party main"
    commit id: "Checkpoint"
    branch autotest
    checkout autotest
    commit id: "Auto turn 1"
    commit id: "Auto turn 2"
    checkout main
    commit id: "Real player continues"
```

Фактически это не Git branch, а Gateway branch с собственной `state_campaign_id`.

Run:

1. создаёт checkpoint текущего head;
2. копирует state, training runtime snapshot, видимый turn prefix, checks, memory, journal и lore;
3. назначает branch-local IDs;
4. пишет новые ходы только в branch;
5. оставляет source Party доступной и неизменной;
6. показывает branch в read-only checkpoint tools, а не как отдельную Party.

Auto-player видит только public character description и player/GM transcript. Он не получает state, rubric, hidden score, answer key, Prompt Inspector или service data.

Runs сохраняют status, requested/completed turns, fallback count, provider/model, prompt, last action и error. Каждый narrator turn имеет idempotency key. Незавершённый run может продолжиться после рестарта Gateway без дублирования завершённого хода.

## Три уровня semantic evidence

Devkit не смешивает детерминированные тесты, реальные provider-вызовы и
браузерную приёмку в один нечёткий статус:

| Уровень | Что проверяет | Разрешённые эффекты |
|---|---|---|
| Offline | Схемы, сохранённые service responses, детектор тавтологии, отдельные метрики, Gateway/JS tests | Нет сети, provider-вызовов и запуска приложения |
| Provider canary | Реальный prompt/model через admin-autotest, повторные semantic reports и неизменность source Party | Только явно подтверждённый bounded autotest branch; сохранённые ответы помечены `producer: provider-canary` |
| Production endurance | Длинная живая RP-партия и `causal_probe` до влияния на последующие сцены | Только read-only наблюдение уже записанного runtime; эта ступень одна может доказать `держится` |

Provider canary использует существующий `POST /api/admin/autotests`: до запуска
он хеширует history/state source Party, после завершения сравнивает их снова и
считает run неуспешным при любом изменении main-line. Session cookie или bearer
берутся только из process environment, не попадают в аргументы, JSON report или
Git. При poll timeout runner запрашивает stop, чтобы не оставлять бесконтрольный
фоновой run. Candidate revision выше observed нужно передать явно; runner пишет
в отчёт requested и effective revision созданной branch и не принимает proof,
если Gateway создал ветку с другим значением.

Оракул `evals/acceptance/manifest.yml` и `acceptance/corpus/**` размечен
пользователем и read-only для исполнителя. Пороги читаются только из манифеста.
Отчёт не сворачивает метрики: отдельно показывает event precision/recall,
character attribution accuracy, empty-scene false-positive rate,
positive-trust recall, correction retention и разрезы по классу события. Ответ
`events=[]` на всём корпусе обязан провалить recall. Зелёный offline CI даёт
только `каркас`; он не доказывает реальную семантику provider и длинной партии.

Browser smoke не заменяется `curl`: UI считается проверенным только после
осмотра authenticated DOM, browser console и фактических network responses.
Отчёты runner пишутся в ignored `artifacts/evals/`.

## Логи как исходный корпус

Каждый новый turn сохраняет:

- точные prompt messages;
- player и assistant text;
- provider response;
- state version;
- request и idempotency IDs;
- scenario type и WorldPack;
- narrator provider/model;
- authoritative outcome;
- публичные artifact snapshots и потреблённые typed evidence без значений полей;
- validator result, repair и fallback;
- training runtime contract hash;
- origin: human/main или autotest branch.

Это operational log, а не автоматически хороший датасет.

## Review overlay

Партия и каждый turn независимо имеют статус:

- `review` — требует проверки;
- `approved` — разрешён для export;
- `excluded` — исключён.

Экспортируется только пересечение `party approved AND turn approved`. Успешный HTTP, отсутствие validator errors или 👍 игрока не заменяют кураторское решение.

Автоматические tags включают mode, main/branch, opening, autotest, repaired, validator-invalid, fallback, missing-prompt, `player-liked` и `player-disliked`.

## Обратная связь игрока

👍/👎 относится к полной паре `player_message -> assistant_response` и хранится по Gateway turn ID. Состояния взаимоисключающие: positive, negative, none. Любое изменение создаёт audit event.

- Positive полезен как сигнал для поиска SFT candidates.
- Negative полезен для review/exclusion.
- Ни один из них не меняет approval автоматически.
- Dislike сам по себе не образует DPO pair: предпочтительного replacement ответа нет.

## JSONL для LoRA/QLoRA

Admin endpoint создаёт `rp-gateway.sft.v1`:

```json
{"messages":[{"role":"system","content":"..."},{"role":"user","content":"..."},{"role":"assistant","content":"..."}],"metadata":{"schema_version":"rp-gateway.sft.v1","sample_id":"campaign:turn","group_id":"campaign","scenario_type":"rp","worldpack_id":"mechanist-new-world","tags":["rp","main","player-liked"]}}
```

LoRA и QLoRA используют один формат данных. Loss должен применяться к assistant completion, а prompt messages остаются входом.

Legacy turns без `prompt_json` получают `missing-prompt` и не экспортируются. Gateway не реконструирует отсутствующий input «по памяти».

## Защита от leakage

Checkpoint branch получает копию runtime history, но не dataset labels. Это не позволяет автоматически продублировать одобренный main-line sample.

Train/validation/test делятся по `metadata.group_id` целыми campaign/branch или ещё крупнее по миру. Случайный split соседних turns запрещён: они содержат почти одинаковую prompt history.

## API

```text
GET/POST  /api/admin/autotests...
GET/POST  /api/parties/{party_id}/branches...
GET/POST  /api/parties/{party_id}/artifacts...
POST      /api/showroom/runs/{run_id}/artifact-events
GET       /api/showroom/runs/{run_id}/workspace
GET       /api/showroom/runs/{run_id}/workspace/files/{file_id}/content
POST      /api/showroom/runs/{run_id}/workspace-events
PATCH     /api/admin/datasets/parties/{party_id}
GET/PUT   /api/admin/datasets/parties/{party_id}/turns...
GET       /api/admin/datasets/export.jsonl
PUT       /api/parties/{party_id}/turns/{turn_id}/feedback
PUT       /api/showroom/runs/{run_id}/turns/{turn_id}/feedback
```

## Источники

- [Training builder contract](../../codex-skills/training-world-pack-builder/SKILL.md)
- [Autotest ADR](../../roles/apps/files/rp-stack/docs/decisions/011-admin-llm-autotests.md)
- [Dataset ADR](../../roles/apps/files/rp-stack/docs/decisions/013-party-dataset-capture.md)
- [Autotest service](../../roles/apps/files/rp-stack/rp-gateway/app/services/autotest.py)
- [Party and dataset store](../../roles/apps/files/rp-stack/rp-gateway/app/services/party_store.py)
- [Interactive artifact ADR](../../roles/apps/files/rp-stack/docs/decisions/014-interactive-training-site-artifacts.md)
- [Training capability ADR](../../roles/apps/files/rp-stack/docs/decisions/015-training-scenario-interaction-capabilities.md)
- [WorldPack training runtime ADR](../../roles/apps/files/rp-stack/docs/decisions/017-worldpack-owned-training-runtime.md)
- [Training runtime tests](../../roles/apps/files/rp-stack/rp-gateway/tests/test_training_runtime.py)
