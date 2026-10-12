#!/usr/bin/env python3
"""Guest-local transactional Telegraf URL routing; never prints config or credentials."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import tomllib
import urllib.request

SOURCE = 'http://127.0.0.1:8086'
DESTINATION = 'http://10.77.0.1:18086'
CONFIG = Path('/etc/telegraf/telegraf.conf')
INCLUDES = Path('/etc/telegraf/telegraf.d')
BACKUPS = Path('/var/lib/ngfw-native-route')
HEADER = re.compile(rb'(?m)^[ \t]*\[\[outputs\.influxdb\]\][ \t]*(?:#[^\r\n]*)?(?:\r?\n|$)')
TABLE = re.compile(rb'(?m)^[ \t]*\[{1,2}[A-Za-z0-9_.\-\x22\x27 ]+\]{1,2}[ \t]*(?:#[^\r\n]*)?(?:\r?\n|$)')


class RouteError(RuntimeError):
    """Only fixed, non-secret diagnostic codes may cross the CLI boundary."""


def sha(data):
    return hashlib.sha256(data).hexdigest()


def parse(data):
    try:
        return tomllib.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise RouteError('invalid_toml_source') from None


def transform(data, expected_sha256, destination=DESTINATION):
    if destination != DESTINATION:
        raise RouteError('destination_not_allowlisted')
    if not isinstance(expected_sha256, str) or not re.fullmatch('[a-f0-9]{64}', expected_sha256):
        raise RouteError('exact_expected_sha256_required')
    document = parse(data)
    outputs = document.get('outputs', {}).get('influxdb')
    if not isinstance(outputs, list) or len(outputs) != 1:
        raise RouteError('exactly_one_outputs_influxdb_required')
    urls = outputs[0].get('urls')
    if urls not in ([SOURCE], [DESTINATION]):
        raise RouteError('exactly_one_expected_url_required')
    headers = list(HEADER.finditer(data))
    if len(headers) != 1:
        raise RouteError('unambiguous_influxdb_table_required')
    start = headers[0].end()
    following = TABLE.search(data, start)
    end = following.start() if following else len(data)
    section = data[start:end]
    # Syntax deliberately bounded. Unsupported escaped/dotted forms fail closed.
    assignments = list(re.finditer(rb'(?ms)^[ \t]*urls[ \t]*=[ \t]*\[(.*?)\]', section))
    if len(assignments) != 1:
        raise RouteError('unambiguous_urls_assignment_required')
    assignment = assignments[0]
    current = urls[0].encode()
    tokens = list(re.finditer(rb'([\x22\x27])(' + re.escape(current) + rb')\1', assignment.group(1)))
    if len(tokens) != 1:
        raise RouteError('one_literal_url_token_required')
    token = tokens[0]
    left = start + assignment.start(1) + token.start(2)
    right = start + assignment.start(1) + token.end(2)
    original = data[:left] + SOURCE.encode() + data[right:]
    if sha(original) != expected_sha256:
        raise RouteError('source_sha256_mismatch')
    candidate = data[:left] + DESTINATION.encode() + data[right:]
    expected = copy.deepcopy(document)
    expected['outputs']['influxdb'][0]['urls'] = [DESTINATION]
    if parse(candidate) != expected:
        raise RouteError('semantic_change_outside_url')
    return candidate, {'changed': data != candidate, 'before_sha256': sha(data),
                       'original_sha256': expected_sha256, 'after_sha256': sha(candidate)}


def private_directory(path):
    if path.is_symlink():
        raise RouteError('backup_directory_symlink_rejected')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    os.chown(path, 0, 0)


def metadata(path):
    current = path.lstat()
    if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
        raise RouteError('source_must_be_regular_nonlinked_file')
    return {'uid': current.st_uid, 'gid': current.st_gid, 'mode': stat.S_IMODE(current.st_mode)}


def atomic_write(path, data, owner):
    descriptor, temporary = tempfile.mkstemp(prefix='.ngfw-route-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
            os.fchown(output.fileno(), owner['uid'], owner['gid'])
            os.fchmod(output.fileno(), owner['mode'])
            os.fsync(output.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def safe_command(argv, timeout=45):
    try:
        subprocess.run(argv, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        raise RouteError('bounded_service_or_validation_command_failed') from None


def validate_installed(path):
    # Smoke-test an existing lightweight input only. Running vendor exec inputs
    # again alongside the service adds observers and is not needed for URL-only
    # routing. Full native collection requires independent post-apply readback.
    documents = [parse(Path(path).read_bytes())]
    documents.extend(parse(include.read_bytes()) for include in sorted(INCLUDES.glob('*.conf')))
    if not any(isinstance(document.get('inputs', {}).get('system'), list)
               and document['inputs']['system'] for document in documents):
        raise RouteError('configured_system_input_required_for_preflight')
    try:
        result = subprocess.run(
            ['/usr/bin/telegraf', '--config', str(path), '--config-directory', str(INCLUDES),
             '--test', '--input-filter', 'system', '--quiet'],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=45)
    except (OSError, subprocess.SubprocessError):
        raise RouteError('bounded_filtered_validation_failed') from None
    # Keep captured metrics in memory only; never return or print private tags.
    if not any(re.match(rb'^> system(?:,[^\r\n]*)? [^\r\n]+$', line)
               for line in result.stdout.splitlines()):
        raise RouteError('filtered_validation_no_system_metrics')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def ping_destination():
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(DESTINATION + '/ping', timeout=5) as response:
            if response.status != 204:
                raise RouteError('destination_ping_not_204')
    except RouteError:
        raise
    except Exception:
        raise RouteError('destination_ping_failed') from None


def service_active():
    try:
        result = subprocess.run(['systemctl', 'is-active', '--quiet', 'telegraf'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        raise RouteError('service_state_unavailable') from None


def restart():
    safe_command(['systemctl', 'restart', 'telegraf'], timeout=30)
    if not service_active():
        raise RouteError('telegraf_not_active_after_restart')


def read_backup(directory, expected):
    original = directory / (expected + '.original')
    state_path = directory / (expected + '.json')
    if original.is_symlink() or state_path.is_symlink():
        raise RouteError('backup_symlink_rejected')
    try:
        data, state = original.read_bytes(), json.loads(state_path.read_text())
    except (OSError, ValueError):
        raise RouteError('backup_state_missing_or_invalid') from None
    if sha(data) != expected or state.get('original_sha256') != expected:
        raise RouteError('backup_hash_mismatch')
    if (not isinstance(state.get('owner'), dict) or set(state['owner']) != {'uid', 'gid', 'mode'}
            or not all(type(state['owner'][k]) is int and state['owner'][k] >= 0 for k in state['owner'])
            or not 0 <= state['owner']['mode'] <= 0o7777 or state.get('active_before') is not True):
        raise RouteError('backup_metadata_invalid')
    return data, state


def validate_include_outputs(directory):
    # Refuse a second influxdb output in any loaded conf; do not edit includes.
    for path in sorted(directory.glob('*.conf')):
        if parse(path.read_bytes()).get('outputs', {}).get('influxdb'):
            raise RouteError('additional_influxdb_output_in_config_directory')


def transaction(config, backups, expected, action, destination=DESTINATION,
                validator=validate_installed, ping=ping_destination, active=service_active,
                restart_service=restart, writer=atomic_write, secure_dir=private_directory):
    if action not in ('plan', 'apply', 'readback', 'restore'):
        raise RouteError('unsupported_action')
    before_owner = metadata(config)
    before = config.read_bytes()
    candidate, result = transform(before, expected, destination)
    if action == 'readback':
        if not result['changed']:
            _, state = read_backup(backups, expected)
            if state.get('status') != 'APPLIED' or before_owner != state['owner']:
                raise RouteError('routed_readback_backup_metadata_unconfirmed')
        result.update(status='SOURCE' if result['changed'] else 'ROUTED', service_active=active(), changed=False)
        return result
    if action == 'restore':
        original, state = read_backup(backups, expected)
        if sha(before) not in (state['original_sha256'], state['after_sha256']):
            raise RouteError('restore_refuses_unrelated_current_configuration')
        config_changed = before != original or before_owner != state['owner']
        if config.read_bytes() != before or metadata(config) != before_owner:
            raise RouteError('source_changed_before_restore')
        if config_changed:
            writer(config, original, state['owner'])
        # An interrupted rollback can leave original bytes with the old routed
        # configuration still resident in an active process. Explicit restore
        # always reloads the original service, even if no file bytes differ.
        restart_service()
        if config.read_bytes() != original or metadata(config) != state['owner']:
            raise RouteError('restore_readback_mismatch')
        if not active():
            raise RouteError('restored_configuration_but_service_not_active')
        return {'status': 'RESTORED', 'changed': True, 'config_changed': config_changed,
                'service_restarted': True, 'sha256': sha(original), 'service_active': True}
    if not result['changed']:
        _, state = read_backup(backups, expected)
        if state.get('status') != 'APPLIED' or state['after_sha256'] != sha(before) or state['owner'] != before_owner:
            raise RouteError('incomplete_transaction_requires_explicit_restore')
        if not active():
            raise RouteError('already_routed_but_telegraf_not_active')
        return dict(result, status='UNCHANGED', changed=False, service_active=True)
    if not active():
        raise RouteError('source_telegraf_must_be_active')
    # Validate both current and candidate before changing the authoritative file.
    validator(config)
    ping()
    descriptor, staging = tempfile.mkstemp(prefix='.ngfw-route-candidate-', dir=config.parent)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            output.write(candidate)
            output.flush()
            os.fsync(output.fileno())
        validator(Path(staging))
        if action == 'plan':
            return dict(result, status='PLAN_VALIDATED', changed=False, would_change=True)
    finally:
        os.unlink(staging)
    # Refuse a concurrent edit that appeared during ping/validation.
    if config.read_bytes() != before or metadata(config) != before_owner:
        raise RouteError('source_changed_during_preflight')
    secure_dir(backups)
    original_path, state_path = backups / (expected + '.original'), backups / (expected + '.json')
    state = dict(result, owner=before_owner, active_before=True, status='PREPARED')
    if original_path.exists() or state_path.exists():
        saved, saved_state = read_backup(backups, expected)
        if saved != before or saved_state['owner'] != before_owner:
            raise RouteError('existing_backup_metadata_differs')
    else:
        writer(original_path, before, {'uid': 0, 'gid': 0, 'mode': 0o600})
    writer(state_path, json.dumps(state, sort_keys=True).encode(), {'uid': 0, 'gid': 0, 'mode': 0o600})
    if config.read_bytes() != before or metadata(config) != before_owner:
        raise RouteError('source_changed_before_atomic_apply')
    restart_attempted = False
    try:
        writer(config, candidate, before_owner)
        restart_attempted = True
        restart_service()
        ping()
        if config.read_bytes() != candidate or metadata(config) != before_owner or not active():
            raise RouteError('post_apply_readback_failed')
        state['status'] = 'APPLIED'
        writer(state_path, json.dumps(state, sort_keys=True).encode(), {'uid': 0, 'gid': 0, 'mode': 0o600})
    except Exception:
        try:
            current = config.read_bytes()
            if current not in (before, candidate):
                raise RouteError('rollback_refuses_unrelated_concurrent_configuration')
            writer(config, before, before_owner)
            if current != before or restart_attempted:
                restart_service()
            if config.read_bytes() != before or metadata(config) != before_owner or not active():
                raise RouteError('rollback_readback_failed')
            state['status'] = 'ROLLED_BACK'
            writer(state_path, json.dumps(state, sort_keys=True).encode(), {'uid': 0, 'gid': 0, 'mode': 0o600})
        except Exception:
            raise RouteError('apply_failed_and_rollback_unconfirmed') from None
        raise RouteError('apply_failed_original_restored') from None
    return dict(result, status='APPLIED', service_active=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--action', choices=['plan', 'apply', 'readback', 'restore'], default='plan')
    parser.add_argument('--expected-sha256', required=True)
    parser.add_argument('--destination', default=DESTINATION)
    args = parser.parse_args()
    try:
        if os.name != 'posix' or os.geteuid() != 0:
            raise RouteError('guest_root_execution_required')
        import fcntl
        # A /run lock does not carry config or authentication material.
        with Path('/run/ngfw-native-route.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RouteError('another_route_transaction_active') from None
            validate_include_outputs(INCLUDES)
            result = transaction(CONFIG, BACKUPS, args.expected_sha256, args.action, args.destination)
        print(json.dumps(result, sort_keys=True))
        return 0
    except RouteError as error:
        print(json.dumps({'status': 'FAILED', 'changed': None, 'reason': str(error)}))
        return 2
    except Exception:
        print(json.dumps({'status': 'FAILED', 'changed': None, 'reason': 'unexpected_local_failure_no_private_details'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
