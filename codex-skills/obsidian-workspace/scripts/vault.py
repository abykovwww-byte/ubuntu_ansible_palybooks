#!/usr/bin/env python3
"""One JSON request on stdin. No shell commands, deletes, backups or external APIs."""
import base64
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
from datetime import datetime, timezone

import yaml

TEXT_TYPES = {'.md', '.base'}
BINARY_TYPES = {'.png', '.jpg', '.jpeg', '.pdf', '.svg'}
MAX_BYTES = 8 * 1024 * 1024


class VaultError(Exception):
    pass


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise VaultError('Duplicate YAML property: ' + str(key))
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def metadata(path, data):
    if path.suffix.lower() not in TEXT_TYPES:
        return {}
    text = data.decode('utf-8')
    if '\x00' in text:
        raise VaultError('NUL is forbidden in text files')
    if path.suffix.lower() == '.base':
        value = yaml.load(text, Loader=UniqueLoader)
        if not isinstance(value, dict) or not isinstance(value.get('views'), list):
            raise VaultError('A base requires a mapping with views')
        return {}
    match = re.match(r'\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)', text, re.S)
    if not match:
        raise VaultError('Markdown notes require YAML frontmatter with id and type')
    value = yaml.load(match.group(1), Loader=UniqueLoader)
    if not isinstance(value, dict) or not all(isinstance(value.get(k), str) and value[k].strip() for k in ('id', 'type')):
        raise VaultError('A note requires nonempty string id and type')
    return value


@contextlib.contextmanager
def locked(path):
    with path.open('a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            stream.seek(0)
            if not stream.read(1):
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class Vault:
    def __init__(self, root, operations):
        self.root = Path(root).resolve(strict=True)
        self.operations = Path(operations).resolve(strict=True)
        if not self.root.is_dir() or not self.operations.is_dir():
            raise VaultError('Vault and operations directories must already exist')
        if self.operations == self.root or self.operations.is_relative_to(self.root):
            raise VaultError('Operations directory must be outside the vault')

    def target(self, relative):
        if not isinstance(relative, str) or not relative or '\\' in relative or ':' in relative:
            raise VaultError('Use a relative POSIX path')
        parts = PurePosixPath(relative).parts
        if PurePosixPath(relative).as_posix() != relative:
            raise VaultError('Use a normalized path without repeated separators or dot segments')
        if PurePosixPath(relative).is_absolute() or any(p in ('.', '..') or p.startswith('.') for p in parts):
            raise VaultError('Hidden paths and traversal are forbidden')
        if len(relative) > 240 or any(re.search(r'[<>"|?*\x00-\x1f]', p) or p.endswith((' ', '.')) for p in parts):
            raise VaultError('Path is not portable between Windows and Linux')
        if any(re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', p) for p in parts):
            raise VaultError('Reserved Windows filename')
        current = self.root
        for part in parts:
            current = current / part
            if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
                raise VaultError('Symlinks and junctions are forbidden')
        resolved = current.resolve()
        if not resolved.is_relative_to(self.root):
            raise VaultError('Path escapes vault')
        if current.suffix.lower() not in TEXT_TYPES | BINARY_TYPES:
            raise VaultError('Unsupported file type')
        if current.exists():
            info = current.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise VaultError('Target must be an ordinary, unlinked file')
        return current

    def files(self):
        for folder, dirs, names in os.walk(self.root, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith('.') and not (Path(folder) / d).is_symlink()
                             and not (hasattr(Path(folder) / d, 'is_junction') and (Path(folder) / d).is_junction()))
            for name in sorted(names):
                candidate = Path(folder) / name
                if name.startswith('.') or candidate.suffix.lower() not in TEXT_TYPES | BINARY_TYPES:
                    continue
                relative = candidate.relative_to(self.root).as_posix()
                try:
                    self.target(relative)
                except VaultError:
                    continue
                yield relative

    def read(self, relative):
        path = self.target(relative)
        if not path.exists():
            return {'path': relative, 'exists': False, 'sha256': None}
        if path.stat().st_size > MAX_BYTES:
            raise VaultError('File exceeds the 8 MiB limit')
        data = path.read_bytes()
        result = {'path': relative, 'exists': True, 'sha256': digest(data), 'bytes': len(data)}
        if path.suffix.lower() in TEXT_TYPES:
            result['text'] = data.decode('utf-8')
        else:
            result['base64'] = base64.b64encode(data).decode('ascii')
        return result

    def audit(self, record):
        record['at'] = datetime.now(timezone.utc).isoformat()
        with (self.operations / 'events.ndjson').open('a', encoding='utf-8', newline='\n') as stream:
            stream.write(json.dumps(record, ensure_ascii=True) + '\n')
            stream.flush()
            os.fsync(stream.fileno())

    def apply(self, request):
        relative = request['path']
        path = self.target(relative)
        if 'expected_sha256' not in request:
            raise VaultError('expected_sha256 is required; use null only for a new file')
        expected = request['expected_sha256']
        if expected is not None and not re.fullmatch(r'[0-9a-f]{64}', str(expected)):
            raise VaultError('Invalid expected hash')
        if path.suffix.lower() in TEXT_TYPES:
            data = request['text'].encode('utf-8')
        else:
            if not relative.startswith('_attachments/'):
                raise VaultError('Binary files belong in _attachments')
            data = base64.b64decode(request['base64'], validate=True)
        if len(data) > MAX_BYTES:
            raise VaultError('File exceeds the 8 MiB limit')
        values = metadata(path, data)
        wanted = digest(data)
        with locked(self.operations / 'writer.lock'):
            path = self.target(relative)
            previous = path.read_bytes() if path.exists() else None
            current = digest(previous) if previous is not None else None
            if current == wanted:
                return {'path': relative, 'changed': False, 'sha256': current, 'read_back': True}
            if current != expected:
                raise VaultError('Conflict: file changed since read; re-read and merge')
            if values.get('id'):
                for other in self.files():
                    if other != relative and Path(other).suffix.lower() == '.md':
                        other_path = self.target(other)
                        try:
                            other_id = metadata(other_path, other_path.read_bytes()).get('id')
                        except (VaultError, yaml.YAMLError, UnicodeError):
                            continue
                        if other_id == values['id']:
                            raise VaultError('Duplicate note ID at ' + other)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.target(relative)
            event = {'op': 'apply', 'path': relative, 'before': current, 'after': wanted,
                     'source': str(request.get('source', 'user-command'))[:200]}
            self.audit(dict(event, status='prepared'))
            fd, staged = tempfile.mkstemp(prefix='.obsidian-write-', dir=path.parent)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(staged, stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600)
                self.target(relative)
                latest = path.read_bytes() if path.exists() else None
                if (digest(latest) if latest is not None else None) != current:
                    raise VaultError('Conflict during staging; target preserved')
                os.replace(staged, path)
                if os.name != 'nt':
                    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(parent_fd)
                    finally:
                        os.close(parent_fd)
                read_back = self.target(relative).read_bytes()
                if digest(read_back) != wanted:
                    raise VaultError('Changed after commit; re-read before further writes')
                self.audit(dict(event, status='committed'))
                return {'path': relative, 'changed': True, 'sha256': wanted, 'read_back': True}
            finally:
                if os.path.exists(staged):
                    os.unlink(staged)

    def execute(self, request):
        operation = request.get('op')
        if operation == 'list':
            return {'files': list(self.files())}
        if operation == 'read':
            return self.read(request['path'])
        if operation == 'search':
            query = str(request.get('query', '')).casefold()
            if not query:
                raise VaultError('A search query is required')
            results = []
            for relative in self.files():
                if Path(relative).suffix.lower() in TEXT_TYPES:
                    note = self.read(relative)
                    for number, line in enumerate(note['text'].splitlines(), 1):
                        if query in line.casefold():
                            results.append({'path': relative, 'line': number, 'text': line[:300]})
                            if len(results) == 100:
                                return {'matches': results, 'truncated': True}
            return {'matches': results, 'truncated': False}
        if operation == 'apply':
            return self.apply(request)
        raise VaultError('Supported operations: list, search, read, apply')


def main():
    try:
        # Installed root-owned config, not a path supplied in an LLM request.
        config = json.loads(Path(__file__).with_name('vault-config.json').read_text(encoding='utf-8'))
        incoming = sys.stdin.buffer.read(2 * MAX_BYTES + 1)
        if len(incoming) > 2 * MAX_BYTES:
            raise VaultError('Request too large')
        request = json.loads(incoming)
        if not isinstance(request, dict):
            raise VaultError('Request must be an object')
        result = Vault(config['root'], config['operations']).execute(request)
        print(json.dumps({'ok': True, **result}, ensure_ascii=True))
        return 0
    except (VaultError, OSError, ValueError, KeyError, TypeError, UnicodeError, yaml.YAMLError) as error:
        print(json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=True))
        return 1


if __name__ == '__main__':
    sys.exit(main())
