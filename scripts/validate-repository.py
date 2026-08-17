#!/usr/bin/env python3
"""Validate repository-level Codex, Wiki, JSON, and plugin contracts."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IGNORED_PARTS = {".git", ".venv", "graphify-out", "node_modules", "__pycache__"}
LINK_RE = re.compile(r"(?<!!)\[[^]]*]\(([^)]+)\)")
SSH_COMMAND_RE = re.compile(
    r"(?i)(?:^|[\\/\s])ssh(?:\.exe)?\s+(?:-[A-Za-z]|[A-Za-z0-9._-]+@[A-Za-z0-9])"
)


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
        ROOT / "docs" / "repository-work-standard.md",
        ROOT / "scripts" / "sync-codex-skills.ps1",
    ]
    for path in required:
        if not path.is_file():
            fail(errors, f"missing project policy file: {path.relative_to(ROOT)}")

    merge_policy_files = [
        ROOT / "AGENTS.md",
        ROOT / "codex-skills" / "abykovserv-iac-deploy" / "SKILL.md",
        ROOT / "codex-skills" / "abykovserv-iac-deploy" / "references" / "deployment-map.md",
        ROOT / "codex-skills" / "rp-world-pack-builder" / "SKILL.md",
        ROOT / "codex-skills" / "rp-world-pack-builder" / "references" / "world-pack-contract.md",
        ROOT / "codex-skills" / "training-world-pack-builder" / "SKILL.md",
        ROOT / "docs" / "repository-work-standard.md",
        ROOT / "docs" / "server-setup-notes.md",
        ROOT / "docs" / "use-framework.md",
        ROOT / "docs" / "wiki" / "09-operations-and-repository.md",
        ROOT / "docs" / "wiki" / "README.md",
        ROOT / "plugins" / "rp-stack-devkit" / "skills" / "rp-stack-devkit" / "SKILL.md",
        ROOT / "roles" / "apps" / "files" / "rp-stack" / "README.md",
        ROOT / "roles" / "apps" / "files" / "rp-stack" / "docs" / "decisions" / "018-separate-training-and-rp-gateways.md",
        ROOT / "roles" / "apps" / "files" / "rp-stack" / "docs" / "decisions" / "019-retire-legacy-awareness-gateway-path.md",
        ROOT / "roles" / "apps" / "files" / "rp-stack" / "docs" / "decisions" / "020-rp-relationship-pressure-layer.md",
        ROOT / "roles" / "apps" / "files" / "rp-stack" / "docs" / "plans" / "011-local-gemma4-vulkan.md",
        ROOT / "roles" / "apps" / "files" / "rp-stack" / "docs" / "plans" / "014-interactive-training-site-artifacts.md",
        ROOT / "roles" / "apps" / "files" / "rp-stack" / "docs" / "plans" / "015-training-scenario-interaction-capabilities.md",
    ]
    merge_policy_markers = {
        "non-draft pull request": re.compile(r"(?i)non-draft (?:pull request|PR)"),
        "green CI": re.compile(r"(?i)(?:green|зел[её]н\w*)[- ]CI|CI is green"),
        "merge": re.compile(r"(?i)(?:merge|мерж)"),
        "main": re.compile(r"(?i)\bmain\b"),
    }
    for path in merge_policy_files:
        if not path.is_file():
            fail(errors, f"missing merge-policy file: {path.relative_to(ROOT)}")
            continue
        text = path.read_text(encoding="utf-8")
        missing = [name for name, pattern in merge_policy_markers.items() if not pattern.search(text)]
        if missing:
            fail(
                errors,
                f"merge policy missing from {path.relative_to(ROOT)}: {', '.join(missing)}",
            )

    direct_main_push = re.compile(
        r"(?i)(?:git push origin main|push(?: changes)? (?:to|in|в) "
        r"`?(?:GitHub )?(?:origin/)?main`?|push origin/main|commit \+ push.{0,80}GitHub main)"
    )
    prohibition = re.compile(r"(?i)(?:do not|must not|never|prohibit|запрещ)")
    stale_pushed_stop = re.compile(
        r"(?i)(?:stop(?:s|ped)? (?:after|at) .{0,20}pushed|"
        r"automation stops at .{0,20}pushed|останавливается.{0,40}pushed)"
    )
    result = subprocess.run(
        ["git", "ls-files", "*.md"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        fail(errors, "cannot enumerate Markdown files for merge-policy validation")
        return
    for relative_text in result.stdout.splitlines():
        path = ROOT / relative_text
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if direct_main_push.search(line) and not prohibition.search(line):
                fail(errors, f"direct push to main instruction: {relative_text}:{line_number}")
            if stale_pushed_stop.search(line):
                fail(errors, f"sudo boundary stops at pushed instead of merged: {relative_text}:{line_number}")


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


def tracked_files(errors: list[str]) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        fail(errors, "cannot enumerate tracked files with git ls-files")
        return []
    return [ROOT / item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def markdown_command_fragments(text: str) -> list[str]:
    fragments: list[str] = []
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            fragments.append(line)
        fragments.extend(re.findall(r"`([^`]+)`", line))
    return fragments


def validate_environment_contracts(errors: list[str]) -> None:
    inventory = ROOT / "inventories" / "local" / "group_vars" / "server.yml"
    production_env_template = ROOT / "roles" / "apps" / "templates" / "rp-stack.env.j2"
    env_templates = (
        production_env_template,
        ROOT / "roles" / "apps" / "templates" / "rp-stack.env.example.j2",
    )
    retention_variable = "rp_stack_gateway_service_call_log_retention_days: 0"
    retention_mapping = (
        "SERVICE_CALL_LOG_RETENTION_DAYS="
        "{{ rp_stack_gateway_service_call_log_retention_days | default(0) }}"
    )
    if (
        not inventory.is_file()
        or retention_variable not in inventory.read_text(encoding="utf-8")
    ):
        fail(errors, "RP Stack inventory must default service-call log retention to unlimited (0)")
    for path in env_templates:
        if not path.is_file() or retention_mapping not in path.read_text(encoding="utf-8"):
            fail(
                errors,
                f"RP Stack env template does not render service-call log retention: {path.relative_to(ROOT)}",
            )

    observed_revision_variable = "rp_stack_gateway_rp_contract_observed_revision: 6"
    observed_revision_mapping = (
        "RP_CONTRACT_OBSERVED_REVISION="
        "{{ rp_stack_gateway_rp_contract_observed_revision }}"
    )
    if (
        not inventory.is_file()
        or observed_revision_variable not in inventory.read_text(encoding="utf-8")
    ):
        fail(errors, "RP Stack inventory must keep rp-core.v2 revision 6 observed")
    if (
        not production_env_template.is_file()
        or observed_revision_mapping not in production_env_template.read_text(encoding="utf-8")
    ):
        fail(errors, "RP Stack production env must render the explicit observed RP revision")

    marketplace = Path(".agents/plugins/marketplace.json")
    old_profile = b"C:" + b"\\Users\\" + b"albykov"
    old_plugin_path = b".agents/plugins/" + b"rp-stack-devkit/"
    tracked = tracked_files(errors)
    for path in tracked:
        if not path.is_file():
            continue
        data = path.read_bytes()
        relative = path.relative_to(ROOT)
        if old_profile in data:
            fail(errors, f"tracked file contains obsolete profile path: {relative}")
        if relative != marketplace and old_plugin_path in data:
            fail(errors, f"tracked file contains obsolete devkit path: {relative}")

    project_policy = ROOT / ".codex" / "hooks" / "rp_stack_policy.ps1"
    plugin_policy = ROOT / "plugins" / "rp-stack-devkit" / "hooks" / "rp_stack_policy.ps1"
    if project_policy.is_file() and plugin_policy.is_file() and project_policy.read_bytes() != plugin_policy.read_bytes():
        fail(errors, "project and plugin rp_stack_policy.ps1 copies differ")

    for base in (ROOT / "codex-skills", ROOT / "plugins"):
        for path in base.rglob("*.md"):
            text = path.read_text(encoding="utf-8")
            for fragment in markdown_command_fragments(text):
                if SSH_COMMAND_RE.search(fragment) and "-i" not in fragment and "keyless-example" not in fragment:
                    fail(errors, f"SSH command lacks explicit -i: {path.relative_to(ROOT)}: {fragment.strip()}")

    ignore_path = ROOT / ".graphifyignore"
    required_ignores = {".tools/", "tmp/", "codex-worktrees/", "graphify-out/"}
    if not ignore_path.is_file():
        fail(errors, "missing .graphifyignore")
    else:
        actual_ignores = {
            line.strip().replace("\\", "/")
            for line in ignore_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        for required in sorted(required_ignores - actual_ignores):
            fail(errors, f".graphifyignore missing required entry: {required}")


def validate_adr_registry(errors: list[str]) -> None:
    validator = ROOT / "scripts" / "validate-adr-registry.py"
    if not validator.is_file():
        fail(errors, "missing scripts/validate-adr-registry.py")
        return
    result = subprocess.run(
        [sys.executable, str(validator), "--root", str(ROOT)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stdout + result.stderr).strip()
        fail(errors, f"ADR registry validation failed: {detail}")


def main() -> int:
    errors: list[str] = []
    validate_json(errors)
    validate_wiki(errors)
    validate_agents(errors)
    validate_plugin(errors)
    validate_environment_contracts(errors)
    validate_adr_registry(errors)
    if errors:
        print("Repository validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Repository contracts valid: JSON, Wiki, AGENTS, plugin, environment, SSH, policy, and Graphify guards.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
