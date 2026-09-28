# Work note model

Navigation: `00 Главная`, `01 Входящие`, `10 Проекты`, `20 Задачи`, `30 Системы и процессы`, `40 Решения`, `50 Проверки и доказательства`, `60 Источники`, `70 Встречи и журнал`, `80 Знания и инструкции`, `90 Архив`, `99 Служебное`, `_attachments`.

Every Markdown note has YAML frontmatter with nonempty string `id` and `type`. IDs are unique across Work. Use TT IDs only for actual tracker records. Other examples: `PRJ-IDM`, `DEC-20260928-01`, `EVD-20260928-01`. Filename is stable; use title/aliases for readability.

Common properties: id, type, projects (a list of quoted wikilinks), created, updated. Keep source_captured_at, observed_at and verified_at separate from the page's updated timestamp. Date formats are ISO; display time in Europe/Moscow. Unknown dates stay empty.

Project: goal, confirmed state, next action, blockers, task links, decisions, checks, sources. Project status is not inferred from the existence of files.

Task: a readable narrative with context, observed progress, limits and a useful next action. Keep accounting fields in properties and a folded source block below the narrative, under `## Источники`. Set `editorial_content: true` after authoring: the importer preserves the narrative and title while refreshing tracker properties and the source block. It also preserves nonempty `## Рабочие заметки` and custom frontmatter. These section titles delimit importer-owned content; do not reuse them within the narrative. The legacy source heading `## Снимок трекера` is migrated on import. Outcome is a target, not evidence of completion. Keep due_at separate from review_at, and priority/source separate from urgency. UI changes to imported fields do not change GitHub. Reconcile narrative claims when the authoritative snapshot changes.

Evidence: question, object/version, conditions, observations, conclusion, limits, source. `evidence_kind`: observed/documented/inferred/hypothesis; `result`: pass/fail/partial/unknown. For mixed evidence, label each claim individually. Record local/CI/merge/apply/runtime as distinct scopes. For NGFW retain workload; for IDM import state; for DS distinguish catalogue/access/job start/classification.

Decision: question, options, proposal or accepted decision, rationale, consequences, review condition, approval source. Default `status: proposed` if actual acceptance is unknown. Never invent decision_owner.

Source: corporate URL or local Windows path, source capture date, scope and freshness. A Windows path is a workstation-only pointer until an explicitly selected attachment is copied. Do not ingest full AD tables merely to create navigation.

Meetings/daily notes link to canonical task notes instead of maintaining a second workflow list. Named contacts are not inferred task owners. Completed task files remain addressable; an archive view is sufficient.

Use the vault's templates and built-in Properties/Bases/Search/Backlinks. Bases are views over the same notes, not a new database. Core syntax: https://help.obsidian.md/bases/syntax . YAML parsers validate syntax; only the installed Obsidian UI can validate rendering and behavior.
