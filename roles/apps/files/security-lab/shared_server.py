"""Host entry point for the single shared Security Lab service."""

import argparse
import logging
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings", type=Path, required=True)
    args = parser.parse_args()
    # pythonw has no console streams. Own a private log independent of a bridge
    # process or its STDIO handles before importing/initializing the MCP SDK.
    log = args.settings.with_suffix(".service.log").open("a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = log
    import uvicorn
    from security_lab.compose_runtime import ComposePentestRuntime, ComposeRuntimeConfig
    from security_lab.shared_service import build_shared_app, load_credentials, load_settings
    settings = load_settings(args.settings)
    runtime = None
    if settings.get("pentestgpt_host"):
        runtime = ComposePentestRuntime(ComposeRuntimeConfig(mode="ssh",
            ssh_host=settings["pentestgpt_host"], ssh_identity=settings["pentestgpt_identity"],
            compose_directory=settings.get("pentestgpt_directory", "/srv/apps/pentestgpt"),
            runs_directory=settings.get("pentestgpt_runs_directory", "/srv/app-data/pentestgpt/runs"),
            workspace_directory=settings.get("pentestgpt_workspace_directory", "/srv/app-data/pentestgpt/workspace")))
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
