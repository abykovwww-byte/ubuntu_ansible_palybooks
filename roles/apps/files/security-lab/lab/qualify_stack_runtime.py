"""Read live stack capabilities through the installed bridge; no target jobs."""
import asyncio
import json
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def inspect():
    root = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(command=sys.executable, args=[str(root / 'client_proxy.py'), '--settings',
        str(Path.home() / '.local/share/security-lab/service.json'), '--client', 'opencode'])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            async def call(name, args=None):
                response = await session.call_tool(name, args or {})
                if response.isError:
                    raise RuntimeError('Stack diagnostic failed: ' + name)
                return response.structuredContent
            async def revisions():
                result, offset = {}, 0
                while True:
                    page = await call('list_cases', {'offset':offset})
                    for case in page['cases']:
                        view = await call('get_case', {'case_id':case['id']})
                        result[case['id']] = view['revision']
                    offset = page['next_offset']
                    if offset is None:
                        return result
            before = await revisions()
            tools = {tool.name for tool in (await session.list_tools()).tools}
            required = {'start_poc_action','start_service_check','start_pentestgpt_run','get_runtime_inventory'}
            assert required <= tools
            inventory = await call('get_runtime_inventory')
            assert before == await revisions()
            return {'mcp_tools_present':sorted(required), 'case_history_unchanged':True,
                    'inventory':inventory, 'target_requests':0,'provider_calls':0}


if __name__ == '__main__':
    print(json.dumps(asyncio.run(inspect()), ensure_ascii=False, indent=2))
