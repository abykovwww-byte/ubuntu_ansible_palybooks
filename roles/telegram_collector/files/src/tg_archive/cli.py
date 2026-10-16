"""Owner CLI: guarded discovery, migrations and text/metadata collection."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from telethon import errors

from . import __version__, collector, database, maintenance
from .config import ChatSpec, ConfigError, load_settings
from .credentials import load_session, save_session
from .discovery import list_dialogs, probe_file, scan_chat, select_peer
from .locking import AlreadyRunning, sync_lock
from .network import TelegramNetworkError, connect_client, network_failure
from .runtime import run_async
from .telegram import RequestDenied, login_scope, make_client


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Telegram Archive: read-only Telegram collection")
    result.add_argument(
        "--config", type=Path, default=Path(__file__).resolve().parents[2] / "tg-archive.toml"
    )
    result.add_argument("--version", action="version", version=__version__)
    result.add_argument("--check", action="store_true", help="Validate local files; no network")
    commands = result.add_subparsers(dest="command")
    commands.add_parser("login", help="Owner-only interactive login")
    commands.add_parser("list-chats", help="Refresh local group/channel snapshot")
    discovery = commands.add_parser("discover", help="Count metadata without DB/server writes")
    discovery.add_argument("--chat", type=int)
    discovery.add_argument("--log-requests", action="store_true")
    discovery.add_argument("--probe-file", help="Accessible group peer-id:message-id")
    commands.add_parser("migrate", help="Apply numbered SQL with temporary owner credentials")
    commands.add_parser("status", help="Safe run/chat counts; no message/profile text")
    run = commands.add_parser("run", help="Collect selected text and attachment metadata")
    run.add_argument("--chat", help="Configured chat slug")
    run.add_argument("--no-media", action="store_true")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--log-requests", action="store_true")
    fsck = commands.add_parser("fsck", help="Two-phase server/DB consistency repair")
    fsck.add_argument("--dry-run", action="store_true")
    fsck.add_argument("--confirm-mass", action="store_true")
    purge = commands.add_parser("purge", help="Owner: remove a chat and its server files")
    purge.add_argument("slug")
    return result


async def interactive_login(settings):
    if not sys.stdin.isatty():
        raise ConfigError("Owner login requires an interactive terminal")
    client = make_client(settings, "")
    operation = "connect"
    with login_scope():
        try:
            print("Connecting to Telegram using the current Windows network settings...")
            await connect_client(client)
            phone = getpass.getpass("Telegram phone (hidden): ")
            operation = "request_login_code"
            code_info = await client.send_code_request(phone)
            code = getpass.getpass("Telegram code (hidden): ")
            operation = "sign_in"
            try:
                await client.sign_in(
                    phone=phone, code=code, phone_code_hash=code_info.phone_code_hash
                )
            except errors.SessionPasswordNeededError:
                password = getpass.getpass("Telegram 2FA password (hidden): ")
                await client.sign_in(password=password)
                del password
            del phone, code, code_info
            operation = "save_session"
            save_session(client.session.save())
        except OSError as error:
            if operation == "save_session":
                raise ConfigError("Cannot save session in Windows Credential Manager") from None
            raise network_failure(operation, error) from None
        finally:
            await client.disconnect()
    print("Login complete; session saved in Windows Credential Manager")


async def run_network(args, settings):
    settings.require_credentials()
    if args.command == "login":
        await interactive_login(settings)
        return 0
    journal = (
        (lambda name: print(name, file=sys.stderr))
        if getattr(args, "log_requests", False)
        else None
    )
    client = make_client(settings, load_session(), journal)
    try:
        await connect_client(client)
        if not await client.is_user_authorized():
            raise ConfigError("Telegram session expired: the owner must run tg-sync login")
        rows, entities = await list_dialogs(client, settings)
        if args.command == "list-chats":
            print(json.dumps(rows, ensure_ascii=False, indent=2))
            return 0
        if args.probe_file:
            try:
                peer, msg = args.probe_file.split(":")
                peer, msg = int(peer), int(msg)
                if msg <= 0:
                    raise ValueError
            except ValueError:
                raise ConfigError("Use --probe-file <peer-id>:<positive-message-id>") from None
            entity = select_peer(peer, entities)
            print(json.dumps(await probe_file(client, entity, msg, settings)))
            return 0
        until = datetime.now(UTC)
        if args.chat is not None:
            entity = select_peer(args.chat, entities)
            peer = next(key for key, value in entities.items() if value is entity)
            spec = next(
                (c for c in settings.chats if c.chat_id == peer),
                ChatSpec(peer, "candidate-1", settings.default_since),
            )
            selected = [(entity, spec)]
        else:
            selected = [(entities.get(c.chat_id), c) for c in settings.chats]
        if not selected:
            raise ConfigError("Select --chat explicitly or fill chats.yaml; no default full scan")
        reports = []
        for entity, spec in selected:
            if entity is None:
                reports.append({"slug": spec.slug, "status": "lost_access", "complete": False})
            else:
                reports.append(await scan_chat(client, entity, spec, settings, until))
        print(json.dumps({"captured_at": until.isoformat(), "chats": reports}, indent=2))
        return 0 if all(r["status"] == "ok" for r in reports) else 2
    except OSError as error:
        raise network_failure("read", error) from None
    finally:
        await client.disconnect()


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and getattr(stream, "encoding", "utf-8").lower() not in {
            "utf-8",
            "utf8",
        }:
            stream.reconfigure(encoding="utf-8")
    cli = parser()
    args = cli.parse_args(argv)
    if args.check and args.command:
        cli.error("--check cannot be combined with a network command")
    if not args.check and not args.command:
        cli.error("Choose --check or a command")
    try:
        settings = load_settings(args.config)
        if args.check:
            print(
                json.dumps(
                    {
                        "configuration": "ok",
                        "network_used": False,
                        "selected_chats": len(settings.chats),
                        "credentials_present": bool(settings.api_id and settings.api_hash),
                    }
                )
            )
            return 0
        with sync_lock(settings.root):
            if args.command == "fsck":
                report = run_async(
                    maintenance.fsck(settings, dry_run=args.dry_run, confirm_mass=args.confirm_mass)
                )
                print(json.dumps(report, indent=2))
                return 0 if report["fsck_status"] == "ok" else 2
            if args.command == "purge":
                report = run_async(maintenance.purge(settings, args.slug))
                print(json.dumps(report, indent=2))
                return 2 if report["file_delete_failed"] else 0
            if args.command == "migrate":
                print(json.dumps(run_async(database.migrate(settings.root))))
                return 0
            if args.command == "status":
                print(
                    json.dumps(
                        run_async(database.status(settings.root, settings)), default=str, indent=2
                    )
                )
                return 0
            if args.command == "run":
                args.journal = (
                    (lambda name: print(name, file=sys.stderr)) if args.log_requests else None
                )
                report, code = run_async(collector.run(args, settings))
                print(json.dumps(report, indent=2))
                return code
            return run_async(run_network(args, settings))
    except RequestDenied as error:
        print(str(error), file=sys.stderr)
    except (ConfigError, AlreadyRunning, TelegramNetworkError) as error:
        print(str(error), file=sys.stderr)
    except (OSError, tomllib.TOMLDecodeError):
        print("Local file/configuration error; no raw values logged", file=sys.stderr)
    except errors.FloodWaitError as error:
        print(f"FloodWait: {error.seconds} seconds; operation stopped", file=sys.stderr)
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except Exception:
        # Exception reprs in Telegram/auth/HTTP stacks can contain private values.
        print("Operation failed; no exception parameters logged", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
