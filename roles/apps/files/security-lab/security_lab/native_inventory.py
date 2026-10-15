"""Fixed zero-target image inventory. Never reads credentials or invokes a model."""

INVENTORY_PROGRAM = r'''
import json, subprocess
tools = {}
for name, argv in {'curl':['/usr/bin/curl','--version'], 'nmap':['/usr/bin/nmap','--version'],
                   'openssl':['/usr/bin/openssl','version']}.items():
    try:
        value = subprocess.run(argv, capture_output=True, text=True, timeout=5)
        tools[name] = {'available':value.returncode == 0, 'version':value.stdout.splitlines()[:1]}
    except (OSError, subprocess.TimeoutExpired):
        tools[name] = {'available':False, 'version':[]}
try:
    value = subprocess.run(['/usr/local/bin/pentestgpt-run','--capabilities'], capture_output=True, text=True, timeout=5)
    launcher = json.loads(value.stdout) if value.returncode == 0 else {'native_targets_revision':1}
except (OSError, ValueError, subprocess.TimeoutExpired):
    launcher = {'native_targets_revision':1}
print(json.dumps({'tools':tools,'launcher':launcher,'target_requests':0,'provider_calls':0}))
'''
