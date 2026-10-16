"""Actual collector image: internal-network direct egress must fail closed."""

import json
import subprocess
import uuid


def run(*args, check=True):
    result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=60)
    if check and result.returncode:
        raise RuntimeError('Synthetic collector network fixture failed')
    return result


def main():
    ident = uuid.uuid4().hex[:10]
    internal, external = 'tg-internal-' + ident, 'tg-external-' + ident
    target = 'tg-target-' + ident
    try:
        run('network', 'create', '--internal', internal)
        run('network', 'create', external)
        run('run', '-d', '--name', target, '--network', external,
            '--entrypoint', 'python', 'telegram-collector-ci', '-m', 'http.server', '2080')
        host = json.loads(run('inspect', target).stdout)[0]['NetworkSettings']['Networks'][external]['IPAddress']
        # Positive control uses the target's own normal network, with no public services.
        positive = 'import socket,time\nfor i in range(20):\n try:\n  socket.create_connection(("' + host + '",2080),1).close();break\n except OSError: time.sleep(.1)\nelse: raise SystemExit(1)'
        run('run', '--rm', '--network', external, '--entrypoint', 'python',
            'telegram-collector-ci', '-c', positive)
        negative = '''import socket
from tg_archive.network import system_proxy
p = system_proxy()
assert (p['proxy_type'],p['addr'],p['port']) == ('socks5','vpn-egress',1080)
try:
    socket.create_connection((TARGET,2080),2).close()
except OSError:
    pass
else:
    raise AssertionError('Direct egress unexpectedly succeeded')
from types import SimpleNamespace
from tg_archive.telegram import make_client
client = make_client(SimpleNamespace(require_credentials=lambda:None, api_id=1,api_hash='0'*32),'')
assert client._proxy == p
import asyncio
from python_socks import ProxyError
async def verify_no_proxy():
    try:
        await asyncio.wait_for(client.connect(),3)
    except (OSError,TimeoutError,ProxyError):
        pass
    else:
        raise AssertionError('MTProto unexpectedly connected without its proxy')
    finally:
        await client.disconnect()
asyncio.run(verify_no_proxy())
'''.replace('TARGET', repr(host))
        run('run', '--rm', '--network', internal, '--read-only', '--cap-drop', 'ALL',
            '--entrypoint', 'python', 'telegram-collector-ci', '-c', negative)
        print(json.dumps({'fixture': 'actual-collector-internal-network', 'status': 'ok',
                          'target_positive_control': True, 'direct_egress_denied': True,
                          'mtproto_without_proxy_denied': True,
                          'actual_telethon_proxy': 'vpn-egress:1080'}))
    finally:
        run('rm', '-f', target, check=False)
        run('network', 'rm', internal, external, check=False)


if __name__ == '__main__':
    main()
