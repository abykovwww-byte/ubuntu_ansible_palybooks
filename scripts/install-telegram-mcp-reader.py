"""Install only the reviewed MCP scope adapter into the active Windows source tree."""

import argparse
import shutil
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    bundle = Path(__file__).resolve().parents[1] / "roles/telegram_collector/files/src/tg_archive"
    destination = args.source.absolute() / "src/tg_archive"
    if not (destination / "mcp_server.py").is_file():
        raise ValueError("Existing MCP source tree required")
    for name in ("reader.py", "reader_config.py", "mcp_server.py", "remote_scope.py"):
        shutil.copyfile(bundle / name, destination / name)
    print("Installed four reviewed reader files; config, credentials and collector remain unchanged.")


if __name__ == "__main__":
    main()
