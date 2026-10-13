"""Offline protocol/client regressions; actual host smoke runs in Docker build/CI."""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / 'roles/apps/files/pentestgpt/test_codex_host.py'
spec = importlib.util.spec_from_file_location('pentest_codex_host_smoke', SCRIPT)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class Input:
    def __init__(self):
        self.frames = []
        self.closed = False

    def write(self, value):
        length = struct.unpack('<I', value[:4])[0]
        assert length == len(value) - 4
        self.frames.append(json.loads(value[4:]))

    async def drain(self):
        pass

    def close(self):
        self.closed = True


def replies():
    def response(kind, request_id, value):
        return {'type': kind, 'id': request_id, 'result': {'status': 'ok', 'value': value}}
    return [
        {'type': 'connection/ready', 'selectedVersion': 1, 'capabilities': []},
        response('operation/response', 1, {'type': 'session/ready', 'sessionId': smoke.SESSION}),
        response('operation/response', 2, {'type': 'execution/started', 'cellId': 'cell-1'}),
        {'type': 'delegate/request', 'id': 10, 'sessionId': smoke.SESSION,
         'request': {'type': 'tool/invoke', 'invocation': {'cell_id': 'cell-1',
             'runtime_tool_call_id': 'nested-1', 'tool_name': {'name': 'exec_command', 'namespace': None},
             'tool_kind': 'function', 'input': {'cmd': 'OFFLINE_SHELL_STUB_ONLY'}}}},
        response('execute/initialResponse', 2, {'Result': {'cell_id': 'cell-1', 'error_text': None,
            'content_items': [{'type': 'input_text', 'text': smoke.MARKER}], 'code_mode_host_duration_ns': 1}}),
        {'type': 'cell/closed', 'sessionId': smoke.SESSION, 'cellId': 'cell-1'},
        response('operation/response', 3, {'type': 'session/closed', 'sessionId': smoke.SESSION}),
    ]


class Process:
    def __init__(self, messages):
        self.stdin = Input()
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(b''.join(smoke.encode_frame(row) for row in messages))
        self.stdout.feed_eof()
        self.returncode = None
        self.killed = False

    async def wait(self):
        self.returncode = 0
        return 0

    def kill(self):
        self.killed = True


class CodexHostSmokeTests(unittest.TestCase):
    def test_wire_codec_uses_upstream_little_endian_length_and_utf8_bytes(self):
        frame = smoke.encode_frame({'type': 'fixture', 'text': 'данные'})
        self.assertEqual(struct.unpack('<I', frame[:4])[0], len(frame[4:]))
        async def run():
            reader = asyncio.StreamReader()
            reader.feed_data(frame)
            self.assertEqual(await smoke.read_frame(reader), {'type': 'fixture', 'text': 'данные'})
        asyncio.run(run())

    def test_oversize_and_nonobject_response_frames_are_refused(self):
        async def run(payload):
            reader = asyncio.StreamReader()
            reader.feed_data(payload)
            reader.feed_eof()
            await smoke.read_frame(reader)
        for payload in (struct.pack('<I', smoke.MAX_FRAME_BYTES + 1), struct.pack('<I', 2) + b'[]'):
            with self.subTest(payload=payload[:4]), self.assertRaises(ValueError):
                asyncio.run(run(payload))

    def test_missing_paired_host_fails_before_cli_or_provider_process(self):
        with patch.object(Path, 'is_file', side_effect=[True, False]), \
                patch.object(smoke.os, 'access', return_value=True), \
                patch.object(smoke.subprocess, 'run') as runner:
            with self.assertRaisesRegex(AssertionError, 'Required Codex executable missing'):
                smoke.check_binaries(smoke.CLI, smoke.HOST, smoke.clean_environment('/empty'))
        runner.assert_not_called()

    def test_helpers_must_be_executable_siblings(self):
        with self.assertRaisesRegex(AssertionError, 'siblings'):
            smoke.check_binaries(smoke.CLI, Path('/other/codex-code-mode-host'), {})
        with patch.object(Path, 'is_file', return_value=True), patch.object(smoke.os, 'access', return_value=False):
            with self.assertRaisesRegex(AssertionError, 'Required Codex executable missing'):
                smoke.check_binaries(smoke.CLI, smoke.HOST, {})

    def test_actual_version_and_help_commands_have_clean_environment_no_auth(self):
        environment = smoke.clean_environment('/empty')
        self.assertEqual(environment, {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': '/empty', 'CODEX_HOME': '/empty'})
        with patch.object(Path, 'is_file', return_value=True), \
                patch.object(smoke.os, 'access', return_value=True), \
                patch.object(smoke.subprocess, 'run', side_effect=[SimpleNamespace(stdout=smoke.EXPECTED_VERSION),
                    SimpleNamespace(stdout='--listen <URL> [default: stdio]')]) as runner:
            smoke.check_binaries(smoke.CLI, smoke.HOST, environment)
        self.assertEqual([call.args[0] for call in runner.call_args_list],
                         [[str(smoke.CLI), '--version'], [str(smoke.HOST), '--help']])
        self.assertTrue(all(call.kwargs['env'] == environment and call.kwargs['timeout'] == 5
                            for call in runner.call_args_list))

    def test_mismatched_cli_version_is_refused_before_host_launch(self):
        with patch.object(Path, 'is_file', return_value=True), patch.object(smoke.os, 'access', return_value=True), \
                patch.object(smoke.subprocess, 'run', return_value=SimpleNamespace(stdout='codex-cli 0.161.0')) as runner:
            with self.assertRaisesRegex(AssertionError, 'version differs'):
                smoke.check_binaries(smoke.CLI, smoke.HOST, {})
        self.assertEqual(runner.call_count, 1)

    def test_protocol_executes_static_js_and_returns_only_inmemory_shell_stub(self):
        async def run():
            process = Process(replies())
            environment = smoke.clean_environment('/empty')
            with patch.object(smoke.asyncio, 'create_subprocess_exec', return_value=process) as launcher:
                await smoke.exercise_host(smoke.HOST, environment)
            launcher.assert_called_once_with(str(smoke.HOST), '--listen', 'stdio',
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=environment)
            self.assertTrue(process.stdin.closed)
            self.assertFalse(process.killed)
            frames = process.stdin.frames
            self.assertEqual(frames[0]['type'], 'connection/hello')
            self.assertEqual(frames[2]['request']['method'], 'session/execute')
            self.assertEqual(frames[2]['request']['request']['enabled_tools'][0]['name'], 'exec_command')
            self.assertEqual(frames[3]['type'], 'delegate/response')
            self.assertEqual(frames[3]['result']['value']['result']['stdout'], smoke.MARKER)
            self.assertEqual(frames[-1]['request']['method'], 'session/shutdown')
        asyncio.run(run())

    def test_unexpected_delegated_command_is_refused_and_host_is_killed(self):
        async def run():
            messages = replies()
            messages[3]['request']['invocation']['input'] = {'cmd': 'curl https://example.test'}
            process = Process(messages)
            with patch.object(smoke.asyncio, 'create_subprocess_exec', return_value=process):
                with self.assertRaisesRegex(AssertionError, 'Unexpected offline nested-tool'):
                    await smoke.exercise_host(smoke.HOST, {})
            self.assertTrue(process.killed)
            self.assertFalse(any(row['type'] == 'delegate/response' for row in process.stdin.frames))
        asyncio.run(run())

    def test_failed_js_result_cannot_pass_on_matching_marker(self):
        async def run():
            messages = replies()
            messages[4]['result']['value']['Result']['error_text'] = 'fixture error'
            process = Process(messages)
            with patch.object(smoke.asyncio, 'create_subprocess_exec', return_value=process):
                with self.assertRaisesRegex(AssertionError, 'did not return the stub marker'):
                    await smoke.exercise_host(smoke.HOST, {})
            self.assertTrue(process.killed)
        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
