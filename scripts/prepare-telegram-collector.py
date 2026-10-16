"""Stage only the existing owner-approved archive scope and session over SSH stdin.

Run on the original Windows host with its existing telegram-archive interpreter.
No private payload is saved locally, printed, or passed in process arguments.
"""

import argparse
import json
import subprocess
import sys
import tomllib
from pathlib import Path

RECEIVER = r'''
import json, os, pathlib, sys, tempfile
payload = json.load(sys.stdin)
expected = {'telegram-session', 'api.json', 'settings.json', 'chats.yaml', 'approved-private-peers.json'}
if set(payload) != expected:
    raise SystemExit(1)
os.umask(0o077)
folder = pathlib.Path(tempfile.mkdtemp(prefix='.tg-collector-migration-', dir=pathlib.Path.home()))
for name, value in payload.items():
    if not isinstance(value, str):
        raise SystemExit(1)
    with (folder / name).open('x', encoding='utf-8', newline='\n') as output:
        output.write(value)
print(folder)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--host", default="abykov@192.168.1.88")
    parser.add_argument("--identity", type=Path, default=Path.home() / ".ssh/id_ed25519")
    args = parser.parse_args()
    if sys.platform != "win32":
        raise ValueError("Prepare the existing authorization on its original Windows host")
    guard = r'''
$ErrorActionPreference = 'Stop'
$tasks = @('Telegram Archive daily', 'Telegram Archive work hours')
foreach ($task in $tasks) {
    if ((Get-ScheduledTask -TaskName $task).State -ne 'Disabled') { exit 1 }
}
if (@(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -match 'tg_archive\.cli' -and $_.CommandLine -match '\brun\b'
}).Count -gt 0) { exit 1 }
'''
    check = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", guard],
                           capture_output=True, timeout=15)
    if check.returncode:
        raise ValueError("Pause Windows collection and wait for active collectors before staging")
    root = args.source.resolve()
    sys.path.insert(0, str(root / "src"))
    from tg_archive.config import load_env, load_settings
    from tg_archive.credentials import load_session
    from tg_archive.locking import sync_lock

    settings = load_settings(root / "tg-archive.toml")
    settings.require_credentials()
    if not settings.chats:
        raise ValueError("No selected chats")
    config = tomllib.loads((root / "tg-archive.toml").read_text(encoding="utf-8-sig"))
    # Explicitly whitelist config fields; never transfer DB owner/reader or SFTP secrets.
    config = {key: config[key] for key in (
        "default_since", "refresh_days", "max_per_chat_run", "media"
    )}
    config["media"] = {key: config["media"][key] for key in (
        "kinds", "max_file_mb", "max_per_run_gb", "max_total_gb", "min_free_gb"
    )}
    credentials = load_env(root / "sync.env")
    receipt = root / "state/approved-private-peers.json"
    approval = json.loads(receipt.read_text(encoding="utf-8-sig")) if receipt.exists() else {
        "approved_by": "owner", "approved_private_ids": []
    }
    if approval.get("approved_by") != "owner":
        raise ValueError("Private peers have no owner receipt")
    with sync_lock(root):
        payload = {
            "telegram-session": load_session(),
            "api.json": json.dumps({"api_id": credentials["TG_API_ID"],
                                    "api_hash": credentials["TG_API_HASH"]}),
            "settings.json": json.dumps(config, default=str),
            "chats.yaml": (root / "chats.yaml").read_text(encoding="utf-8-sig"),
            "approved-private-peers.json": json.dumps(approval),
        }
        # Code is public and fixed; the secret payload travels only through stdin.
        import base64

        receiver = base64.b64encode(RECEIVER.encode()).decode("ascii")
        command = ["ssh", "-i", str(args.identity), "-o", "BatchMode=yes",
                   "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=8", args.host,
                   "python3 -c \"import base64;exec(base64.b64decode('" + receiver + "'))\""]
        child = subprocess.run(command, input=json.dumps(payload).encode(), capture_output=True,
                               timeout=30)
    if child.returncode:
        print("Staging failed; no private diagnostics logged", file=sys.stderr)
        return 1
    folder = child.stdout.decode("utf-8").strip()
    if not folder.startswith("/home/abykov/.tg-collector-migration-") or "\n" in folder:
        print("Unexpected staging receipt; inspect server privately", file=sys.stderr)
        return 1
    print(json.dumps({"status": "staged", "selected_chats": len(settings.chats),
                      "server_directory": folder, "collection_started": False}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("Migration preparation failed; no private values logged", file=sys.stderr)
        raise SystemExit(1) from None
