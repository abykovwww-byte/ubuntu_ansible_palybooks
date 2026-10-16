"""Use the owner's Windows proxy settings and keep network errors secret-free."""

from __future__ import annotations

import asyncio
import sys
import urllib.request
from urllib.parse import unquote, urlsplit

from .config import ConfigError

CONNECT_TIMEOUT = 30


class TelegramNetworkError(RuntimeError):
    """Only an operation name and numeric error codes may reach the terminal."""


def system_proxy() -> dict | None:
    if sys.platform != "win32":
        # A server collector never falls back to a direct MTProto connection.
        return parse_proxy("socks5://vpn-egress:1080")
    # Read Windows Internet Settings rather than sandbox/environment HTTP_PROXY.
    endpoints = urllib.request.getproxies_registry()
    endpoint = endpoints.get("https") or endpoints.get("http")
    return parse_proxy(endpoint) if endpoint else None


def parse_proxy(endpoint: str) -> dict:
    try:
        parsed = urlsplit(endpoint if "://" in endpoint else "http://" + endpoint)
        kind = {"http": "http", "socks4": "socks4", "socks5": "socks5"}.get(parsed.scheme)
        port = parsed.port or {"http": 80, "socks4": 1080, "socks5": 1080}.get(parsed.scheme)
        if not kind or not parsed.hostname or not port or parsed.path not in ("", "/"):
            raise ValueError
        if parsed.query or parsed.fragment:
            raise ValueError
    except ValueError:
        raise ConfigError("Unsupported Windows proxy configuration; no values logged") from None
    return {
        "proxy_type": kind,
        "addr": parsed.hostname,
        "port": port,
        "username": unquote(parsed.username) if parsed.username else None,
        "password": unquote(parsed.password) if parsed.password else None,
        "rdns": True,
    }


def network_failure(operation: str, error: OSError) -> TelegramNetworkError:
    codes = []
    for name in ("errno", "winerror"):
        value = getattr(error, name, None)
        if type(value) is int:
            codes.append(f"{name}={value}")
    suffix = "; " + ", ".join(codes) if codes else ""
    return TelegramNetworkError(
        f"Telegram network failure during {operation}{suffix}; "
        "check the Windows proxy/VPN connection; no raw values logged"
    )


async def connect_client(client) -> None:
    try:
        await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
    except TimeoutError:
        raise TelegramNetworkError(
            f"Telegram connection timed out after {CONNECT_TIMEOUT} seconds; "
            "check the Windows proxy/VPN connection"
        ) from None
    except OSError as error:
        raise network_failure("connect", error) from None
