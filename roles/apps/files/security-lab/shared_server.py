"""Host entry point for the single shared Security Lab service.

The same service runs either on the operator workstation or on abykovserv. On
the server it uses the local Compose runtime, so the campaign loop, hypothesis
ledger and native jobs all keep running with the Windows workstation off.
"""
import argparse
import json
import logging
from pathlib import Path
import sys


def open_service_log(settings_path: Path, settings: dict):
    """Open a private log if writable, else fall back to the systemd journal.

    The settings directory may be root-owned and read-only for the service user,
    so the log must not be a hard requirement next to it.
    """
    candidates = [Path(settings["log_file"])] if settings.get("log_file") else []
    candidates.append(settings_path.with_suffix(".service.log"))
    for candidate in candidates:
        try:
            return candidate.open("a", encoding="utf-8", buffering=1)
        except OSError:
            continue
    return None


def runtime_for_settings(settings: dict):
    """Select the Compose runtime from settings; never starts anything at import.

    mode=local  -> the service itself runs on the PentestGPT host (server).
    mode=ssh    -> the service reaches the host over SSH (workstation).
    """
    from security_lab.compose_runtime import ComposePentestRuntime, ComposeRuntimeConfig
    mode = settings.get("pentestgpt_mode", "ssh")
    if mode == "local":
        return ComposePentestRuntime(ComposeRuntimeConfig(mode="local",
            compose_directory=settings.get("pentestgpt_directory", "/srv/apps/pentestgpt"),
            runs_directory=settings.get("pentestgpt_runs_directory", "/srv/app-data/pentestgpt/runs"),
            workspace_directory=settings.get("pentestgpt_workspace_directory", "/srv/app-data/pentestgpt/workspace"),
            tool_service=settings.get("pentestgpt_tool_service", "pentest-tools")))
    if mode != "ssh":
        raise ValueError("Unsupported pentestgpt_mode; use local or ssh")
    if not settings.get("pentestgpt_host"):
        return None
    return ComposePentestRuntime(ComposeRuntimeConfig(mode="ssh",
        ssh_host=settings["pentestgpt_host"], ssh_identity=settings["pentestgpt_identity"],
        compose_directory=settings.get("pentestgpt_directory", "/srv/apps/pentestgpt"),
        runs_directory=settings.get("pentestgpt_runs_directory", "/srv/app-data/pentestgpt/runs"),
        workspace_directory=settings.get("pentestgpt_workspace_directory", "/srv/app-data/pentestgpt/workspace")))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings", type=Path, required=True)
    args = parser.parse_args()
    # pythonw has no console streams. Own a private log independent of a bridge
    # process or its STDIO handles before importing/initializing the MCP SDK.
    # If no writable log is available (e.g. a read-only settings dir), keep the
    # processes' streams so systemd/journald still captures output.
    log = open_service_log(args.settings, json.loads(args.settings.read_text(encoding="utf-8")))
    if log is not None:
        sys.stdout = sys.stderr = log
    import uvicorn
    from security_lab.shared_service import build_shared_app, load_credentials, load_settings
    settings = load_settings(args.settings)
    runtime = runtime_for_settings(settings)
    server = None
    def shutdown():
        server.should_exit = True
    app = build_shared_app(settings, load_credentials(settings), pentest_runtime=runtime, shutdown=shutdown)
    logging.basicConfig(level=logging.WARNING, stream=log, force=True)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=settings["port"],
        workers=1, access_log=False, log_level="warning", proxy_headers=False, timeout_graceful_shutdown=30))
    server.run()


if __name__ == "__main__":
    main()
