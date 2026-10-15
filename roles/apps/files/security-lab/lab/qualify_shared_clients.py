"""Read-only live qualification using the actual installed client registrations."""

import asyncio
import argparse
import json
from pathlib import Path
import sys
import tomllib

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from configure_shared import store_summary
from security_lab.shared_service import load_settings


async def qualify(*, check_migration=False):
    home = Path.home()
    codex = tomllib.loads((home / ".codex/config.toml").read_text(encoding="utf-8"))["mcp_servers"]["security-lab-probe"]
    opencode = json.loads((home / ".config/opencode/opencode.jsonc").read_text(encoding="utf-8"))["mcp"]["security-lab-probe"]
    settings_path = home / ".local/share/security-lab/service.json"
    settings = load_settings(settings_path)
    before = store_summary(settings["data_dir"])

    async def inspect(name, command):
        params = StdioServerParameters(command=command[0], args=command[1:])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                status = await session.call_tool("integration_status", {})
                if status.isError:
                    raise RuntimeError("Status call failed")
                status = status.structuredContent
                assert status["client"] == name and status["shared_controller"] is True
                cases, offset = [], 0
                while True:
                    page = await session.call_tool("list_cases", {"offset": offset})
                    assert not page.isError
                    cases.extend(page.structuredContent["cases"])
                    offset = page.structuredContent["next_offset"]
                    if offset is None:
                        break
                for case in cases:
                    view = await session.call_tool("get_case", {"case_id": case["id"]})
                    assert not view.isError and view.structuredContent["id"] == case["id"]
                return {"client": name, "cases": cases,
                    "native_runtime_configured": status["existing_pentestgpt_adapter"]["configured"],
                    "tools": len((await session.list_tools()).tools)}

    results = await asyncio.gather(inspect("codex", [codex["command"], *codex["args"]]),
                                   inspect("opencode", opencode["command"]))
    assert results[0]["cases"] == results[1]["cases"]
    after = store_summary(settings["data_dir"])
    assert before == after
    if check_migration:
        baseline = json.loads(settings_path.with_name("migration-baseline.json").read_text(encoding="utf-8"))
        assert baseline == after
    for result in results:
        result["case_count"] = len(result.pop("cases"))
    return {"passed": True, "simultaneous_clients": results, "existing_history_unchanged": True,
            "migration_baseline_checked": check_migration,
            "target_requests": 0, "provider_calls": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-migration", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(qualify(check_migration=args.check_migration)), ensure_ascii=False, indent=2))
