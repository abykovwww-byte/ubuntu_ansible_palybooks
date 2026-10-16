"""Prepare moving an existing engagement to the server-resident control plane.

Three no-sudo commands run from the operator workstation. They never print
secret tokens, never mutate the server, and never start target work.

  snapshot  Consistent copy of the workstation store (SQLite backup) plus its
            case list and history digest for a later integrity comparison.
  verify    Read the server store over SSH and prove the copied cases match the
            snapshot digest; report any missing or changed case.
  switch    Configure the workstation client to reach the server through an SSH
            loopback forward, generate the shared service id/tokens and write a
            private local-overrides fragment for the operator to place on the
            server. Dry-run unless --apply.

Applying Ansible with sudo, copying the snapshot to the server and starting the
systemd service remain the operator's explicit external steps.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
import uuid

TABLES = ("cases", "manifests", "audit", "evidence", "source_receipts", "surface", "surface_links", "jobs")


def store_summary(database: Path) -> dict:
    """Same projection the installer uses; read-only and non-mutating."""
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        history = {table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall() for table in TABLES}
        cases = [row[0] for row in db.execute("SELECT id FROM cases ORDER BY created,id").fetchall()]
        return {"cases_count": len(cases), "cases": cases,
                "history_sha256": hashlib.sha256(json.dumps(history, sort_keys=True).encode("utf-8")).hexdigest()}


def ssh_prefix(ssh: str, identity: str | None) -> list[str]:
    windows = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32/OpenSSH/ssh.exe"
    executable = str(windows) if os.name == "nt" and windows.exists() else "ssh"
    command = [executable, "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    if identity:
        command += ["-i", identity]
    return command + [ssh]


def remote_summary(ssh: str, identity: str | None, database: str) -> dict:
    program = (
        "import hashlib,json,sqlite3,sys;"
        "t=('" + "','".join(TABLES) + "');"
        "db=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True);"
        "h={k:db.execute('SELECT * FROM '+k+' ORDER BY rowid').fetchall() for k in t};"
        "c=[r[0] for r in db.execute('SELECT id FROM cases ORDER BY created,id')];"
        "print(json.dumps({'cases_count':len(c),'cases':c,"
        "'history_sha256':hashlib.sha256(json.dumps(h,sort_keys=True).encode()).hexdigest()}))")
    # Stream the program over stdin: a multi-line argv does not survive the
    # ssh remote shell quoting. `python3 - <db>` reads the script from stdin.
    result = subprocess.run(ssh_prefix(ssh, identity) + ["python3", "-", database],
                            input=program, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("Remote store read failed: " + (result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown"))
    return json.loads(result.stdout.strip().splitlines()[-1])


def command_snapshot(args) -> int:
    settings = json.loads(args.settings.read_text(encoding="utf-8"))
    source = Path(settings["data_dir"]) / "control.db"
    if not source.is_file():
        raise RuntimeError("Workstation store not found")
    out = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src, sqlite3.connect(out) as dst:
        src.backup(dst)
    summary = store_summary(out)
    out.with_suffix(out.suffix + ".json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    _harden_local(out)
    _harden_local(out.with_suffix(out.suffix + ".json"))
    print(json.dumps({"snapshot": str(out), "cases_count": summary["cases_count"],
                      "history_sha256": summary["history_sha256"], "secrets_printed": False}, indent=2))
    return 0


def command_verify(args) -> int:
    expected = json.loads(Path(str(args.snapshot) + ".json").read_text(encoding="utf-8"))
    actual = remote_summary(args.ssh, args.identity, args.server_database)
    missing = sorted(set(expected["cases"]) - set(actual["cases"]))
    digest_ok = expected["history_sha256"] == actual["history_sha256"]
    report = {"snapshot_cases": expected["cases_count"], "server_cases": actual["cases_count"],
              "digest_match": digest_ok, "missing_in_server": missing, "preserved": digest_ok and not missing}
    print(json.dumps(report, indent=2))
    if not report["preserved"]:
        print("Store integrity NOT confirmed; do not switch clients until reconciled.", file=sys.stderr)
        return 1
    return 0


def _load_tokens(settings_path: Path, settings: dict, staged: Path) -> dict:
    credentials_path = Path(settings["credentials_file"])
    for candidate in (staged, credentials_path):
        if candidate.is_file():
            tokens = json.loads(candidate.read_text(encoding="utf-8"))
            if set(tokens) == {"codex", "opencode"} and all(isinstance(v, str) and len(v) >= 40 for v in tokens.values()):
                return tokens
    return {"codex": secrets.token_urlsafe(48), "opencode": secrets.token_urlsafe(48)}


def _override_text(settings: dict, service_id: str, principal: str, service_user: str, tokens: dict) -> str:
    # security_lab_principal is the Controller identity that must equal the
    # migrated store's case owner; security_lab_owner is the Linux service user.
    return (f"# Generated by lab/server_enablement.py. Place on the server and apply\n"
            f"# with the scoped command printed by `deploy`. Server-private; do not commit.\n"
            f"security_lab_enabled: true\n"
            f"security_lab_principal: {principal}\n"
            f"security_lab_owner: {service_user}\n"
            f"security_lab_port: {settings['port']}\nsecurity_lab_service_id: {service_id}\n"
            f"security_lab_codex_token: {tokens['codex']}\n"
            f"security_lab_opencode_token: {tokens['opencode']}\n")


def command_prepare(args) -> int:
    """Generate the shared ids/tokens and the server override fragment only.

    Does NOT modify the running client's service.json or credentials.json.
    """
    settings_path = args.settings
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    staged = settings_path.parent / "credentials.json.staged"
    service_id = settings.get("service_id") or uuid.uuid4().hex
    tokens = _load_tokens(settings_path, settings, staged)
    override_path = settings_path.parent / "local-overrides.security-lab.yml"
    staged.write_text(json.dumps(tokens, indent=2) + "\n", encoding="utf-8")
    override_path.write_text(_override_text(settings, service_id, settings["principal"], args.service_user, tokens), encoding="utf-8")
    _harden_local(staged)
    _harden_local(override_path)
    print(json.dumps({"prepared": True, "service_id": service_id, "port": settings["port"],
                      "principal": settings["principal"], "service_user": args.service_user,
                      "private_overrides": str(override_path), "staged_credentials": str(staged),
                      "client_unchanged": True, "secrets_printed": False}, indent=2))
    return 0


def _harden_local(path: Path) -> None:
    """Best-effort owner-only mode for private local artifacts."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _scp_upload(source: Path, ssh: str, identity: str | None, destination: str) -> None:
    _, _, host = ssh.partition("@")
    windows = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32/OpenSSH/scp.exe"
    executable = str(windows) if os.name == "nt" and windows.exists() else "scp"
    command = [executable, "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    if identity:
        command += ["-i", identity]
    command += [str(source), f"{ssh}:{destination}"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError(f"Upload to {host} failed")
    # scp preserves no POSIX mode from a Windows source; force owner-only.
    harden = subprocess.run(ssh_prefix(ssh, identity) + ["chmod 600 " + shlex.quote(destination)],
                            capture_output=True, text=True, timeout=30)
    if harden.returncode:
        raise RuntimeError(f"Could not restrict {destination} to 0600")


def command_deploy(args) -> int:
    """Upload the private override fragment and (optionally) the store snapshot.

    Both land in the operator's home (no sudo); prints the exact scoped apply
    command and the remaining cutover steps.
    """
    settings_path = args.settings
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    override_path = settings_path.parent / "local-overrides.security-lab.yml"
    if not override_path.is_file():
        print("Run `prepare` first.", file=sys.stderr)
        return 1
    _scp_upload(override_path, args.ssh, args.identity, args.server_overrides)
    uploaded = {"overrides": args.server_overrides}
    if args.snapshot is not None:
        _scp_upload(args.snapshot, args.ssh, args.identity, args.server_seed)
        uploaded["seed_store"] = args.server_seed
    home = args.server_overrides.rsplit("/", 1)[0]
    apply_command = (
        f"sudo {args.repo_dir}/.venv/bin/ansible-playbook -i {args.repo_dir}/inventories/local/hosts.yml "
        f"{args.repo_dir}/playbooks/apps.yml "
        f"--extra-vars @/etc/ansible/local-overrides.yml "
        f"--extra-vars @{args.server_overrides} "
        f"-e security_lab_seed_store={args.server_seed if args.snapshot is not None else ''} "
        f"--tags security-lab")
    print(json.dumps({"uploaded": uploaded, "secrets_printed": False,
                      "apply_command": apply_command,
                      "then": ["verify --snapshot <file>", "switch --apply", "restart Codex/OpenCode"],
                      "note": "Cut over only when the workstation Controller is idle and stopped; one store writer."},
                     indent=2))
    return 0


def command_switch(args) -> int:
    settings_path = args.settings
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    credentials_path = Path(settings["credentials_file"])
    staged = settings_path.parent / "credentials.json.staged"
    port = settings["port"]
    service_id = settings.get("service_id") or uuid.uuid4().hex
    tokens = _load_tokens(settings_path, settings, staged)
    remote = {**settings, "remote_ssh": args.ssh, "remote_ssh_identity": args.identity,
              "port": port, "service_id": service_id}
    override_path = settings_path.parent / "local-overrides.security-lab.yml"
    backup = settings_path.with_suffix(".json.pre-remote")
    if not args.apply:
        print(json.dumps({"apply": False, "would_write": [str(settings_path), str(credentials_path),
              str(override_path)], "namespace_preserved": True, "secrets_printed": False}, indent=2))
        return 0
    if not backup.exists():
        shutil.copy2(settings_path, backup)
    credentials_path.write_text(json.dumps(tokens, indent=2) + "\n", encoding="utf-8")
    override_path.write_text(_override_text(settings, service_id, settings["principal"], args.service_user, tokens), encoding="utf-8")
    settings_path.write_text(json.dumps(remote, indent=2) + "\n", encoding="utf-8")
    for path in (credentials_path, override_path, settings_path):
        _harden_local(path)
    print(json.dumps({"apply": True, "client_settings": str(settings_path),
                      "private_overrides": str(override_path), "secrets_printed": False,
                      "backup": str(backup)}, indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--settings", type=Path, required=True)
    snap = sub.add_parser("snapshot", parents=[common])
    snap.add_argument("--out", type=Path, required=True)
    prep = sub.add_parser("prepare", parents=[common])
    prep.add_argument("--service-user", default="abykov")
    dep = sub.add_parser("deploy", parents=[common])
    dep.add_argument("--ssh", required=True)
    dep.add_argument("--identity")
    dep.add_argument("--snapshot", type=Path)
    dep.add_argument("--server-overrides", default="/home/abykov/security-lab-overrides.yml")
    dep.add_argument("--server-seed", default="/home/abykov/security-lab-seed.db")
    dep.add_argument("--repo-dir", default="/opt/ubuntu_ansible_palybooks")
    ver = sub.add_parser("verify", parents=[common])
    ver.add_argument("--snapshot", type=Path, required=True)
    ver.add_argument("--ssh", required=True)
    ver.add_argument("--identity")
    ver.add_argument("--server-database", default="/srv/app-data/security-lab/control.db")
    sw = sub.add_parser("switch", parents=[common])
    sw.add_argument("--ssh", required=True)
    sw.add_argument("--identity")
    sw.add_argument("--service-user", default="abykov")
    sw.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    handlers = {"snapshot": command_snapshot, "verify": command_verify, "switch": command_switch,
                "prepare": command_prepare, "deploy": command_deploy}
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
