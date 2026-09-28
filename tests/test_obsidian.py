import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'codex-skills/obsidian-workspace/scripts'


def module(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + '.py'))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


v = module('vault')
importer = module('import_tasks')


def note(identifier='NOTE-1', body='Пример'):
    return f'---\nid: {identifier}\ntype: evidence\n---\n\n# {body}\n'


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        (self.base / 'vault').mkdir()
        (self.base / 'operations').mkdir()
        self.vault = v.Vault(self.base / 'vault', self.base / 'operations')

    def put(self, text, expected=None, path='Проекты/Проверка.md'):
        return self.vault.execute({'op': 'apply', 'path': path, 'text': text, 'expected_sha256': expected})

    def test_unicode_readback_idempotency_and_conflict(self):
        first = self.put(note())
        self.assertTrue(first['read_back'])
        self.assertEqual(self.vault.read('Проекты/Проверка.md')['text'], note())
        self.assertFalse(self.put(note())['changed'])
        with self.assertRaisesRegex(v.VaultError, 'Conflict'):
            self.put(note(body='Stale overwrite'))
        second = self.put(note(body='Новый результат'), first['sha256'])
        self.assertNotEqual(second['sha256'], first['sha256'])

    def test_traversal_config_and_reserved_paths_rejected(self):
        for path in ('../outside.md', '/outside.md', 'C:/outside.md', '.obsidian/app.md', 'x\\y.md', 'aux.md', 'x//y.md', 'x/./y.md'):
            with self.subTest(path=path), self.assertRaises(v.VaultError):
                self.put(note(), path=path)
        self.assertFalse((self.base / 'outside.md').exists())

    def test_symlink_cannot_escape(self):
        target = self.base / 'outside'
        target.mkdir()
        try:
            (self.base / 'vault/linked').symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest('Host does not permit test symlinks; Linux CI covers this')
        with self.assertRaises(v.VaultError):
            self.put(note(), path='linked/change.md')
        self.assertEqual(list(target.iterdir()), [])

    def test_hardlink_cannot_modify_outside_file(self):
        outside = self.base / 'outside.md'
        outside.write_text(note(), encoding='utf-8')
        os.link(outside, self.base / 'vault/hard.md')
        with self.assertRaises(v.VaultError):
            self.put(note(), path='hard.md')

    def test_duplicate_id_and_duplicate_yaml_key_rejected(self):
        self.put(note())
        with self.assertRaisesRegex(v.VaultError, 'Duplicate note ID'):
            self.put(note(), path='second.md')
        with self.assertRaisesRegex(v.VaultError, 'Duplicate YAML'):
            self.put('---\nid: X\nid: Y\ntype: task\n---\n', path='bad.md')
        with self.assertRaises(v.VaultError):
            self.put('invalid without properties', path='bad.MD')

    def test_failed_replace_preserves_original_and_cleans_staging(self):
        from unittest.mock import patch
        first = self.put(note())
        with patch.object(v.os, 'replace', side_effect=OSError('simulated disk error')):
            with self.assertRaises(OSError):
                self.put(note(body='replacement'), first['sha256'])
        self.assertEqual(self.vault.read('Проекты/Проверка.md')['text'], note())
        self.assertEqual(list((self.base / 'vault/Проекты').glob('.obsidian-write-*')), [])

    def test_binary_attachment_hash_and_search(self):
        payload = b'%PDF-1.4 example attachment'
        result = self.vault.apply({'op': 'apply', 'path': '_attachments/test.pdf',
                                  'base64': v.base64.b64encode(payload).decode(), 'expected_sha256': None})
        self.assertEqual(result['sha256'], v.digest(payload))
        self.put(note())
        self.assertEqual(len(self.vault.execute({'op': 'search', 'query': 'Пример'})['matches']), 1)

    def test_log_contains_hashes_not_note_content_and_no_backup(self):
        self.put(note(body='PRIVATE SENTENCE'))
        log = (self.base / 'operations/events.ndjson').read_text()
        self.assertNotIn('PRIVATE SENTENCE', log)
        self.assertEqual([json.loads(line)['status'] for line in log.splitlines()], ['prepared', 'committed'])
        self.assertEqual({p.name for p in (self.base / 'operations').iterdir()}, {'writer.lock', 'events.ndjson'})


class ImportTests(unittest.TestCase):
    def test_manual_date_properties_survive_import(self):
        from datetime import date
        import yaml
        result = importer.frontmatter({'id': 'TT-9999', 'custom_date': date(2026, 9, 28)})
        self.assertEqual(yaml.safe_load(result.split('---')[1])['custom_date'], '2026-09-28')

    def test_followup_and_manual_notes_survive_closed_jira(self):
        task = {'id': 'TT-9999', 'title': 'IDM follow-up', 'workflow': {'status': 'waiting'},
                'priority': {'level': 'unknown', 'source': 'unknown'}, 'schedule': {'due_at': None},
                'ownership': {'owner': None}, 'jira': {'key': 'EXAMPLE-1', 'url': 'https://example.invalid/1',
                'external_status': 'Closed', 'last_sync': '2026-09-25T00:00:00Z'}}
        previous = '---\nid: TT-9999\ntype: task\ncustom_property: keep\n---\n## Рабочие заметки\nРучной комментарий\n'
        rendered = importer.render(task, '2026-09-28', 'a' * 40, previous)
        meta = v.metadata(Path('task.md'), rendered.encode())
        self.assertEqual(meta['status'], 'waiting')
        self.assertEqual(meta['external_status'], 'Closed')
        self.assertIsNone(meta['due_at'])
        self.assertEqual(meta['custom_property'], 'keep')
        self.assertIn('Ручной комментарий', rendered)
        self.assertEqual(importer.render(task, '2026-09-28', 'a' * 40, rendered), rendered)


if __name__ == '__main__':
    unittest.main()
