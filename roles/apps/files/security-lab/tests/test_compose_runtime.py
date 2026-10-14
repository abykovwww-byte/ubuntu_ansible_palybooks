import asyncio
import io
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from security_lab.compose_runtime import (ComposePentestRuntime, ComposeRuntimeConfig,
                                         _HOST_SCRIPT, _request_goal, _process_environment, MAX_GOAL_CHARS, MAX_CONTEXT_BYTES,
                                         prepare_handoff_context, _serialize_handoff)
from security_lab.contracts import Denied, Scope, digest
from security_lab.handoff_files import HANDOFF_FILE_PROGRAM


def config(tmp_path, **values):
    return ComposeRuntimeConfig(mode="local", compose_directory="/srv/apps/pentestgpt",
                                runs_directory="/srv/app-data/pentestgpt/runs", **values)


def scope(**values):
    return {"targets":[{"host":"fixture.example.test", "port":8443, "paths":["/public"], "methods":["GET"]}],
        "authorization":{"reference":"lab:owned", "owner":"operator", "basis":"Owned fixture",
                         "hosts":["fixture.example.test"], "valid_from":1, "valid_until":2_000_000_000},
        "expires_at":2_000_000_000, **values}


def packet(manifest=None, data=None):
    manifest = Scope.model_validate(manifest or scope()).model_dump(mode="json")
    result = {"schema_version":1, "stage":"checks", "execution_authorized":False,
              "case_id":"fixture-case", "generation":1,
              "binding":{"id":"scope-id", "hash":digest(manifest)},
              "scope_manifest":manifest, "effective_scope":manifest,
              "rules_of_engagement":manifest["authorization"], "snapshot_at":1800000000,
              "policy":{"tool_rule":"typed facade only"}, "available_tools":[],
              "untrusted_osint_data":data or {"surface_nodes":[], "surface_links":[], "coverage":[]}}
    result["context_hash"] = digest(result)
    return result


def nmap():
    return ("/usr/bin/nmap", "--unprivileged", "-sT", "-Pn", "-n", "--disable-arp-ping",
            "--max-retries", "0", "--max-parallelism", "1", "--max-rate", "2", "--scan-delay", "500ms",
            "--host-timeout", "5s", "-p", "8080", "-oX", "-", "172.31.64.2")


class Runner:
    def __init__(self):
        self.calls = []
        self.failed_start = False

    async def __call__(self, command, *, payload, timeout):
        request = json.loads(payload)
        self.calls.append((command, request, timeout))
        if self.failed_start and request["operation"] == "start":
            raise TimeoutError("private diagnostic")
        result = {"status":"running"} if request["operation"] == "start" else {
            "status":"stopped", "access_closed":True} if request["operation"] == "stop" else {
            "status":"completed", "receipts":[{"type":"command_run", "stdout":"PENTESTGPT_FIXTURE_OK", "exit_code":0}]}
        if request["operation"] == "start":
            result['handoff'] = {'descriptor':request['handoff'], 'identity':{'device':1,'inode':2},
                'file_hash_verified':True,'file_read_only_mode':True,
                'workspace_config_verified':True,'workspace_mount_verified':True,
                'immutable_after_model_start':False}
        return 0, json.dumps({"ok":True, "result":result}).encode(), b"private stderr"


@pytest.mark.skipif(os.name != 'nt', reason='Windows OpenSSH environment contract')
def test_mcp_minimal_environment_restores_only_windows_programdata(monkeypatch):
    from mcp.client.stdio import get_default_environment
    minimal = get_default_environment()
    monkeypatch.setattr(os, 'environ', minimal)
    restored = _process_environment()
    assert restored is not None and restored['ProgramData']
    assert {k:v for k,v in restored.items() if k != 'ProgramData'} == minimal


def test_start_is_explicit_and_payload_never_shell_interpolated(tmp_path):
    runner = Runner()
    runtime = ComposePentestRuntime(ComposeRuntimeConfig(mode="ssh", compose_directory="/srv/apps/pentestgpt",
        runs_directory="/srv/app-data/pentestgpt/runs", ssh_host="operator@owned-host", ssh_identity="C:/fixed/key"), runner=runner)
    assert not runner.calls
    result = asyncio.run(runtime.start(run_id="host-generated-001", scope=scope(),
        osint_context=packet(data={"site":"' $(touch /tmp/bad); `id`", "api_key":"private-credential"})))
    command, payload, _ = runner.calls[0]
    assert command[:2] == ["ssh", "-T"]
    assert "$(touch" not in command[-1]
    assert "$(touch" not in payload["goal"]
    assert "$(touch" in payload["context_json"]
    assert "private-credential" not in str(payload)
    assert json.loads(payload["context_json"])["rules_of_engagement"]["reference"] == "lab:owned"
    assert payload["target"] == "https://fixture.example.test:8443/public"
    assert len(payload["goal"]) <= MAX_GOAL_CHARS
    assert result["native_full_access"] is True and result["reduced_scope_enforced"] is False
    assert result["phase_enforcement"] == "prompt_only"
    assert "owner" not in result and "private stderr" not in str(result)


def test_goal_preserves_constraints_and_uses_only_full_file_descriptor():
    manifest = Scope.model_validate(scope())
    context = packet(data={"long":"x"*10000})
    descriptor = prepare_handoff_context(manifest, context)
    _, goal = _request_goal(manifest, descriptor)
    assert '\"methods\":[\"GET\"]' in goal and '\"paths\":[\"/public\"]' in goal
    assert "UNTRUSTED DATA" in goal and descriptor['worker_path'] in goal
    assert descriptor['content_sha256'] in goal and 'x'*100 not in goal
    assert len(goal) <= MAX_GOAL_CHARS
    assert "one local preflight command" in goal
    assert "summary must be at most 500 characters" in goal
    assert "one exact contiguous evidence_excerpt of at most 1500 characters" in goal
    assert "do not repeat file verification" in goal
    assert "proceed to GET or HEAD" in goal
    many = scope(targets=[{"host":"fixture.example.test", "paths":["/one", "/two"]}])
    with pytest.raises(Denied, match="one exact target"):
        _request_goal(Scope.model_validate(many), {})


def test_whole_file_context_preserves_late_graph_history_and_coverage():
    data = {'evidence_index':[{'id':f'e{i}', 'source':'fixture', 'synthetic':True} for i in range(1000)],
            'surface_nodes':[{'id':'late', 'value':'late-discovered.example.test', 'evidence_ids':['e999']}],
            'observations':[{'entity_id':'late', 'attributes':{'historical':True}}],
            'coverage':[{'source':'git', 'status':'not_attempted'}], 'api_key':'private-credential'}
    text, descriptor = _serialize_handoff(Scope.model_validate(scope()), packet(data=data), run_id='actual-run')
    decoded = json.loads(text)
    assert descriptor['size_bytes'] > 4000
    assert decoded['untrusted_osint_data']['surface_nodes'] == data['surface_nodes']
    assert decoded['untrusted_osint_data']['observations'] == data['observations']
    assert decoded['untrusted_osint_data']['coverage'] == data['coverage']
    assert decoded['untrusted_osint_data']['evidence_index'] == data['evidence_index']
    assert 'private-credential' not in text and descriptor['redaction_applied'] is True
    assert decoded['scope_manifest']['authorization']['reference'] == 'lab:owned'
    import hashlib
    assert hashlib.sha256(text.encode()).hexdigest() == descriptor['content_sha256']
    assert descriptor['context_hash'] == decoded['context_hash']
    _, goal = _request_goal(Scope.model_validate(scope()), descriptor)
    assert len(goal) <= MAX_GOAL_CHARS
    assert "compact JSON result of at most 1500 characters" in goal
    assert "Do not print full graph" in goal
    assert "late-discovered.example.test" not in goal


def test_oversized_context_refused_before_runtime_handle_or_container(tmp_path):
    runner = Runner()
    runtime = ComposePentestRuntime(config(tmp_path), runner=runner)
    with pytest.raises(Denied, match='1 MiB; no map was truncated'):
        asyncio.run(runtime.start(run_id='oversized', scope=scope(),
                                  osint_context=packet(data={'value':'x'*MAX_CONTEXT_BYTES})))
    assert runner.calls == [] and runtime._handles == {}


@pytest.mark.parametrize('value', [{}, {'schema_version':1}, {'schema_version':1,'run_id':None},
                                   {'schema_version':1,'run_id':'ok','generation':True}])
def test_partial_descriptor_consistently_denied(value):
    with pytest.raises(Denied, match='descriptor'):
        _request_goal(Scope.model_validate(scope()), value)


def test_context_binding_tamper_and_only_seconds_reduction():
    original = Scope.model_validate(scope(budget={'seconds':300}))
    context = packet(original.model_dump(mode='json'))
    reduced = original.model_copy(update={'budget':original.budget.model_copy(update={'seconds':90})})
    assert prepare_handoff_context(reduced, context)['scope_hash'] == digest(original.model_dump(mode='json'))
    changed = original.model_copy(update={'budget':original.budget.model_copy(update={'jobs':1})})
    with pytest.raises(Denied): prepare_handoff_context(changed, context)
    context['generation'] = 2
    with pytest.raises(Denied): prepare_handoff_context(original, context)


def test_unknown_run_and_repeated_id_never_launch(tmp_path):
    runner = Runner()
    runtime = ComposePentestRuntime(config(tmp_path), runner=runner)
    with pytest.raises(Denied, match="not owned"):
        asyncio.run(runtime.stop("foreign"))
    asyncio.run(runtime.start(run_id="owned", scope=scope(), osint_context=packet()))
    with pytest.raises(Denied, match="already owned"):
        asyncio.run(runtime.start(run_id="owned", scope=scope(), osint_context=packet()))
    assert len(runner.calls) == 1


def test_timeout_attempts_only_own_cleanup_and_retains_retry_handle(tmp_path):
    runner = Runner()
    runner.failed_start = True
    runtime = ComposePentestRuntime(config(tmp_path), runner=runner)
    with pytest.raises(TimeoutError):
        asyncio.run(runtime.start(run_id="owned", scope=scope(), osint_context=packet()))
    assert [request["operation"] for _,request,_ in runner.calls] == ["start", "stop"]
    assert runner.calls[0][1]["owner"] == runner.calls[1][1]["owner"]
    assert asyncio.run(runtime.stop("owned"))["access_closed"] is True


class Docker:
    def __init__(self, request):
        self.request, self.calls = request, []
        self.exists, self.foreign = False, False
        self.running = True
        self.tool_result = "<nmaprun><host>owned fixture</host></nmaprun>"

    def run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        args = command[1:]
        output = ""
        code = 0
        if args[0] == "compose":
            if "config" in args:
                output = json.dumps({'services':{'pentestgpt':{'volumes':[{'type':'bind', 'target':'/workspace',
                    'source':self.request.get('workspace_directory')}]}}})
            elif "--entrypoint" in args:
                self.exists = False
                output = self.tool_result
            else:
                self.exists = True
                output = "abc123\n"
        elif args[0] == "ps":
            output = "abc123\n" if self.exists else ""
        elif args[0] == "inspect":
            output = json.dumps([{"Id":"abc123", "Name":"/"+self.request["container_name"],
                "Mounts":[{'Type':'bind','Destination':'/workspace','Source':self.request.get('workspace_directory')}],
                "State":{"Running":self.running}, "Config":{"Labels":{
                "security-lab.compose-owner":"foreign" if self.foreign else self.request["owner"],
                "security-lab.run-id":self.request["run_id"]}}}])
        elif args[0] == "stop":
            self.exists = False
        elif args[0] == "rm":
            self.exists = False
        return SimpleNamespace(returncode=code, stdout=output, stderr="")


def payload(tmp_path, operation):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(exist_ok=True)
    result = {"operation":operation, "compose_directory":str(tmp_path), "runs_directory":str(tmp_path / "runs"),
            "run_id":"owned", "container_name":"security-lab-pentest-fixed", "owner":"nonce-owner",
            "command_timeout_seconds":20, "max_runtime_seconds":1800, "max_receipt_chars":12000,
            "target":"https://fixture.example.test:8443/public", "goal":"fixed checks only",
            "kind":"nmap", "tool_service":"pentest-tools", "workspace_directory":str(workspace)}
    if operation == 'start':
        text, descriptor = _serialize_handoff(Scope.model_validate(scope()), packet(), run_id='owned')
        result.update(context_json=text, handoff=descriptor, scope_hash=descriptor['scope_hash'])
    return result


def execute_host(monkeypatch, request, docker, *, handoff_store=None):
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    monkeypatch.setattr(sys, "stdout", output)
    monkeypatch.setattr(subprocess, "run", docker.run)
    try:
        script, namespace = _HOST_SCRIPT, {}
        if handoff_store is not None:
            # This isolates the Docker ownership race on Windows as well as
            # Linux; native filesystem guards have separate POSIX tests.
            assert script.startswith(HANDOFF_FILE_PROGRAM + '\n')
            script = script[len(HANDOFF_FILE_PROGRAM) + 1:]
            namespace['HandoffStore'] = handoff_store
        exec(compile(script, "trusted-host-program", "exec"), namespace)
    except SystemExit:
        pass
    return json.loads(output.getvalue())


@pytest.mark.skipif(os.name != 'posix', reason='Native artifact store requires POSIX no-follow dirfds')
def test_host_launch_exact_allowlist_and_existing_topology(monkeypatch, tmp_path):
    request = payload(tmp_path, "start")
    docker = Docker(request)
    result = execute_host(monkeypatch, request, docker)
    launch = next(command for command,_ in docker.calls if command[1] == "compose" and 'run' in command)
    assert launch[2:6] == ["--file", "compose.yml", "--profile", "manual"]
    assert "PENTESTGPT_ALLOWED_TARGET_DOMAINS=" in launch
    assert "PENTESTGPT_ALLOWED_TARGETS="+request["target"] in launch
    assert "--privileged" not in launch and "--network" not in launch and "--volume" not in launch
    assert launch[-7:] == ["pentestgpt-run", "--run-id", "owned", "--target", request["target"], "--goal", request["goal"]]
    assert result["result"]["status"] == "running"


def test_host_stop_foreign_ownership_denies_without_mutation(monkeypatch, tmp_path):
    request = payload(tmp_path, "stop")
    docker = Docker(request)
    docker.exists = docker.foreign = True
    result = execute_host(monkeypatch, request, docker)
    assert result["ok"] is False
    assert not any(command[1] in {"stop", "rm", "kill"} for command,_ in docker.calls)


class InspectRaceDocker(Docker):
    def __init__(self, request, outcome):
        super().__init__(request)
        self.exists = True
        self.outcome = outcome
        self.ps_calls = 0

    def run(self, command, **kwargs):
        result = super().run(command, **kwargs)
        if command[1] == 'ps':
            self.ps_calls += 1
            if self.ps_calls == 2 and self.outcome == 'query_failed':
                return SimpleNamespace(returncode=1, stdout='', stderr='private Docker diagnostics')
            if self.ps_calls == 2 and self.outcome == 'ambiguous':
                return SimpleNamespace(returncode=0, stdout='abc123\ndef456\n', stderr='')
        if command[1] == 'inspect':
            if self.outcome in {'removed', 'removed_after_timeout'}:
                self.exists = False
            if self.outcome == 'removed_after_timeout':
                raise subprocess.TimeoutExpired(command, 20)
            return SimpleNamespace(returncode=1, stdout='', stderr='private Docker diagnostics')
        return result


def tracked_store(request):
    class Store:
        cleanup_calls = 0
        released = False

        def __init__(self, workspace, run_id, owner, descriptor):
            assert (workspace, run_id, owner, descriptor) == (request['workspace_directory'],
                request['run_id'], request['owner'], request['handoff'])

        def cleanup(self, identity):
            assert identity == request['artifact_identity']
            Store.cleanup_calls += 1
            return True

        def release(self):
            Store.released = True
    return Store


@pytest.mark.parametrize('outcome', ['removed', 'removed_after_timeout'])
def test_ps_inspect_disappearance_confirms_absence_then_artifact_cleanup(monkeypatch, tmp_path, outcome):
    request = payload(tmp_path, 'start')
    request.update(operation='stop', artifact_identity={'device':1,'inode':2})
    store = tracked_store(request)
    docker = InspectRaceDocker(request, outcome)
    reply = execute_host(monkeypatch, request, docker, handoff_store=store)
    assert reply['ok'] is True
    assert reply['result']['access_closed'] is True and reply['result']['handoff_removed'] is True
    assert store.cleanup_calls == 1 and store.released is True
    queries = [command for command,_ in docker.calls if command[1] == 'ps']
    assert len(queries) == 2 and queries[0] == queries[1]
    assert 'name=^/' + request['container_name'] + '$' in queries[1]
    assert not any(command[1] in {'stop','rm','kill'} for command,_ in docker.calls)


@pytest.mark.parametrize('outcome', ['still_present', 'query_failed', 'ambiguous'])
def test_failed_inspect_without_confirmed_absence_preserves_artifact(monkeypatch, tmp_path, outcome):
    request = payload(tmp_path, 'start')
    request.update(operation='stop', artifact_identity={'device':1,'inode':2})
    store = tracked_store(request)
    docker = InspectRaceDocker(request, outcome)
    reply = execute_host(monkeypatch, request, docker, handoff_store=store)
    assert reply['ok'] is False and 'access_closed' not in reply
    assert store.cleanup_calls == 0 and store.released is True
    assert docker.ps_calls == 2
    assert 'private Docker diagnostics' not in str(reply)
    assert not any(command[1] in {'stop','rm','kill'} for command,_ in docker.calls)


def test_host_poll_reads_native_receipts_not_assistant_claims(monkeypatch, tmp_path):
    request = payload(tmp_path, "poll")
    directory = tmp_path / "runs" / "owned"
    trace = directory / "traces" / "episode-1"
    trace.mkdir(parents=True)
    (directory / "summary.json").write_text(json.dumps({"run_id":"owned", "status":"completed", "auth":"never returned"}))
    (trace / "events.jsonl").write_text('\n'.join(json.dumps(event) for event in [
        {"type":"assistant_text", "text":"unverified victory"},
        {"type":"command_run", "sequence":7, "command":"curl target", "output":"PENTESTGPT_FIXTURE_OK", "exit_code":0},
        {"type":"tool_result", "sequence":8, "output":"actual tool data"}])+'\n{"partial":')
    result = execute_host(monkeypatch, request, Docker(request))["result"]
    assert result["status"] == "completed"
    assert len(result["receipts"]) == 2
    assert result["receipts"][0]["episode_id"] == "episode-1"
    assert result["receipts"][0]["output"] == "PENTESTGPT_FIXTURE_OK"
    assert result["receipts_truncated"] is True
    assert "unverified victory" not in str(result) and "never returned" not in str(result)


def test_fixed_nmap_receipt_and_no_provider_invocation(tmp_path):
    runner = Runner()
    runtime = ComposePentestRuntime(config(tmp_path), runner=runner)
    result = asyncio.run(runtime.run_tool(run_id="nmap-owned", argv=nmap(), seconds=5))
    request = runner.calls[0][1]
    assert request["operation"] == "tool" and request["argv"][0] == "/usr/bin/nmap"
    assert result["provider_called"] is False and result["native_full_access"] is False
    assert result["receipts"][0]["exit_code"] == 0
    assert asyncio.run(runtime.poll("nmap-owned")) == result
    with pytest.raises(Denied):
        asyncio.run(runtime.run_tool(run_id="bad", argv=nmap()+("--script", "evil.nse"), seconds=5))
    with pytest.raises(Denied):
        asyncio.run(runtime.run_tool(run_id="bad", argv=("/bin/sh", "-c", "id"), seconds=5))
    assert len(runner.calls) == 1


def test_stop_terminal_and_secret_receipts_redacted(tmp_path):
    class SensitiveRunner(Runner):
        async def __call__(self, command, *, payload, timeout):
            request = json.loads(payload)
            if request["operation"] == "poll":
                result = {"status":"completed", "receipts":[{"type":"command_run", "exit_code":0,
                    "output":"Authorization: Bearer fixture-secret\nCookie: sensitive=session\nPENTESTGPT_FIXTURE_OK"}],
                    "api_key":"hidden-key"}
                return 0, json.dumps({"ok":True, "result":result}).encode(), b"private host diagnostics"
            return await super().__call__(command, payload=payload, timeout=timeout)
    runtime = ComposePentestRuntime(config(tmp_path), runner=SensitiveRunner())
    asyncio.run(runtime.start(run_id="owned", scope=scope(), osint_context=packet()))
    result = asyncio.run(runtime.poll("owned"))
    assert "fixture-secret" not in str(result) and "sensitive=session" not in str(result)
    assert "hidden-key" not in str(result) and "PENTESTGPT_FIXTURE_OK" in str(result)
    closed = asyncio.run(runtime.stop("owned"))
    assert closed["access_closed"] is True
    assert asyncio.run(runtime.poll("owned"))["status"] == "stopped"


def test_bad_response_does_not_expose_stderr_or_promise_closure(tmp_path):
    class BadRunner:
        async def __call__(self, command, *, payload, timeout):
            return 1, b"not json", b"private credential should never appear"
    runtime = ComposePentestRuntime(config(tmp_path), runner=BadRunner())
    with pytest.raises(Denied, match="closure may need verification") as error:
        asyncio.run(runtime.start(run_id="owned", scope=scope(), osint_context=packet()))
    assert "private credential" not in str(error.value)


def test_actual_launcher_timeout_uses_scope_budget_and_window(monkeypatch, tmp_path):
    monkeypatch.setattr("security_lab.compose_runtime.time.time", lambda: 1_800_000_000.4)
    runner = Runner()
    runtime = ComposePentestRuntime(config(tmp_path), runner=runner)
    asyncio.run(runtime.start(run_id="budget", scope=scope(budget={"seconds":90}), osint_context=packet(scope(budget={"seconds":90}))))
    assert runner.calls[0][1]["max_runtime_seconds"] == 90
    asyncio.run(runtime.start(run_id="window", scope=scope(expires_at=1_800_000_072), osint_context=packet(scope(expires_at=1_800_000_072))))
    assert runner.calls[1][1]["max_runtime_seconds"] == 71
    with pytest.raises(Denied, match="sixty seconds"):
        asyncio.run(runtime.start(run_id="short", scope=scope(budget={"seconds":59}), osint_context=packet()))
    assert len(runner.calls) == 2


def test_nmap_requires_host_selected_literal_addresses(tmp_path):
    runtime = ComposePentestRuntime(config(tmp_path), runner=Runner())
    with pytest.raises(Denied, match="addresses"):
        asyncio.run(runtime.run_tool(run_id="bad", argv=nmap()[:-1]+("pentest-fixture",), seconds=5))


def test_subfinder_actual_version_before_fixed_passive_command(tmp_path):
    class PassiveRunner(Runner):
        async def __call__(self, command, *, payload, timeout):
            request = json.loads(payload)
            self.calls.append((command, request, timeout))
            result = {'status':'completed', 'actual_version':'2.16.0', 'access_closed':True,
                'receipts_truncated':False, 'receipts':[{'type':'command_run', 'exit_code':0,
                'stdout':'{"host":"api.example.test","input":"example.test","sources":["crtsh"]}',
                'stderr':'', 'started_at':1000.0, 'finished_at':1001.0}]}
            return 0, json.dumps({'ok':True, 'result':result}).encode(), b''
    runner = PassiveRunner()
    runtime = ComposePentestRuntime(config(tmp_path), runner=runner)
    result = asyncio.run(runtime.run_subfinder(run_id='passive-owned', seed='example.test'))
    assert [request['operation'] for _,request,_ in runner.calls] == ['version', 'tool']
    assert runner.calls[1][1]['actual_version'] == '2.16.0'
    assert runner.calls[1][1]['argv'] == ['/usr/local/bin/subfinder', '-d', 'example.test', '-json', '-silent',
        '-collect-sources', '-timeout', '10', '-max-time', '1', '-disable-update-check']
    assert result['provider_called'] is False and result['actual_version'] == '2.16.0'
    assert result['scope_enforcement'] == 'host_typed_plan_and_existing_compose_networks'
    with pytest.raises(Denied, match='tools-only'):
        asyncio.run(ComposePentestRuntime(config(tmp_path, tool_service='pentestgpt'), runner=runner).run_subfinder(
            run_id='legacy', seed='example.test'))
    assert len(runner.calls) == 2


def test_host_version_real_stderr_and_passive_capture(monkeypatch, tmp_path):
    class PassiveDocker(Docker):
        def run(self, command, **kwargs):
            result = super().run(command, **kwargs)
            if '-version' in command:
                return SimpleNamespace(returncode=0, stdout='', stderr='[INF] Current Version: v2.16.0\n')
            if '/usr/local/bin/subfinder' in command:
                return SimpleNamespace(returncode=2, stdout='{"host":"api.example.test","input":"example.test"}\n', stderr='source unavailable')
            return result
    request = {**payload(tmp_path, 'version'), 'kind':'subfinder', 'argv':[], 'seconds':90}
    docker = PassiveDocker(request)
    version = execute_host(monkeypatch, request, docker)['result']
    assert version['actual_version'] == '2.16.0'
    command = next(command for command,_ in docker.calls if command[1] == 'compose')
    assert command[-3:] == ['/usr/local/bin/subfinder', '-version', '-disable-update-check']
    assert 'pentest-tools' in command and 'pentestgpt' not in command
    request.update(operation='tool', actual_version=version['actual_version'],
        argv=['/usr/local/bin/subfinder', '-d', 'example.test', '-json', '-silent', '-collect-sources',
              '-timeout', '10', '-max-time', '1', '-disable-update-check'])
    result = execute_host(monkeypatch, request, PassiveDocker(request))['result']
    receipt = result['receipts'][0]
    assert result['status'] == 'failed' and result['access_closed'] is True
    assert receipt['exit_code'] == 2 and receipt['stderr'] == 'source unavailable'
    assert receipt['started_at'] <= receipt['finished_at']
    assert result['actual_version'] == '2.16.0'


def test_host_version_failure_cannot_start_passive_scan(monkeypatch, tmp_path):
    request = {**payload(tmp_path, 'version'), 'kind':'subfinder', 'argv':[], 'seconds':90}
    docker = Docker(request)  # XML fake output has no actual semantic version.
    result = execute_host(monkeypatch, request, docker)
    assert result['ok'] is False
    commands = [command for command,_ in docker.calls if command[1] == 'compose']
    assert len(commands) == 1 and '-version' in commands[0]


def test_subfinder_unknown_version_stops_exact_handle_and_never_collects(tmp_path):
    class NoVersionRunner(Runner):
        async def __call__(self, command, *, payload, timeout):
            request = json.loads(payload)
            self.calls.append((command, request, timeout))
            result = {'status':'completed', 'access_closed':True} if request['operation'] == 'version' else {
                'status':'stopped', 'access_closed':True}
            return 0, json.dumps({'ok':True, 'result':result}).encode(), b''
    runner = NoVersionRunner()
    runtime = ComposePentestRuntime(config(tmp_path), runner=runner)
    with pytest.raises(Denied, match='Actual Subfinder version'):
        asyncio.run(runtime.run_subfinder(run_id='version-failed', seed='example.test'))
    assert [request['operation'] for _,request,_ in runner.calls] == ['version', 'stop']
    assert runner.calls[0][1]['owner'] == runner.calls[1][1]['owner']


def test_host_nmap_existing_compose_receipt(monkeypatch, tmp_path):
    request = {**payload(tmp_path, "tool"), "argv":list(nmap()), "seconds":5}
    docker = Docker(request)
    result = execute_host(monkeypatch, request, docker)["result"]
    command = next(command for command,_ in docker.calls if command[1] == "compose")
    assert "/usr/bin/timeout" in command and "--signal=TERM" in command
    assert command[-len(nmap()):] == list(nmap())
    assert "--network" not in command
    assert result["status"] == "completed" and result["access_closed"] is True
    assert result["receipts"][0]["stdout"] == docker.tool_result


@pytest.mark.parametrize("values", [{"mode":"disabled"}, {"max_runtime_seconds":True}, {"command_timeout_seconds":0},
                                     {"compose_directory":"relative"}, {"ssh_host":"-oProxyCommand=evil", "mode":"ssh"}])
def test_invalid_host_configuration_denied(tmp_path, values):
    base = {"mode":"local", "compose_directory":"/srv/apps/pentestgpt", "runs_directory":"/srv/app-data/pentestgpt/runs"}
    with pytest.raises(Denied):
        ComposeRuntimeConfig(**(base | values))


def test_poll_and_stop_never_retransmit_full_context(tmp_path):
    runner=Runner(); runtime=ComposePentestRuntime(config(tmp_path),runner=runner)
    asyncio.run(runtime.start(run_id='one', scope=scope(), osint_context=packet(data={'large':'x'*50000})))
    asyncio.run(runtime.poll('one')); asyncio.run(runtime.stop('one'))
    assert 'context_json' in runner.calls[0][1]
    assert all('context_json' not in request for _,request,_ in runner.calls[1:])
    assert all('handoff' in request for _,request,_ in runner.calls)
