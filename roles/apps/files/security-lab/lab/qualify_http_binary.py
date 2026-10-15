"""Exercise the deployed tools image and SSH receipt path on loopback only.

No Controller/store, provider, external target, credentials or persistent files.
The fixture and curl run inside one short-lived, exact-owned tools container.
"""
import argparse
import json
from pathlib import Path
import secrets
import shlex
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from security_lab.compose_runtime import _HOST_SCRIPT
from security_lab.native_checks import evaluate_action_response


FIXTURE = r'''
import hashlib, http.server, io, json, subprocess, sys, threading, zipfile
body = b'------fixture\r\nContent-Disposition: form-data; name="file"; filename="test.rtf"\r\n\r\n{\\rtf1 safe}\r\n------fixture--\r\n'
archive = io.BytesIO()
with zipfile.ZipFile(archive, 'w') as output:
    output.writestr(zipfile.ZipInfo('word/document.xml', (2026, 1, 1, 0, 0, 0)), '<document>safe fixture</document>')
binary = archive.getvalue()
requests = []
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        received = self.rfile.read(int(self.headers['Content-Length']))
        requests.append((self.path, received == body))
        self.send_response(200 if self.path == '/convert' and received == body else 400)
        self.send_header('Content-Type', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document')
        self.send_header('Content-Length', str(len(binary)))
        self.end_headers()
        self.wfile.write(binary)
server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    result = subprocess.run(['/usr/bin/curl', '--disable', '--silent', '--show-error', '--noproxy', '*',
        '--http1.1', '--retry', '0', '--max-redirs', '0', '--max-time', '5', '--include',
        '--request', 'POST', '--header', 'content-type: multipart/form-data; boundary=----fixture',
        '--data-raw', body.decode(), '--write-out', '\nSECURITY_LAB_HTTP_STATUS:%{http_code}\n',
        '--url', 'http://127.0.0.1:' + str(server.server_port) + '/convert'], capture_output=True, timeout=8)
finally:
    server.shutdown()
    server.server_close()
assert requests == [('/convert', True)], requests
assert result.returncode == 0, result.returncode
sys.stdout.buffer.write(result.stdout)
sys.stderr.write('SECURITY_LAB_BINARY_FIXTURE:' + json.dumps({'loopback_requests': 1, 'multipart_exact': True,
    'response_sha256': hashlib.sha256(binary).hexdigest(), 'response_bytes': len(binary)}))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssh-host', default='abykov@192.168.1.88')
    parser.add_argument('--ssh-identity', required=True)
    args = parser.parse_args()
    identity = secrets.token_hex(12)
    request = {'operation': 'tool', 'kind': 'http', 'tool_service': 'pentest-tools',
        'compose_directory': '/srv/apps/pentestgpt', 'run_id': 'binary-fixture-' + identity,
        'container_name': 'security-lab-binary-fixture-' + identity, 'owner': identity,
        'command_timeout_seconds': 20, 'max_receipt_chars': 12000, 'seconds': 15,
        'argv': ['/usr/local/bin/python', '-c', FIXTURE]}
    execution = subprocess.run(['ssh', '-T', '-o', 'BatchMode=yes', '-i', args.ssh_identity,
        args.ssh_host, 'python3 -c ' + shlex.quote(_HOST_SCRIPT)],
        input=json.dumps(request).encode(), capture_output=True, timeout=60)
    reply = json.loads(execution.stdout)
    assert execution.returncode == 0 and reply.get('ok'), 'Host diagnostic failed'
    receipt = reply['result']
    row = receipt['receipts'][0]
    assert receipt['access_closed'] and row['exit_code'] == 0 and not receipt['receipts_truncated']
    fixture = json.loads(row['stderr'].rsplit('SECURITY_LAB_BINARY_FIXTURE:', 1)[1])
    assert row['response_body']['sha256'] == fixture['response_sha256']
    assert row['response_body']['size_bytes'] == fixture['response_bytes']
    assert row['response_body']['prefix_hex'].startswith('504b0304')
    assert evaluate_action_response(receipt, {'status': 200})['poc_observation'] == 'expected_observation_matched'
    print(json.dumps({'qualification': 'synthetic_loopback_not_target_evidence',
        'target_requests': 0, 'provider_calls': 0, 'access_closed': True,
        'http_status': 200, **fixture, 'response_body': row['response_body']}, indent=2))


if __name__ == '__main__':
    main()
