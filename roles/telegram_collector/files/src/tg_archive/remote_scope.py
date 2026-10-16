"""Dedicated forced-command SSH protocol; no collector or admin credentials."""

import json
from pathlib import Path

import asyncssh


async def request_scope(settings, payload):
    encoded = json.dumps(payload)
    if len(encoded.encode()) > 65536:
        raise ValueError("Scope request too large")
    async with asyncssh.connect(
        settings.host,
        port=settings.port,
        username="tgarchive-scope",
        client_keys=[settings.key],
        known_hosts=str(Path.home() / ".ssh/known_hosts"),
        agent_path=None,
        connect_timeout=15,
        login_timeout=15,
    ) as connection:
        result = await connection.run("scope", input=encoded, timeout=30)
    if result.exit_status != 0 or len(result.stdout.encode()) > 8 * 1024**2:
        raise ValueError("Server scope operation failed")
    response = json.loads(result.stdout)
    if not isinstance(response, dict) or response.get("authority") != "server":
        raise ValueError("Invalid server scope receipt")
    return response
