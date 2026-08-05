#!/usr/bin/env python3
"""Validate repository-level Codex, Wiki, JSON, and plugin contracts."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IGNORED_PARTS = {".git", ".venv", "graphify-out", "node_modules", "__pycache__"}
LINK_RE = re.compile(r"(?<!!)\[[^]]*]\(([^)]+)\)")


def fail(errors: list[str], message: str) -> None:
    errors.append(message)


def validate_json(errors: list[str]) -> None:
    for path in ROOT.rglob("*.json"):
        if any(part in IGNORED_PARTS for part in path.parts):
            continue
        try:
            json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - aggregate every invalid file
            fail(errors, f"invalid JSON: {path.relative_to(ROOT)}: {exc}")


def validate_wiki(errors: list[str]) -> None:
    wiki_root = ROOT / "docs" / "wiki"
    readme = wiki_root / "README.md"
    if not readme.is_file():
        fail(errors, "missing docs/wiki/README.md")
        return

    for path in wiki_root.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        fence_count = sum(1 for line in text.splitlines() if line.lstrip().startswith("```"))
        if fence_count % 2:
            fail(errors, f"unbalanced Markdown fence: {path.relative_to(ROOT)}")

        for match in LINK_RE.finditer(text):
            raw_target = match.group(1).strip().split(maxsplit=1)[0].strip("<>")
            if not raw_target or raw_target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            target_path = raw_target.split("#", 1)[0]
            if not target_path:
                continue
            resolved = (path.parent / target_path).resolve()
            try:
                resolved.relative_to(ROOT.resolve())
            except ValueError:
                fail(errors, f"Wiki link escapes repository: {path.relative_to(ROOT)} -> {raw_target}")
                continue
            if not resolved.exists():
                fail(errors, f"broken Wiki link: {path.relative_to(ROOT)} -> {raw_target}")


def validate_agents(errors: list[str]) -> None:
    required = [
        ROOT / "AGENTS.md",
        ROOT / "roles" / "apps" / "files" / "rp-stack" / "AGENTS.md",
        ROOT / "roles" / "apps" / "files" / "rp-stack" / "rp-gateway" / "AGENTS.md",
        ROOT / "roles" / "apps" / "files" / "rp-stack" / "worldpacks" / "AGENTS.md",
        ROOT / ".codex" / "config.toml",
        ROOT / ".codex" / "hooks.json",
    ]
    for path in required:
        if not path.is_file():
            fail(errors, f"missing project policy file: {path.relative_to(ROOT)}")


def validate_plugin(errors: list[str]) -> None:
    plugin_root = ROOT / "plugins" / "rp-stack-devkit"
    manifest_path = plugin_root / ".codex-plugin" / "plugin.json"
    marketplace_path = ROOT / ".agents" / "plugins" / "marketplace.json"
    required_files = [
        manifest_path,
        plugin_root / ".mcp.json",
        plugin_root / "skills" / "rp-stack-devkit" / "SKILL.md",
        plugin_root / "scripts" / "mcp-server.ps1",
        plugin_root / "scripts" / "rp-stack-ops.ps1",
        plugin_root / "hooks" / "hooks.json",
        marketplace_path,
    ]
    for path in required_files:
        if not path.is_file():
            fail(errors, f"missing plugin file: {path.relative_to(ROOT)}")
    if errors and not manifest_path.is_file():
        return

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for field in ("name", "version", "description", "author", "interface"):
        if field not in manifest:
            fail(errors, f"plugin manifest missing field: {field}")
    if manifest.get("name") != "rp-stack-devkit":
        fail(errors, "plugin manifest name must be rp-stack-devkit")
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?", str(manifest.get("version", ""))):
        fail(errors, "plugin version must be strict semver")
    if manifest.get("mcpServers") != "./.mcp.json":
        fail(errors, "plugin must declare ./.mcp.json")

    if marketplace_path.is_file():
        marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
        entry = next(
            (item for item in marketplace.get("plugins", []) if item.get("name") == "rp-stack-devkit"),
            None,
        )
        if entry is None:
            fail(errors, "marketplace does not list rp-stack-devkit")
        else:
            policy = entry.get("policy", {})
            if not policy.get("installation") or not policy.get("authentication"):
                fail(errors, "marketplace plugin policy is incomplete")
            if entry.get("source", {}).get("path") != "./plugins/rp-stack-devkit":
                fail(errors, "marketplace plugin source path is not canonical")


def main() -> int:
    errors: list[str] = []
    validate_json(errors)
    validate_wiki(errors)
    validate_agents(errors)
    validate_plugin(errors)
    if errors:
        print("Repository validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Repository contracts valid: JSON, Wiki links/fences, AGENTS, hooks, and plugin manifests.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
