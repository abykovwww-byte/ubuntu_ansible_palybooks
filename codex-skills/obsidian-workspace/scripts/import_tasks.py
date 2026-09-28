#!/usr/bin/env python3
"""Build or apply one-way TT note projections; preserve the user's free-form section."""
import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys

MARKER = '## Рабочие заметки'


def frontmatter(values):
    # JSON scalar values are YAML compatible. Flat values work in Obsidian Properties.
    def yaml_scalar(value):
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        raise TypeError('Unsupported frontmatter value: ' + type(value).__name__)
    return '---\n' + ''.join(json.dumps(k, ensure_ascii=False) + ': ' + json.dumps(v, ensure_ascii=False, default=yaml_scalar) + '\n' for k, v in values.items()) + '---\n\n'


def body_without_generated(text):
    return text.split(MARKER, 1)[1] if MARKER in text else '\n\n'


def project_for(task):
    title = task['title'].casefold()
    if any(s in title for s in ('idm', 'inrights', 'ptx')) and 'vpn' not in title:
        return 'IDM'
    if any(s in title for s in ('ngfw', 'auditd')):
        return 'NGFW'
    if any(s in title for s in ('data security', 's3-ds', ' по ds')):
        return 'Data Security'
    if 'sar' in title:
        return 'SAR'
    if any(s in title for s in ('пасов', 'забот', 'автоматизац')):
        return 'OneOps'
    if 'wiki' in title or title == 'почистить jira':
        return 'Jira и Wiki'
    return 'Инфраструктура и согласования'


def render(task, captured_at, blob_sha, previous=''):
    if not re.fullmatch(r'TT-\d{4,}', task['id']):
        raise ValueError('Invalid task ID')
    workflow, priority, schedule = task['workflow'], task['priority'], task['schedule']
    jira = task.get('jira') or {}
    project = project_for(task)
    observed = datetime.now(timezone.utc).date().isoformat()
    # Keep local metadata and handwritten notes, while updating authoritative snapshot fields.
    existing = {}
    if previous.startswith('---\n'):
        import yaml
        existing = yaml.safe_load(previous.split('---', 2)[1]) or {}
    values = {**existing, 'id': task['id'], 'type': 'task', 'projects': ['[[' + project + ']]'],
              'project_mapping': 'proposed', 'canonical_store': 'github-task-tracker',
              'status': workflow['status'], 'status_reason': workflow.get('status_reason'),
              'priority': priority['level'], 'priority_source': priority.get('source'),
              'urgency': priority.get('urgency'), 'owner': task['ownership'].get('owner'),
              'due_at': schedule.get('due_at'), 'review_at': schedule.get('review_at'),
              'jira_key': jira.get('key'), 'external_status': jira.get('external_status'),
              'jira_last_sync': jira.get('last_sync'), 'source_captured_at': captured_at,
              'source_blob_sha': blob_sha, 'created': existing.get('created', observed), 'updated': observed}
    if task['id'] in ('TT-0014', 'TT-0015', 'TT-0020', 'TT-0038'):
        values['reconciliation'] = 'needs_review'
    else:
        values.setdefault('reconciliation', 'unknown')
    criteria = task.get('acceptance_criteria') or []
    blockers = task.get('blockers') or []
    text = frontmatter(values) + '# ' + task['title'] + '\n\n## Снимок трекера\n\n'
    text += '**Результат:** ' + (task.get('outcome') or 'Не определён в исходной карточке.') + '\n\n'
    text += '**Следующий шаг:** ' + (task.get('next_action') or 'Не задан.') + '\n\n'
    text += '**Основание статуса:** ' + (workflow.get('status_reason') or 'Не указано.') + '\n\n'
    text += '**Критерии:**\n' + ('\n'.join('- ' + str(x) for x in criteria) if criteria else '- Не заданы в источнике.') + '\n\n'
    if blockers:
        text += '**Блокеры:**\n' + '\n'.join('- ' + b.get('description', '') for b in blockers) + '\n\n'
    text += 'Основной реестр: [GitHub task tracker](https://github.com/abykovwww-byte/codex-task-tracker/blob/main/data/tasks.json).\n\n'
    text += 'Снимок реестра: ' + captured_at + '. Предлагаемая группировка: [[' + project + ']].\n\n'
    if jira:
        text += 'Jira: [' + jira['key'] + '](' + jira['url'] + '). Последняя синхронизация источника: ' + str(jira.get('last_sync')) + '. Jira заново этим импортом не опрашивалась.\n\n'
    text += MARKER + body_without_generated(previous)
    return text


def remote(request):
    process = subprocess.run([sys.executable, str(Path(__file__).with_name('remote.py'))],
                             input=json.dumps(request, ensure_ascii=True).encode(), capture_output=True)
    if process.returncode:
        raise RuntimeError(process.stdout.decode('utf-8', 'replace') or process.stderr.decode('utf-8', 'replace'))
    return json.loads(process.stdout)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('snapshot', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--ids', help='Comma-separated pilot IDs; omit for all records, including terminal ones')
    args = parser.parse_args()
    if args.apply == bool(args.output):
        parser.error('Choose either --apply or --output')
    source = json.loads(args.snapshot.read_text(encoding='utf-8-sig'))
    selected = set(args.ids.split(',')) if args.ids else None
    available = {t['id'] for t in source['tasks']}
    if selected and not selected <= available:
        raise ValueError('Unknown selected task IDs')
    results = []
    for task in source['tasks']:
        if selected and task['id'] not in selected:
            continue
        relative = '20 Задачи/' + task['id'] + '.md'
        old = remote({'op': 'read', 'path': relative}) if args.apply else {}
        text = render(task, source['updated_at'], source.get('_retrieved_blob_sha'), old.get('text', ''))
        if args.apply:
            results.append(remote({'op': 'apply', 'path': relative, 'text': text,
                                   'expected_sha256': old.get('sha256'), 'source': 'github-task-tracker-import'}))
        else:
            path = args.output / relative
            previous = path.read_text(encoding='utf-8') if path.exists() else ''
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(render(task, source['updated_at'], source.get('_retrieved_blob_sha'), previous), encoding='utf-8')
            results.append({'path': relative})
    print(json.dumps({'count': len(results), 'results': results}, ensure_ascii=True))


if __name__ == '__main__':
    main()
