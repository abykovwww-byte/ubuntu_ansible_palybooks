"""Owned HTTPS endpoint; its certificate is the actual endpoint certificate."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import ssl
import subprocess
import threading
import time

ROOT = Path('/fixture')
LOCK = threading.Lock()

def record(name, value):
    with LOCK, (ROOT / name).open('a', encoding='utf-8') as target:
        target.write(json.dumps({**value, 'at': time.time()}) + '\n')

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *args):
        pass
    def do_HEAD(self):
        self.do_GET(head=True)
    def do_GET(self, head=False):
        record('received.jsonl', {'path': self.path, 'method': self.command,
                                  'host': self.headers.get('Host'), 'peer': self.client_address[0],
                                  'peer_port': self.client_address[1],
                                  'port': self.server.server_port})
        stream = self.path == '/public/stream'
        body = b'OWNED DIRECT HTTPS FIXTURE\n'
        self.send_response(302 if self.path == '/public/redirect' else 200)
        if self.path == '/public/redirect':
            self.send_header('Location', 'https://outside.example.test:8443/public/info')
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Connection', 'close')
        if not stream:
            self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if head:
            return
        closed_reason = 'natural_end'
        try:
            if stream:
                for _ in range(480):
                    self.wfile.write(b'owned-stream\n')
                    self.wfile.flush()
                    time.sleep(.25)
            else:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError) as error:
            closed_reason = type(error).__name__
        finally:
            if stream:
                record('stream_closed.jsonl', {'peer': self.client_address[0],
                                               'peer_port': self.client_address[1],
                                               'reason': closed_reason})

class Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6
    daemon_threads = True

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8443)
    parser.add_argument('--extra-port', type=int)
    args = parser.parse_args()
    ROOT.mkdir(exist_ok=True)
    if not (ROOT / 'cert.pem').exists():
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', str(ROOT / 'key.pem'), '-out', str(ROOT / 'cert.pem'),
                        '-days', '1', '-subj', '/CN=fixture.example.test',
                        '-addext', 'subjectAltName=DNS:fixture.example.test,DNS:fixture6.example.test'],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (ROOT / 'key.pem').chmod(0o600)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(ROOT / 'cert.pem', ROOT / 'key.pem')
    servers = []
    for port in [args.port] + ([args.extra_port] if args.extra_port else []):
        server = Server(('::', port), Handler)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        servers.append(server)
    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    (ROOT / 'ready.json').write_text(json.dumps({'ports': [s.server_port for s in servers]}), 'utf-8')
    servers[0].serve_forever()
