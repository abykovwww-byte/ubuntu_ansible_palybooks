"""Fixed zero-target image inventory. Never reads credentials or invokes a model."""

INVENTORY_PROGRAM = r'''
import json, re, subprocess
tools = {}
for name, argv in {'curl':['/usr/bin/curl','--version'], 'nmap':['/usr/bin/nmap','--version'],
                   'openssl':['/usr/bin/openssl','version'],
                   'testssl':['/usr/local/bin/testssl','--version'],
                   'tcpdump':['/usr/bin/tcpdump','--version'],
                   'nft':['/usr/sbin/nft','--version']}.items():
    try:
        value = subprocess.run(argv, capture_output=True, text=True, timeout=5)
        lines = [re.sub(r'\x1b\[[0-9;]*m','',line).strip() for line in value.stdout.splitlines() if line.strip()]
        if name == 'testssl':
            lines = [line for line in lines if line.startswith('testssl.sh version ')]
        tools[name] = {'available':value.returncode == 0 and bool(lines), 'version':lines[:1]}
    except (OSError, subprocess.TimeoutExpired):
        tools[name] = {'available':False, 'version':[]}
try:
    value = subprocess.run(['/usr/local/bin/pentestgpt-run','--capabilities'], capture_output=True, text=True, timeout=5)
    launcher = json.loads(value.stdout) if value.returncode == 0 else {'native_targets_revision':1}
except (OSError, ValueError, subprocess.TimeoutExpired):
    launcher = {'native_targets_revision':1}
print(json.dumps({'tools':tools,'launcher':launcher,'target_requests':0,'provider_calls':0}))
'''
