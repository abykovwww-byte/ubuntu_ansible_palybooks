"""Switch the existing Windows MCP to server scope only after a valid remote receipt."""

import argparse
import asyncio
import json
import re
import sys
from dataclasses import replace
from pathlib import Path


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    root = args.source.resolve()
    sys.path.insert(0, str(root / "src"))
    from tg_archive.reader_config import load_reader
    from tg_archive.remote_scope import request_scope

    path = root / "tg-archive.toml"
    settings = load_reader(path, root / "mcp.env")
    receipt = await request_scope(replace(settings, scope_backend="server"), {"op": "snapshot"})
    if receipt.get("snapshot_status") != "ok":
        raise ValueError("A successful server collection snapshot is required")
    text = path.read_text(encoding="utf-8-sig")
    if "[scope]" in text:
        pattern = r'(?m)^(backend\s*=\s*)"(?:local|server)"\s*$'
        text, count = re.subn(pattern, 'backend = "server"', text)
        if count != 1:
            raise ValueError("Unexpected existing scope configuration")
    else:
        text = text.rstrip() + '\n\n[scope]\nbackend = "server"\n'
    path.write_text(text, encoding="utf-8")
    print(json.dumps({"scope_authority": "server", "selected_chats": len(receipt["selection"]["chats"]),
                      "restart_mcp_required": True}))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        print("MCP scope switch failed; no private values logged", file=sys.stderr)
        raise SystemExit(1) from None
