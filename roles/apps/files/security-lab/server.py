"""STDIO MCP with passive discovery and opt-in native Compose tools; no shell tool."""

import argparse
from pathlib import Path

from security_lab.mcp_server import build_server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--principal", required=True, help="Host-provisioned prototype operator identity")
    parser.add_argument("--pentestgpt-host", help="Opt-in SSH destination for the existing managed PentestGPT runtime")
    parser.add_argument("--pentestgpt-identity", type=Path, help="Host-private SSH identity path; never read into model context")
    parser.add_argument("--pentestgpt-directory", default="/srv/apps/pentestgpt")
    parser.add_argument("--pentestgpt-runs-directory", default="/srv/app-data/pentestgpt/runs")
    parser.add_argument("--pentestgpt-workspace-directory", default="/srv/app-data/pentestgpt/workspace")
    args = parser.parse_args()
    runtime = None
    if args.pentestgpt_host:
        if args.pentestgpt_identity is None:
            parser.error("--pentestgpt-identity is required with --pentestgpt-host")
        from security_lab.compose_runtime import ComposeRuntimeConfig, ComposePentestRuntime
        runtime = ComposePentestRuntime(ComposeRuntimeConfig(mode='ssh', ssh_host=args.pentestgpt_host,
            ssh_identity=str(args.pentestgpt_identity), compose_directory=args.pentestgpt_directory,
            runs_directory=args.pentestgpt_runs_directory, workspace_directory=args.pentestgpt_workspace_directory))
    elif args.pentestgpt_identity:
        parser.error("--pentestgpt-host is required with --pentestgpt-identity")
    build_server(args.data_dir, args.principal, pentest_runtime=runtime).run(transport="stdio")


if __name__ == "__main__":
    main()
