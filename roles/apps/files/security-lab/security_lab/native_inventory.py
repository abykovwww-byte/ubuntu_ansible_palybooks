"""Fixed zero-target image inventory. Never reads credentials or invokes a model."""

INVENTORY_PROGRAM = r'''
import json, re, subprocess
tools = {}
for name, argv in {'curl':['/usr/bin/curl','--version'], 'nmap':['/usr/bin/nmap','--version'],
                   'openssl':['/usr/bin/openssl','version'],
                   'testssl':['/usr/local/bin/testssl','--version'],
                   'tcpdump':['/usr/bin/tcpdump','--version'],
                   'nft':['/usr/sbin/nft','--version'],
                   'ffuf':['/usr/local/bin/ffuf','-V'],
                   'playwright':['/opt/web-tools-venv/bin/python','-m','playwright','--version'],
                   'chromium':['/opt/web-tools-venv/bin/python','-c',
                       'from playwright.sync_api import sync_playwright; import subprocess; p=sync_playwright().start(); subprocess.run([p.chromium.executable_path,"--version"],check=True); p.stop()'],
                   'zap':['/opt/zap/zap.sh','-Xmx256m','-cmd','-dir','/tmp/security-lab-zap-inventory','-version']}.items():
    try:
        value = subprocess.run(argv, capture_output=True, text=True, timeout=10)
        lines = [re.sub(r'\x1b\[[0-9;]*m','',line).strip() for line in value.stdout.splitlines() if line.strip()]
        if name == 'testssl':
            lines = [line for line in lines if line.startswith('testssl.sh version ')]
        elif name == 'zap':
            lines = [line for line in lines if re.fullmatch(r'\d+\.\d+\.\d+',line)]
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
