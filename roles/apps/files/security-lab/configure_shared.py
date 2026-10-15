"""Install only Security Lab client registrations and its canonical skills.

No credentials are printed, copied into agent configuration, or kept in Git.
Existing store/principal/SSH options come from the registered legacy server.
The shared service starts on demand. Closing idle legacy processes requires
the explicit one-time --retire-idle-legacy option.
"""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tomllib
import uuid


ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[3]
NAME = "security-lab-probe"


def private_directory(path):
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        account = os.environ.get("USERDOMAIN", ".") + "\\" + os.environ["USERNAME"]
        subprocess.run(["icacls.exe", str(path), "/inheritance:r", "/grant:r",
            account + ":(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"], check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    else:
        path.chmod(0o700)


def update_codex(text, executable, settings):
    before = tomllib.loads(text)
    old = before.get("mcp_servers", {}).get(NAME)
    if old is None:
        raise ValueError("The existing Security Lab registration is required")
    pattern = r"(?ms)^\[mcp_servers\.security-lab-probe\][ \t]*\r?\n.*?(?=^\[|\Z)"
    block = (f"[mcp_servers.{NAME}]\ncommand = {json.dumps(str(executable))}\n"
        f"args = {json.dumps([str(ROOT / 'client_proxy.py'), '--settings', str(settings), '--client', 'codex'])}\n"
        "startup_timeout_sec = 30\ntool_timeout_sec = 120\n\n")
    revised, count = re.subn(pattern, lambda _: block, text)
    if count != 1:
        raise ValueError("Unsupported Security Lab TOML layout")
    after = tomllib.loads(revised)
    after["mcp_servers"].pop(NAME)
    before["mcp_servers"].pop(NAME)
    if after != before:
        raise ValueError("Unexpected Codex settings change")
    return revised


def update_opencode(text, executable, settings):
    before = json.loads(text)
    after = json.loads(text)
    after.setdefault("mcp", {})[NAME] = {"type": "local", "command": [str(executable),
        str(ROOT / "client_proxy.py"), "--settings", str(settings), "--client", "opencode"],
        "enabled": True, "timeout": 120000}
    paths = [str(REPO / "codex-skills" / name) for name in ("security-lab-operator", "abykovserv-iac-deploy")]
    skills = after.setdefault("skills", {"paths": []})
    if isinstance(skills, list):
        for path in paths:
            if path not in skills:
                skills.append(path)
    else:
        for path in paths:
            if path not in skills.setdefault("paths", []):
                skills["paths"].append(path)
    # These are the only two managed surfaces. Preserve unrelated MCP servers,
    # providers, model selection, permissions, and already installed skills.
    expected = json.loads(json.dumps(after))
    expected.get("mcp", {}).pop(NAME, None)
    original = json.loads(json.dumps(before))
    original.get("mcp", {}).pop(NAME, None)
    expected.pop("skills", None)
    original.pop("skills", None)
    if expected != original:
        raise ValueError("Unexpected OpenCode settings change")
    return json.dumps(after, ensure_ascii=False, indent=2) + "\n"


def legacy_settings(registration):
    arguments = registration.get("args", [])
    def option(name, default=None):
        return arguments[arguments.index(name) + 1] if name in arguments else default
    directory, principal = option("--data-dir"), option("--principal")
    if not directory or not principal:
        raise ValueError("Cannot derive existing store and principal; refusing a replacement store")
    settings = {"data_dir": directory, "principal": principal, "port": 39118,
                "service_id": uuid.uuid4().hex}
    for flag in ("pentestgpt-host", "pentestgpt-identity", "pentestgpt-directory",
                 "pentestgpt-runs-directory", "pentestgpt-workspace-directory"):
        value = option("--" + flag)
        if value:
            settings[flag.replace("-", "_")] = value
    return settings


def store_summary(directory):
    database = Path(directory) / "control.db"
    if not database.is_file():
        raise ValueError("Existing case database is missing")
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        history = {table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                   for table in ("cases", "manifests", "audit", "evidence", "source_receipts", "surface", "surface_links", "jobs")}
        return {"cases": db.execute("SELECT COUNT(*) FROM cases").fetchone()[0],
                "history_sha256": hashlib.sha256(json.dumps(history, sort_keys=True).encode("utf-8")).hexdigest(),
                "active_jobs": db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('prepared','running')").fetchone()[0],
                "active_discovery": db.execute("SELECT COUNT(*) FROM discovery_runs WHERE status IN ('pending','running')").fetchone()[0]}


def retire_idle_legacy(settings):
    if os.name != "nt":
        raise ValueError("Refresh the legacy STDIO client manually on this OS")
    summary = store_summary(settings["data_dir"])
    if summary["active_jobs"] or summary["active_discovery"]:
        raise ValueError("Work became active; refusing to close legacy processes")
    script_path = str(ROOT / "server.py").replace("'", "''")
    data_path = str(Path(settings["data_dir"])).replace("'", "''")
    script = ("$ErrorActionPreference='Stop'; $matches=@(Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -and "
        "$_.CommandLine.Replace('/','\\').Contains('" + script_path + "') -and "
        "$_.CommandLine.Replace('/','\\').Contains('" + data_path + "') }); "
        "$matches | ForEach-Object { Stop-Process -Id $_.ProcessId -ErrorAction SilentlyContinue }; "
        "$matches.Count")
    powershell = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand",
        base64.b64encode(script.encode("utf-16-le")).decode("ascii")], capture_output=True, check=True, timeout=20)
    return int(result.stdout.strip())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--retire-idle-legacy", action="store_true",
        help="One-time Windows cutover: close only idle legacy processes for this exact script/store")
    parser.add_argument("--codex-config", type=Path, default=Path.home() / ".codex/config.toml")
    parser.add_argument("--opencode-config", type=Path, default=Path.home() / ".config/opencode/opencode.jsonc")
    parser.add_argument("--settings", type=Path, default=Path.home() / ".local/share/security-lab/service.json")
    args = parser.parse_args()
    if args.retire_idle_legacy and not args.apply:
        parser.error("--retire-idle-legacy requires --apply")
    codex_text = args.codex_config.read_text(encoding="utf-8")
    opencode_text = args.opencode_config.read_text(encoding="utf-8")
    registration = tomllib.loads(codex_text)["mcp_servers"][NAME]
    settings = json.loads(args.settings.read_text(encoding="utf-8")) if args.settings.exists() else legacy_settings(registration)
    summary = store_summary(settings["data_dir"])
    if summary["active_jobs"] or summary["active_discovery"]:
        raise ValueError("Existing work is active; finish or stop it explicitly before switching")
    codex = update_codex(codex_text, sys.executable, args.settings)
    opencode = update_opencode(opencode_text, sys.executable, args.settings)
    retired = 0
    if args.apply:
        private_directory(args.settings.parent)
        credentials = args.settings.parent / "credentials.json"
        settings["credentials_file"] = str(credentials)
        if not credentials.exists():
            with credentials.open("x", encoding="utf-8") as file:
                json.dump({name: secrets.token_urlsafe(48) for name in ("codex", "opencode")}, file)
            if os.name != "nt":
                credentials.chmod(0o600)
        args.settings.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        baseline = args.settings.parent / "migration-baseline.json"
        if not baseline.exists():
            baseline.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        # Retain only the nonsecret legacy launch registration for rollback,
        # never a wholesale copy of a secret-bearing global configuration.
        rollback = args.settings.parent / "legacy-registration.json"
        if not rollback.exists() and "--data-dir" in registration.get("args", []):
            allowed = {k: registration[k] for k in ("command", "args", "startup_timeout_sec", "tool_timeout_sec") if k in registration}
            rollback.write_text(json.dumps(allowed, indent=2) + "\n", encoding="utf-8")
        args.codex_config.write_text(codex, encoding="utf-8")
        try:
            args.opencode_config.write_text(opencode, encoding="utf-8")
        except BaseException:
            args.codex_config.write_text(codex_text, encoding="utf-8")
            raise
        for name in ("security-lab-operator", "abykovserv-iac-deploy"):
            source = REPO / "codex-skills" / name
            destination = Path.home() / ".codex/skills" / name
            shutil.copytree(source, destination, dirs_exist_ok=True)
        if args.retire_idle_legacy:
            retired = retire_idle_legacy(settings)
    print(json.dumps({"applied": args.apply, "store": summary, "clients": ["codex", "opencode"],
        "settings": str(args.settings), "shared_controller": True, "idle_legacy_processes_closed": retired,
        "credentials_printed": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
