"""Protocol checks for native tools; injected runtime never contacts a target."""
import asyncio
import json
from types import SimpleNamespace

from mcp.shared.memory import create_connected_server_and_client_session

from security_lab.mcp_server import build_server


class PassiveRuntime:
    config = SimpleNamespace(tool_service='pentest-tools')

    def __init__(self, blocked=False):
        self.calls = []
        self.closed = []
        self.blocked = blocked

    async def run_subfinder(self, *, run_id, seed, seconds):
        self.calls.append((run_id, seed, seconds))
        if self.blocked:
            await asyncio.Event().wait()
        return {'actual_version':'2.16.0', 'access_closed':True, 'synthetic':True,
            'receipts_truncated':False, 'receipts':[{'stdout':json.dumps({
                'host':'api.example.test', 'input':seed, 'sources':['crtsh']}),
                'exit_code':0, 'started_at':1000.0, 'finished_at':1001.0}]}

    async def stop(self, run_id):
        self.closed.append(run_id)
        return {'access_closed':True}


def test_native_inventory_default_denies_execution_without_store(tmp_path):
    async def exercise():
        directory = tmp_path / 'unused'
        async with create_connected_server_and_client_session(build_server(directory, 'fixture')) as session:
            names = {t.name for t in (await session.list_tools()).tools}
            assert {'start_subfinder_discovery', 'start_nmap_scan', 'start_pentestgpt_run'} <= names
            status = (await session.call_tool('integration_status', {})).structuredContent
            assert status['live_execution'] is False
            denied = await session.call_tool('start_subfinder_discovery', {'case_id':'absent'})
            assert denied.isError and not directory.exists()
    asyncio.run(exercise())


def test_passive_mcp_receipts_map_and_chat_stop(tmp_path):
    async def exercise():
        runtime = PassiveRuntime()
        server = build_server(tmp_path / 'cases', 'fixture', pentest_runtime=runtime)
        async with create_connected_server_and_client_session(server) as session:
            assert runtime.calls == []
            created = await session.call_tool('create_case', {'seed':'example.test'})
            case_id = created.structuredContent['id']
            started = await session.call_tool('start_subfinder_discovery', {'case_id':case_id})
            assert not started.isError
            assert started.structuredContent['scope_approval_granted'] is False
            for _ in range(20):
                report = (await session.call_tool('get_report', {'case_id':case_id})).structuredContent
                if report['source_receipts']:
                    break
                await asyncio.sleep(.01)
            assert report['source_receipts'][0]['source'] == 'subfinder'
            assert report['source_receipts'][0]['synthetic'] == 1
            assert any(node['value'] == 'api.example.test' for node in report['surface'])
            assert report['manifests'] == [] and report['jobs'] == []
            runtime.blocked = True
            second = await session.call_tool('start_subfinder_discovery', {'case_id':case_id})
            assert not second.isError
            stopped = await session.call_tool('stop_case', {'case_id':case_id})
            assert stopped.structuredContent['state'] == 'paused'
            assert stopped.structuredContent['discovery_access_closed'] is True
            assert len(runtime.closed) == 1
            assert stopped.structuredContent['discovery_runs'][-1]['status'] == 'cancelled'
    asyncio.run(exercise())
