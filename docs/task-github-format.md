# GitHub Task Import Format

The task reminder app imports new tasks from:

```text
https://raw.githubusercontent.com/abykovwww-byte/task.abykov.site/main/tasks.json
```

Create this file in the repository:

```text
abykovwww-byte/task.abykov.site
```

## Sync Rule

The app imports only new tasks.

Each GitHub task must have a stable unique `id`. After the app imports an `id`, later changes to that same GitHub task are ignored. This prevents GitHub sync from overwriting local edits, completion state, or deletions in the app.

To add a task, append a new object with a new `id`.

To change an already imported task, edit it in the app admin UI.

## tasks.json

Recommended shape:

```json
{
  "version": 1,
  "tasks": [
    {
      "id": "2026-06-09-call-provider",
      "title": "Call internet provider",
      "notes": "Ask about static IP and port forwarding for task.abykov.site.",
      "triggerAt": "2026-06-09T18:30",
      "assignedBy": "Codex",
      "priority": "high",
      "tags": ["server", "network"],
      "enabled": true
    }
  ]
}
```

Fields:

- `id` required. Stable unique string. Use lowercase letters, digits, and dashes.
- `title` required. Short task title.
- `notes` optional. Longer details.
- `triggerAt` optional. Local date-time in `YYYY-MM-DDTHH:mm` format.
- `assignedBy` optional. Person, system, or agent that created the task.
- `priority` optional. Free-form value such as `low`, `normal`, `high`, or `urgent`.
- `tags` optional. Array of custom strings.
- `enabled` optional. Defaults to `true` when omitted.

## Prompt For An AI Task Writer

Use this instruction when asking an AI agent to add tasks:

```text
Edit tasks.json in abykovwww-byte/task.abykov.site.
Append new tasks only. Never modify or delete existing task objects.
Every new task must have a globally unique stable id in kebab-case, a title, optional notes, optional triggerAt in YYYY-MM-DDTHH:mm local time, optional assignedBy, optional priority, optional tags array, and enabled true unless the task should be hidden.
Return the full valid JSON file.
```
