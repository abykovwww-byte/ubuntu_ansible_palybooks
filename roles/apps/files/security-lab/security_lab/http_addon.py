"""Mandatory mitmproxy interceptor, one protected policy/process per job."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import socket
import time

from mitmproxy import ctx, http
from mitmproxy.exceptions import OptionsError

from .contracts import Denied
from .http_policy import HTTPJobPolicy, Lease, LeaseClock, request_permitted


log = logging.getLogger("security_lab.http")


class ScopeProxy:
    def __init__(self):
        self.policy = None
        self.lease = None
        self.lease_body = None
        self.tunnels = {}
        self.active = set()
        self.count = 0
        self.last_request = 0.0
        self.watchdog = None

    def load(self, loader):
        loader.add_option("sl_policy", str, "", "Protected immutable HTTP job policy file")
        loader.add_option("sl_lease", str, "", "Protected host-written lease file")

    def configure(self, updated):
        if not {"sl_policy", "sl_lease"} & updated:
            return
        if self.policy is not None:
            raise OptionsError("Job policy cannot be reconfigured in a running proxy")
        try:
            self.policy = HTTPJobPolicy.model_validate_json(Path(ctx.options.sl_policy).read_text("utf-8"))
            self.lease = LeaseClock(self.policy)
            self.refresh()
        except Exception as exc:
            raise OptionsError(f"Invalid job policy/lease: {type(exc).__name__}") from exc
        # Prevent a future launcher configuration from turning interception into
        # arbitrary TCP passthrough or contacting upstream during client TLS.
        ctx.options.update(connection_strategy="lazy", upstream_cert=False, rawtcp=False,
                           ssl_insecure=False, ignore_hosts=[], allow_hosts=[], tcp_hosts=[],
                           udp_hosts=[], websocket=False, http3=False, body_size_limit="1m")

    def refresh(self):
        body = Path(ctx.options.sl_lease).read_text("utf-8")
        if body != self.lease_body:
            self.lease.update(Lease.model_validate_json(body))
            self.lease_body = body
        self.lease.check()

    async def running(self):
        async def watch():
            while True:
                try:
                    self.refresh()
                    # Close transports before the kernel lease expires, so a
                    # client observes EOF as well as denied packets. No access
                    # is extended: the proxy stops up to one second early.
                    if self.lease.monotonic_until - self.lease.monotonic() <= 1:
                        raise Denied("Lease approaching expiry")
                except Exception:
                    log.warning("job_access_closed reason=lease_or_controller_loss")
                    ctx.master.shutdown()
                    return
                await asyncio.sleep(0.25)
        self.watchdog = asyncio.create_task(watch())

    def done(self):
        if self.watchdog:
            self.watchdog.cancel()

    def deny(self, flow, reason):
        # No raw URL query, body, credentials or token is written to logs.
        log.warning("request_blocked reason=%s", reason)
        flow.response = http.Response.make(403, b"security-lab: request denied\n", {"Content-Type": "text/plain"})

    def client_connected(self, client):
        try:
            self.refresh()
            if not client.peername or client.peername[0] not in self.policy.worker_addresses:
                raise Denied("Client outside assigned worker")
        except Exception:
            client.error = "security-lab: client denied"

    def client_disconnected(self, client):
        self.tunnels.pop(client.id, None)

    def http_connect(self, flow):
        try:
            self.refresh()
            pair = (flow.request.host, flow.request.port)
            if not any((t.host, t.port, t.scheme) == (*pair, "https") for t in self.policy.scope.targets):
                raise Denied("CONNECT outside HTTPS scope")
            if flow.client_conn.id in self.tunnels:
                raise Denied("Nested CONNECT")
            self.tunnels[flow.client_conn.id] = pair
        except Exception:
            self.deny(flow, "connect_scope")

    def tls_clienthello(self, data):
        try:
            self.refresh()
            connect = self.tunnels.get(data.context.client.id)
            if not connect or data.client_hello.sni != connect[0]:
                raise Denied("CONNECT/SNI mismatch")
            if any(p not in {b"h2", b"http/1.1"} for p in data.client_hello.alpn_protocols):
                raise Denied("Non-HTTP ALPN")
            data.establish_server_tls_first = False
            data.ignore_connection = False
        except Exception:
            # Per-job process shutdown is fail-closed even before a flow exists.
            log.warning("job_access_closed reason=tls_identity")
            ctx.master.shutdown()

    def requestheaders(self, flow):
        try:
            self.refresh()
            request_permitted(self.policy, scheme=flow.request.scheme, host=flow.request.host,
                              port=flow.request.port, method=flow.request.method,
                              raw_path=flow.request.data.path,
                              host_headers=flow.request.headers.get_all("host"),
                              http_authority=flow.request.authority,
                              connect=self.tunnels.get(flow.client_conn.id), sni=flow.client_conn.sni)
            if any(name in flow.request.headers for name in (
                "upgrade", "transfer-encoding", "x-http-method-override", "x-method-override"
            )):
                raise Denied("Unsupported protocol/framing/overridden method")
            if flow.request.trailers:
                raise Denied("Trailers unsupported")
            if flow.id in self.active:
                raise Denied("Reused flow")
            now = time.monotonic()
            if self.count >= self.policy.max_requests or len(self.active) >= self.policy.scope.budget.concurrency:
                raise Denied("Request/concurrency budget exhausted")
            if now - self.last_request < 1 / self.policy.scope.budget.rps:
                raise Denied("Request rate exceeded")
            self.count += 1
            self.last_request = now
            self.active.add(flow.id)
            flow.metadata["sl_checked"] = True
            # The effective hostname remains for upstream TLS validation/SNI.
            flow.server_conn.sni = flow.request.host
        except Exception as exc:
            self.deny(flow, type(exc).__name__)

    def request(self, flow):
        # Recheck after buffering a body: a revoked slow upload cannot escape.
        if flow.metadata.get("sl_checked"):
            try:
                self.refresh()
            except Exception:
                self.deny(flow, "lease_after_body")

    def responseheaders(self, flow):
        # Do not hold large/long responses in memory; watchdog closes the actual
        # transport on revoke, including a response already being streamed.
        if flow.metadata.get("sl_checked"):
            flow.response.stream = True

    async def server_connect(self, data):
        try:
            self.refresh()
            host, port = data.server.address
            # DNS happens in the trusted proxy, not in the worker. Only pinned
            # answers pass; the selected literal IP is used by the actual socket.
            answers = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
                host, port, type=socket.SOCK_STREAM), timeout=3)
            address = self.policy.pin(host, port, [v[4][0] for v in answers])
            self.refresh()
            data.server.address = (address, port)
            data.server.sni = host
        except Exception:
            data.server.error = "security-lab: upstream denied"

    def response(self, flow):
        self.active.discard(flow.id)

    def error(self, flow):
        self.active.discard(flow.id)
