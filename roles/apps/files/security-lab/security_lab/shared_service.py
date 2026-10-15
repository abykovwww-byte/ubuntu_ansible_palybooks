"""Process-owned loopback MCP; authenticated clients share one Controller."""

from contextlib import asynccontextmanager
import hmac
import json
from pathlib import Path

from starlette.applications import Starlette

from .client_identity import delegate_client
from .mcp_server import ServerState, build_server


def load_settings(path: Path) -> dict:
    settings = json.loads(path.read_text(encoding="utf-8"))
    if (type(settings.get("port")) is not int or not 1024 <= settings["port"] <= 65535
            or not settings.get("service_id") or not settings.get("principal")):
        raise ValueError("Invalid shared service configuration")
    return settings


def load_credentials(settings: dict) -> dict[str, str]:
    credentials = json.loads(Path(settings["credentials_file"]).read_text(encoding="utf-8"))
    if (set(credentials) != {"codex", "opencode"}
            or any(not isinstance(token, str) or len(token) < 40 for token in credentials.values())
            or len(set(credentials.values())) != 2):
        raise ValueError("Distinct host-provisioned client credentials required")
    return credentials


class AuthenticatedApp:
    def __init__(self, inner, *, settings, credentials, state, shutdown=None):
        self.inner, self.state = inner, state
        self.settings, self.credentials = settings, credentials
        self.authority = f"127.0.0.1:{settings['port']}".encode("ascii")
        self.shutdown = shutdown

    @staticmethod
    async def reply(send, status, value):
        body = json.dumps(value).encode("utf-8")
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", b"application/json"), (b"cache-control", b"no-store"),
            (b"content-length", str(len(body)).encode("ascii"))]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.inner(scope, receive, send)
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        headers = scope.get("headers", [])
        hosts = [v for k, v in headers if k.lower() == b"host"]
        auth = [v for k, v in headers if k.lower() == b"authorization"]
        client = None
        if (hosts == [self.authority] and len(auth) == 1 and scope.get("scheme") == "http"
                and not any(k.lower() == b"origin" for k, _ in headers)):
            for name, token in self.credentials.items():
                if hmac.compare_digest(auth[0], ("Bearer " + token).encode("ascii")):
                    client = name
        if client is None:
            return await self.reply(send, 401, {"error": "Unauthorized"})
        path = scope.get("path")
        if (scope.get("query_string") or scope.get("raw_path") != path.encode("ascii", errors="replace")
                or path not in {"/mcp", "/health", "/shutdown"}):
            return await self.reply(send, 404, {"error": "Not found"})
        if path == "/health" and scope["method"] == "GET":
            return await self.reply(send, 200, {"service": "security-lab", "shared_controller": True,
                "service_id": self.settings["service_id"], "client": client, "ready": True})
        if path == "/shutdown" and scope["method"] == "POST" and self.shutdown is not None:
            # Host lifecycle endpoint, not an MCP tool. Refuse shutdown while
            # work is active; client disconnects never invoke this endpoint.
            c = self.state.slot.get()
            with c.lock:
                busy = c.db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('prepared','running')").fetchone()[0]
                busy += c.db.execute("SELECT COUNT(*) FROM discovery_runs WHERE status IN ('pending','running')").fetchone()[0]
            if busy:
                return await self.reply(send, 409, {"error": "Active work; stop cases explicitly first"})
            self.shutdown()
            return await self.reply(send, 200, {"stopping": True})
        if path != "/mcp" or scope["method"] != "POST":
            return await self.reply(send, 405, {"error": "Method not allowed"})
        marker = delegate_client.set(client)
        try:
            await self.inner(scope, receive, send)
        finally:
            delegate_client.reset(marker)


def build_shared_app(settings: dict, credentials: dict[str, str], *, pentest_runtime=None,
                     pentest_resolver=None, shutdown=None):
    directory = Path(settings["data_dir"])
    state = ServerState(directory, settings["principal"])
    server = build_server(directory, settings["principal"], shared_state=state,
                          pentest_runtime=pentest_runtime, pentest_resolver=pentest_resolver)
    server.settings.stateless_http = True
    server.settings.json_response = True
    inner = server.streamable_http_app()

    @asynccontextmanager
    async def lifespan(app):
        # Acquire once, before readiness. A legacy STDIO owner causes a clear
        # startup failure, never a second writer or a new replacement store.
        marker = delegate_client.set("service")
        try:
            state.slot.get()
        finally:
            delegate_client.reset(marker)
        try:
            async with inner.router.lifespan_context(inner):
                yield
        finally:
            marker = delegate_client.set("service")
            try:
                await state.shutdown()
            finally:
                delegate_client.reset(marker)

    outer = Starlette(lifespan=lifespan)
    outer.mount("/", inner)
    return AuthenticatedApp(outer, settings=settings, credentials=credentials,
                            state=state, shutdown=shutdown)
