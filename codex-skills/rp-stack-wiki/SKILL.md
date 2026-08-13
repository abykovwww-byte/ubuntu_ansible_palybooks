---
name: rp-stack-wiki
description: Maintain the human-readable RP Stack Wiki and its Mermaid architecture diagrams in the ubuntu_ansible_palybooks repository. Use when explaining or documenting RP Stack architecture, reviewing whether documentation matches implementation, or making significant changes to Gateway, Light GUI, Showroom, WorldPacks, scenario modes, turn processing, memory, providers, training, datasets, security, storage, deployment, ports, networks, or component boundaries.
---

# RP Stack Wiki

Keep the versioned RP Stack documentation understandable, accurate, and aligned
with the implementation.

## Canonical Locations

- repository: `C:\Users\Адександр\Documents\Tavern\ubuntu_ansible_palybooks`
- Wiki hub: `docs/wiki/README.md`
- Wiki pages: `docs/wiki/01-architecture.md` through
  `docs/wiki/09-operations-and-repository.md`
- Wiki navigation: `docs/wiki/_Sidebar.md`
- GitHub: `https://github.com/abykovwww-byte/ubuntu_ansible_palybooks/tree/main/docs/wiki`

The Wiki is the human-readable system map. Source code, Ansible, Compose,
schemas, migrations, tests, and current runtime evidence remain authoritative
for implementation facts.

## Start Here

1. Read `docs/wiki/README.md` completely.
2. Read `docs/repository-work-standard.md` for the checked workstation and
   delivery contract.
3. Read every Wiki page relevant to the request. For a full architecture review,
   read all numbered pages and `_Sidebar.md`.
4. Inspect the current Git status and preserve unrelated work.
5. Verify claims against current source and IaC. Use Graphify for navigation when
   `graphify-out/` exists, but confirm important facts in source.
6. Distinguish implemented behavior, planned work, compatibility surfaces, and
   retired behavior explicitly.

Do not use an older Wiki revision, adjacent task, ADR, or graph result as proof
that a feature is still implemented.

## Decision 022 evidence language

Describe RP Stack readiness only as `каркас` (code exists and module tests are green),
`подключено` (execution in the real turn path), `наблюдается` (effect in the
authoritative mechanic store and in a later real-party prompt), or `держится`
(later scenes repeatedly account for the effect without drift). Do not use bare
`implemented`, `working`, `ready`, `реализовано`, `работает`, or `готово` claims.
Green CI is necessary delivery evidence, but is insufficient for
`наблюдается` or `держится`.

Document the acceptance oracle at
`roles/apps/files/rp-stack/evals/acceptance/manifest.yml` and
`roles/apps/files/rp-stack/evals/acceptance/corpus/**` as independent,
user-owned, and read-only. Its labels and thresholds are not implementation
inputs to rewrite. Reports must read thresholds from the manifest and keep
`event_precision`, `event_recall`, `character_id_accuracy`,
`empty_scene_false_positive_rate`, `positive_trust_recall`, and
`correction_retention` separate, including per-event-class metrics when the
manifest requires them.

Keep evidence split into three layers: offline uses saved responses and no
providers; provider-canary uses a real prompt and model through admin-autotest
without mutating the source party; production-endurance uses a long live party
and `causal_probe` through later scene consequences. Only production endurance
can establish `держится`.

When a change introduces or materially expands exact diagnostic prompt/response
capture (`service_call_log`, `turn_trace_events`), or changes its retention or
redaction, record that deployment is paused until the user explicitly decides
retention and redaction depth. An accepted ADR for that exact revision satisfies
the gate; a green PR or configured default does not.

## Documentation Impact Gate

Treat a change as significant when it changes any of these contracts:

- services, containers, ports, networks, trust boundaries, or ownership;
- authentication, authorization, sessions, cookies, provider keys, or secrets;
- API boundaries or a user-visible workflow shared by multiple components;
- party isolation, canonical state, turn lifecycle, rules, validation, rollback,
  branches, audit, or idempotency;
- prompt composition, history, memory, lore, retrieval, context budgets, or model
  routing;
- `rp`, `novel`, or `training` semantics and WorldPack contracts;
- deterministic scoring, debrief, autotests, dataset review, or export;
- persistent data, migrations, backups, restore, privacy, or security risks;
- Ansible delivery, Compose topology, server paths, verification, or rollback.

For every significant change, update the affected Wiki page and every Mermaid
diagram whose nodes, edges, order, labels, or trust boundaries changed. Keep the
documentation update in the same commit as the implementation whenever
practical.

For a change that is not significant, do not create documentation churn. State
in the final report that the Wiki impact was reviewed and found neutral.

## Page Routing

| Change | Update |
|---|---|
| Components, ownership, topology | `01-architecture.md` and its component/data diagrams |
| Light GUI, Showroom, admin, public API | `02-interfaces.md` and relevant sequence diagrams |
| Turn order, rules, validation, persistence | `03-turn-lifecycle.md` and the turn sequence diagram |
| WorldPack layout or scenario contracts | `04-worldpacks-and-modes.md` |
| Prompt, memory, history, lore, retrieval | `05-memory-and-retrieval.md` and context diagrams |
| Models, providers, BYOK, local LLM | `06-models-and-providers.md` and routing diagrams |
| Training, scoring, autotests, datasets | `07-training-autotests-datasets.md` |
| Storage, auth, secrets, privacy, networks | `08-data-and-security.md` |
| IaC, paths, deploy, tests, backup, rollback | `09-operations-and-repository.md` and delivery diagram |
| New, removed, or renamed page | `README.md`, `_Sidebar.md`, and root `README.md` if needed |

Update more than one page when a contract crosses boundaries. Do not duplicate
the same detailed explanation on every page; keep one primary explanation and
link to it.

## Writing and Diagram Rules

- Write the Wiki in Russian unless the user requests another language.
- Lead with system behavior and user-visible meaning, then name code symbols and
  paths as supporting detail.
- Expand uncommon terms on first use and keep tables scannable.
- Prefer small Mermaid diagrams that show real ownership, flow, sequence, or
  trust boundaries. Do not add decorative diagrams.
- Update existing diagrams instead of adding a competing version of the same
  architecture.
- Mark future concepts as planned; never draw them as current runtime.
- Preserve the distinctions between raw history, memory, canonical state, lore,
  and dataset review overlays.
- Preserve the delivery states: local, committed, pushed, applied, and live
  verified are separate claims.
- Never include credentials, private overrides, database contents, private
  prompts, or user data in the Wiki.

## Validation

Before completing a Wiki change:

1. Confirm every relative Markdown link resolves.
2. Confirm Markdown fences are balanced and every Mermaid block closes.
3. Confirm navigation includes every page exactly where intended.
4. Run `git diff --check` on the selected files.
5. Re-read changed diagrams against the source paths that justify them.
6. Confirm the Wiki hub's review date/revision statement is still honest.
7. Review `git diff --cached --name-only` before committing so unrelated work is
   not included.

A documentation-only commit does not require Ansible apply. If documentation is
part of a runtime change, follow `$abykovserv-iac-deploy` for deployment and live
verification.

## Completion Report

Report:

- pages and diagrams changed;
- source or runtime evidence used;
- validation performed;
- local, committed, pushed, applied, and live-verified status separately;
- any intentionally deferred or planned documentation.
