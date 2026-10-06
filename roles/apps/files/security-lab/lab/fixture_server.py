"""Isolated dual-stack HTTPS fixture; records requests on the receiving side."""

import datetime
import http.server
import json
from pathlib import Path
import socket
import ssl
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


root = Path("/fixture")
root.mkdir(exist_ok=True)
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fixture.example.test")])
now = datetime.datetime.now(datetime.timezone.utc)
cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1))
        .not_valid_after(now+datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("fixture.example.test"),
                                                  x509.DNSName("fixture6.example.test")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256()))
(root / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
(root / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        with (root / "received.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": time.time(), "path": self.path, "method": self.command,
                                     "host": self.headers.get("Host"), "peer": self.client_address[0]}) + "\n")
        if self.path == "/public/redirect":
            self.send_response(302)
            self.send_header("Location", "https://outside.example.test:8443/secret")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/public/stream":
            self.send_response(200)
            self.send_header("Content-Length", "500000")
            self.end_headers()
            try:
                for i in range(500):
                    self.wfile.write(b"x" * 1000)
                    self.wfile.flush()
                    time.sleep(0.1)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            body = b"SYNTHETIC FIXTURE: authorized GET reached HTTPS upstream\n"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


class Server(http.server.ThreadingHTTPServer):
    address_family = socket.AF_INET6
    daemon_threads = True

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


server = Server(("::", 8443), Handler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(root / "cert.pem", root / "key.pem")
server.socket = context.wrap_socket(server.socket, server_side=True)
server.serve_forever()
