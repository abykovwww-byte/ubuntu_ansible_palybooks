"""STDIO integration prototype; no HTTP listener, no external targets, no shell tool."""

import argparse
from pathlib import Path

from security_lab.mcp_server import build_server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--principal", required=True, help="Host-provisioned prototype operator identity")
    args = parser.parse_args()
    build_server(args.data_dir, args.principal).run(transport="stdio")


if __name__ == "__main__":
    main()
