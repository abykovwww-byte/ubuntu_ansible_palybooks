"""Developer STDIO qualification: exported map + receipts, never target checks."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


async def qualify(args):
    args.output.mkdir(parents=True, exist_ok=True)
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / 'server.py'),
        '--data-dir', str(args.output / 'controller'), '--principal', 'qualification-operator'], cwd=str(ROOT))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            async def call(name, payload):
                result = await session.call_tool(name, payload)
                if result.isError:
                    raise RuntimeError('Qualification tool denied: ' + name)
                return result.structuredContent
            status = await call('integration_status', {})
            created = await call('create_case', {'seed': args.seed})
            case_id = created['id']
            if args.public:
                await call('start_public_discovery', {'case_id': case_id})
                deadline = time.monotonic() + 210
                prior_count = -1
                while time.monotonic() < deadline:
                    case = await call('get_case', {'case_id': case_id})
                    count = len(case['source_receipts'])
                    if count != prior_count:
                        print(json.dumps({'stage': case['state'], 'receipts': count}, ensure_ascii=False), flush=True)
                        prior_count = count
                    if case['state'] != 'discovering':
                        break
                    await asyncio.sleep(1)
                else:
                    await call('stop_case', {'case_id': case_id})
                    raise RuntimeError('Discovery qualification deadline exceeded')
            else:
                case = await call('collect_osint_fixture', {'case_id': case_id})
            report = await call('get_report', {'case_id': case_id})
            resource = await session.read_resource(f'security-lab://case/{case_id}/report.html')
            (args.output / 'surface-map.html').write_text(resource.contents[0].text, encoding='utf-8')
            (args.output / 'report-preview.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            summary = {'case_id': case_id, 'seed': args.seed, 'synthetic': not args.public,
                       'mcp': status, 'stage': case['state'], 'runs': case['discovery_runs'],
                       'nodes': len(case['surface']), 'links': len(case['links']),
                       'receipts': len(case['source_receipts']), 'coverage': case['coverage'],
                       'checks_jobs': len(case['jobs']), 'manifests': len(case['manifests']),
                       'target_http_requests': 0, 'map': str(args.output / 'surface-map.html')}
            (args.output / 'qualification.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--seed', default='fixture.example.test')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--public', action='store_true', help='Fetch public third-party sources; no target HTTP or checks')
    asyncio.run(qualify(parser.parse_args()))
