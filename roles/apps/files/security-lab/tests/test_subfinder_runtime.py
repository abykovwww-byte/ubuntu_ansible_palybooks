import asyncio
import json

import pytest

from security_lab.controller import Controller
from security_lab.subfinder_runtime import SubfinderDiscovery


class Runtime:
    def __init__(self, stdout='', exit_code=0, truncated=False):
        self.stdout, self.exit_code, self.truncated = stdout, exit_code, truncated
        self.started = []
        self.stopped = []
        self.block = None
        self.failure = False
        self.closed = True

    async def run_subfinder(self, *, run_id, seed, seconds):
        self.started.append((run_id, seed, seconds))
        if self.block:
            await self.block.wait()
        if self.failure:
            raise RuntimeError('private provider credential diagnostic')
        return {'status':'completed' if self.exit_code == 0 else 'failed', 'synthetic':True,
            'actual_version':'2.16.0', 'access_closed':True, 'receipts_truncated':self.truncated,
            'receipts':[{'type':'command_run', 'stdout':self.stdout, 'stderr':'private unused output',
                'exit_code':self.exit_code, 'started_at':1000.0, 'finished_at':1001.0}]}

    async def stop(self, run_id):
        self.stopped.append(run_id)
        return {'access_closed':self.closed, 'status':'stopped'}


@pytest.fixture
def controller(tmp_path):
    value = Controller(tmp_path / 'cases', 'operator', clock=lambda: 1001.0)
    yield value
    value.close()


def output():
    return json.dumps({'host':'api.example.test', 'input':'example.test', 'sources':['crtsh']})


@pytest.mark.parametrize('stdout,exit_code,truncated,status,receipt_status', [
    (output(),0,False,'completed','success'), ('',0,False,'completed','empty'),
    ('',2,False,'failed','error'), (output(),2,False,'partial','partial'),
    (output(),0,True,'partial','partial')])
def test_passive_result_durable_without_scopeapproval(controller, stdout, exit_code, truncated, status, receipt_status):
    async def exercise():
        case_id = controller.create_case('example.test')['id']
        runtime = Runtime(stdout, exit_code, truncated)
        service = SubfinderDiscovery(controller, runtime)
        run = await service.start(case_id)
        assert run['scope_approval_granted'] is False and run['availability'] == 'pending_actual_cli_receipt'
        task = service.tasks[case_id]
        await task
        case = controller.get_case(case_id)
        assert case['discovery_runs'][0]['status'] == status
        assert case['manifests'] == [] and case['state'] == 'discovery_review'
        receipt = controller.export(case_id)['source_receipts'][0]
        assert receipt['status'] == receipt_status and receipt['synthetic'] == 1
        assert receipt['source'] == 'subfinder' and receipt['http_status'] is None
        assert 'private unused output' not in receipt['body']
        assert runtime.started == [('subfinder-'+run['id'], 'example.test', 90)]
        if stdout:
            assert any(node['value'] == 'api.example.test' for node in case['surface'])
    asyncio.run(exercise())


def test_runtime_failure_is_error_receipt_not_empty_or_raw_diagnostic(controller):
    async def exercise():
        case_id = controller.create_case('example.test')['id']
        runtime = Runtime()
        runtime.failure = True
        service = SubfinderDiscovery(controller, runtime)
        await service.start(case_id)
        await service.tasks[case_id]
        report = controller.export(case_id)
        receipt = report['source_receipts'][0]
        assert receipt['status'] == 'error'
        assert json.loads(receipt['body'])['metadata']['tool_version'] == 'unknown'
        assert 'private provider' not in str(report)
        assert report['discovery_runs'][0]['status'] == 'failed'
        assert len(runtime.stopped) == 1
    asyncio.run(exercise())


def test_cancel_and_shutdown_finish_interrupted_and_exact_runtime_closed(controller):
    async def exercise():
        case_id = controller.create_case('example.test')['id']
        runtime = Runtime()
        runtime.block = asyncio.Event()
        service = SubfinderDiscovery(controller, runtime)
        run = await service.start(case_id)
        await asyncio.sleep(0)
        result = await service.shutdown()
        assert result['access_closed'] is True
        assert runtime.stopped == ['subfinder-'+run['id']]
        case = controller.get_case(case_id)
        assert case['discovery_runs'][0]['status'] == 'interrupted'
        assert case['state'] == 'discovery_review'
        assert case['surface'] == []
    asyncio.run(exercise())


def test_stop_before_task_first_turn_never_launches_and_finishes(controller):
    async def exercise():
        case_id = controller.create_case('example.test')['id']
        runtime = Runtime()
        service = SubfinderDiscovery(controller, runtime)
        await service.start(case_id)
        result = await service.stop_case(case_id)
        assert result['access_closed'] is True
        assert runtime.started == runtime.stopped == []
        assert controller.get_case(case_id)['discovery_runs'][0]['status'] == 'interrupted'
    asyncio.run(exercise())


def test_controller_stop_prevents_late_output_and_preserves_paused_state(controller):
    async def exercise():
        case_id = controller.create_case('example.test')['id']
        runtime = Runtime(output())
        runtime.block = asyncio.Event()
        service = SubfinderDiscovery(controller, runtime)
        await service.start(case_id)
        task = service.tasks[case_id]
        await asyncio.sleep(0)
        controller.stop(case_id)
        runtime.block.set()
        await task
        case = controller.get_case(case_id)
        assert case['state'] == 'paused' and case['surface'] == []
        assert case['source_receipts'] == []
        assert case['discovery_runs'][0]['status'] == 'cancelled'
    asyncio.run(exercise())


def test_unconfirmed_closure_remains_gap_after_cancellation(controller):
    async def exercise():
        case_id = controller.create_case('example.test')['id']
        runtime = Runtime()
        runtime.block, runtime.closed = asyncio.Event(), False
        service = SubfinderDiscovery(controller, runtime)
        await service.start(case_id)
        await asyncio.sleep(0)
        result = await service.stop_case(case_id)
        assert result['access_closed'] is False
        case = controller.get_case(case_id)
        assert case['discovery_runs'][0]['status'] == 'interrupted'
        assert 'closure_unconfirmed' in case['discovery_runs'][0]['reason']
    asyncio.run(exercise())
