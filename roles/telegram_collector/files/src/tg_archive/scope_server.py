"""One bounded scope request; no DB, Telegram session, API or general file access."""

import json
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

from .reader_config import selection, snapshot
from .scope import private_ids
from .sync_add import add


def dispatch(root, request):
    if not isinstance(request, dict) or set(request) - {"op", "chat", "project", "since", "slug"}:
        raise ValueError("Invalid scope request")
    config = tomllib.loads((root / "tg-archive.toml").read_text(encoding="utf-8-sig"))
    if request.get("op") == "snapshot" and set(request) == {"op"}:
        rows, status = snapshot(root)
        return {
            "authority": "server",
            "selection": selection(root),
            "dialogs": rows,
            "snapshot_status": status,
            "approved_private_ids": private_ids(config, root),
        }
    if request.get("op") != "add" or not isinstance(request.get("chat"), str):
        raise ValueError("Unsupported scope request")
    settings = SimpleNamespace(root=root, default_since=config["default_since"])
    result = add(
        settings, request["chat"], request.get("project"), request.get("since"), request.get("slug")
    )
    if result["status"] in {"added", "existing"}:
        ident = result["entry"]["id"]
        if not any(entry["id"] == ident for entry in selection(root)["chats"]):
            raise ValueError("Server scope read-back failed")
        result["read_back"] = True
    return dict(result, authority="server")


def main():
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError("Scope request too large")
        response = dispatch(Path("/data"), json.loads(raw))
        encoded = json.dumps(response, default=str, ensure_ascii=False)
        if len(encoded.encode("utf-8")) > 8 * 1024**2:
            raise ValueError("Scope response too large")
        print(encoded)
        return 0
    except Exception as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
