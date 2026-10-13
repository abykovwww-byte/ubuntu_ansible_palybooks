"""Native Linux filesystem and private Compose preflight regressions.

These do not invoke Docker, providers or network. Windows cannot validate
POSIX no-follow/dir-fd semantics, so CI on Linux must execute this module.
"""
import hashlib
import json
import os

import pytest

from security_lab.compose_runtime import _serialize_handoff
from security_lab.contracts import Scope
from security_lab.handoff_files import HANDOFF_FILE_PROGRAM
from test_compose_runtime import Docker, execute_host, packet, payload, scope

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='POSIX no-follow/dir-fd filesystem qualification')


def store_type():
    namespace = {}
    exec(compile(HANDOFF_FILE_PROGRAM, 'trusted-handoff-files', 'exec'), namespace)
    return namespace['HandoffStore']


def make_store(tmp_path, *, run_id='owned', owner='nonce-owner'):
    root = tmp_path / 'workspace'; root.mkdir(exist_ok=True)
    text, descriptor = _serialize_handoff(Scope.model_validate(scope()),
        packet(data={'surface_nodes':[{'id':'late','value':'late.example.test'}], 'coverage':[{'status':'failed'}]}),
        run_id=run_id)
    return store_type()(str(root), run_id, owner, descriptor), text, descriptor


def test_exact_utf8_readonly_hash_and_owned_cleanup(tmp_path):
    store, text, descriptor = make_store(tmp_path)
    try:
        receipt = store.write(text)
        path = tmp_path / 'workspace/handoffs/owned/osint-context.json'
        assert path.read_bytes() == text.encode('utf-8')
        assert hashlib.sha256(path.read_bytes()).hexdigest() == descriptor['content_sha256']
        assert path.stat().st_mode & 0o777 == 0o444
        assert receipt['immutable_after_model_start'] is False
        assert store.verify(receipt['identity'])['file_hash_verified'] is True
        assert store.cleanup(receipt['identity']) is True and not path.parent.exists()
    finally: store.release()


def test_collision_and_changed_artifact_never_deleted(tmp_path):
    store, text, _ = make_store(tmp_path)
    store.write(text); store.release()
    collision, _, _ = make_store(tmp_path, owner='foreign-owner')
    try:
        with pytest.raises(FileExistsError): collision.write(text)
        with pytest.raises(RuntimeError, match='ownership'): collision.cleanup()
    finally: collision.release()
    original, _, _ = make_store(tmp_path)
    path = tmp_path / 'workspace/handoffs/owned/osint-context.json'
    path.chmod(0o644); path.write_text('foreign changed data'); path.chmod(0o444)
    try:
        with pytest.raises(RuntimeError): original.cleanup()
        assert path.read_text() == 'foreign changed data'
    finally: original.release()


def test_symlink_directory_and_unexpected_entries_refused(tmp_path):
    store, text, _ = make_store(tmp_path)
    outside = tmp_path / 'outside'; outside.mkdir(); (outside/'keep').write_text('keep')
    (tmp_path/'workspace/handoffs').symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(OSError): store.write(text)
        assert (outside/'keep').read_text() == 'keep'
    finally: store.release()
    (tmp_path/'workspace/handoffs').unlink()
    valid, _, _ = make_store(tmp_path)
    try:
        valid.write(text)
        path = tmp_path/'workspace/handoffs/owned'
        (path/'unrelated').write_text('do not recursively delete')
        with pytest.raises(RuntimeError, match='unexpected'): valid.cleanup()
        assert (path/'unrelated').exists() and (outside/'keep').exists()
    finally: valid.release()


def test_file_symlink_and_invalid_hash_denied_before_compose_run(monkeypatch, tmp_path):
    request = payload(tmp_path, 'start')
    request['context_json'] += ' '
    docker = Docker(request)
    result = execute_host(monkeypatch, request, docker)
    assert result['ok'] is False
    assert not any('run' in command and command[1] == 'compose' for command,_ in docker.calls)
    store, text, _ = make_store(tmp_path, run_id='second')
    try:
        store.write(text)
        path=tmp_path/'workspace/handoffs/second/osint-context.json'
        path.unlink(); path.symlink_to(tmp_path/'outside-secret')
        with pytest.raises((OSError, RuntimeError)): store.verify()
        with pytest.raises((OSError, RuntimeError)): store.cleanup()
        assert path.is_symlink()
    finally: store.release()


def test_workspace_config_guard_precedes_provider_launch(monkeypatch, tmp_path):
    class WrongMount(Docker):
        def run(self, command, **kwargs):
            result = super().run(command, **kwargs)
            if 'config' in command:
                result.stdout = json.dumps({'services':{'pentestgpt':{'volumes':[
                    {'type':'bind','target':'/workspace','source':'/wrong/workspace'}]}}})
            return result
    request=payload(tmp_path,'start'); docker=WrongMount(request)
    result=execute_host(monkeypatch,request,docker)
    assert result['ok'] is False
    assert not any('run' in command and command[1]=='compose' for command,_ in docker.calls)
    assert not (tmp_path/'workspace/handoffs/owned').exists()


def test_actual_owned_mount_corroboration_and_before_launch_bytes(monkeypatch, tmp_path):
    class VerifyBytes(Docker):
        def run(self, command, **kwargs):
            if command[1]=='compose' and 'run' in command:
                data=(tmp_path/'workspace/handoffs/owned/osint-context.json').read_bytes()
                assert hashlib.sha256(data).hexdigest()==self.request['handoff']['content_sha256']
                assert json.loads(data)['rules_of_engagement']['reference']=='lab:owned'
            return super().run(command, **kwargs)
    request=payload(tmp_path,'start'); docker=VerifyBytes(request)
    result=execute_host(monkeypatch,request,docker)['result']
    assert result['handoff']['workspace_config_verified'] is True
    assert result['handoff']['workspace_mount_verified'] is True
    assert result['handoff']['configuration_guard_race_free'] is False
    request.update(operation='stop',artifact_identity=result['handoff']['identity'])
    request.pop('context_json')
    closed=execute_host(monkeypatch,request,docker)['result']
    assert closed['access_closed'] is True and closed['handoff_removed'] is True


def test_container_closed_but_mutated_handoff_preserved(monkeypatch,tmp_path):
    request=payload(tmp_path,'start'); docker=Docker(request)
    started=execute_host(monkeypatch,request,docker)['result']
    path=tmp_path/'workspace/handoffs/owned/osint-context.json'
    path.chmod(0o644); path.write_text('changed by native actor'); path.chmod(0o444)
    request.update(operation='stop',artifact_identity=started['handoff']['identity'])
    request.pop('context_json')
    closed=execute_host(monkeypatch,request,docker)['result']
    assert closed['access_closed'] is True and closed['handoff_removed'] is False
    assert closed['handoff_cleanup_error']=='owned_artifact_validation_failed'
    assert docker.exists is False and path.read_text()=='changed by native actor'


def test_postlaunch_wrong_actual_mount_closes_owned_container(monkeypatch,tmp_path):
    class WrongActualMount(Docker):
        def run(self,command,**kwargs):
            result=super().run(command,**kwargs)
            if command[1]=='inspect':
                value=json.loads(result.stdout); value[0]['Mounts'][0]['Source']='/wrong'
                result.stdout=json.dumps(value)
            return result
    request=payload(tmp_path,'start'); docker=WrongActualMount(request)
    result=execute_host(monkeypatch,request,docker)
    assert result['ok'] is False and docker.exists is False
    assert not (tmp_path/'workspace/handoffs/owned').exists()
