"""Owner runs this as root after PR merge and after pausing Windows collection."""

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import yaml


def activate(staging):
    if os.geteuid() != 0:
        raise ValueError("Interactive sudo is required")
    if not staging.is_absolute() or staging.parent != Path("/home/abykov"):
        raise ValueError("Unexpected staging location")
    if not staging.name.startswith(".tg-collector-migration-") or staging.is_symlink():
        raise ValueError("Unexpected staging directory")
    directory_info = staging.lstat()
    if not stat.S_ISDIR(directory_info.st_mode) or directory_info.st_mode & 0o077:
        raise ValueError("Staging must be a private directory")
    names = {"api.json", "settings.json", "telegram-session", "chats.yaml",
             "approved-private-peers.json"}
    if {entry.name for entry in staging.iterdir()} != names:
        raise ValueError("Unexpected staging contents")
    for name in names:
        info = (staging / name).lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("Staged inputs must be private regular files")
        if info.st_uid != directory_info.st_uid:
            raise ValueError("Unexpected staged input owner")
    destination = Path("/etc/ansible/telegram-collector-private")
    if destination.is_symlink():
        raise ValueError("Private destination must not be redirected")
    destination.mkdir(mode=0o700, exist_ok=True)
    os.chmod(destination, 0o700)
    for name in names:
        target = destination / name
        if target.is_symlink():
            raise ValueError("Private input must not be redirected")
        # Root private destination; files never appear in command arguments or output.
        with target.open("w", encoding="utf-8") as output:
            os.chmod(target, 0o600)
            output.write((staging / name).read_text(encoding="utf-8"))
    overrides = Path("/etc/ansible/local-overrides.yml")
    text = overrides.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("Invalid overrides document")
    if "telegram_collector_enabled" in data:
        pattern = r"(?m)^telegram_collector_enabled:[ \t]*(?:true|false)[ \t]*(?:#.*)?$"
        text, replacements = re.subn(pattern, "telegram_collector_enabled: true", text)
        if replacements != 1:
            raise ValueError("Enable the collector manually in the existing overrides")
    else:
        text = text.rstrip() + "\ntelegram_collector_enabled: true\n"
    temporary = overrides.with_name(".telegram-collector-overrides.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(text)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, overrides)
    # Remove only the verified, exact staging directory after durable private installation.
    shutil.rmtree(staging)
    repository = Path(__file__).resolve().parents[1]
    return subprocess.run([str(repository / "scripts/apply-local.sh"),
                           "playbooks/telegram-collector.yml"], cwd=repository).returncode


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("Exactly one staging path is required")
        raise SystemExit(activate(Path(sys.argv[1])))
    except Exception:
        print("Activation failed; no private values logged", file=sys.stderr)
        raise SystemExit(1) from None
