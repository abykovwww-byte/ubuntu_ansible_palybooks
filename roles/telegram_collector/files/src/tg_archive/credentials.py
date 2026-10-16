"""Windows Vault or an explicitly mounted private server session file."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

from .config import ConfigError

SERVICE = "telegram-archive"
ACCOUNT = "owner-string-session"


def windows_vault():
    if sys.platform != "win32":
        raise ConfigError("Windows Credential Manager is required")
    from keyring.backends.Windows import WinVaultKeyring

    # Construct the Windows backend explicitly; never select a plaintext fallback.
    return WinVaultKeyring()


def load_session() -> str:
    if sys.platform != "win32":
        path = Path("/run/secrets/telegram-session")
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ConfigError("Server session must be a private regular file")
            if info.st_uid not in {0, os.getuid()}:
                raise ConfigError("Server session has an unexpected owner")
            value = path.read_text(encoding="ascii").strip()
        except OSError:
            raise ConfigError("Private server session is unavailable") from None
        if not value:
            raise ConfigError("Private server session is empty")
        return value
    value = windows_vault().get_password(SERVICE, ACCOUNT)
    if not value:
        raise ConfigError("No Telegram session: the owner must run tg-sync login")
    return value


def save_session(value: str) -> None:
    if not value:
        raise ConfigError("Cannot save an empty Telegram session")
    windows_vault().set_password(SERVICE, ACCOUNT, value)
