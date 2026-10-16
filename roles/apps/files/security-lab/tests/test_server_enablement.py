"""Server enablement helper: snapshot integrity, remote digest parity, client switch."""
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from lab import server_enablement as se
from security_lab.controller import Controller
from test_controller import NOW, approve, scope_body


def make_store(tmp_path, case_ids=("example.com",)):
    control = Controller(tmp_path / "state", "fixture-operator", clock=lambda: NOW)
    for seed in case_ids:
        control.collect_fixture(control.create_case(seed)["id"])
    control.close()
    return tmp_path / "state" / "control.db"


def settings(tmp_path, case_dir, credentials, **extra):
    value = {"data_dir": str(case_dir), "principal": "fixture-operator", "port": 39207,
             "service_id": "0" * 32, "credentials_file": str(credentials)}
    value.update(extra)
    return value


def test_snapshot_matches_store_summary_and_is_consistent(tmp_path):
    database = make_store(tmp_path, ("example.com", "other.example.com"))
    args = SimpleNamespace(settings=tmp_path / "service.json", out=tmp_path / "snap.db")
    args.settings.write_text(json.dumps(settings(tmp_path, database.parent, tmp_path / "creds.json")), encoding="utf-8")
    assert se.command_snapshot(args) == 0
    digest = se.store_summary(tmp_path / "snap.db")
    assert digest == se.store_summary(database)
    assert digest["cases_count"] == 2
    assert json.loads((tmp_path / "snap.db.json").read_text())["history_sha256"] == digest["history_sha256"]


def test_remote_program_digest_parity(tmp_path):
    database = make_store(tmp_path)
    expected = se.store_summary(database)
    program = (
        "import hashlib,json,sqlite3,sys;"
        "t=('" + "','".join(se.TABLES) + "');"
        "db=sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True);"
        "h={k:db.execute('SELECT * FROM '+k+' ORDER BY rowid').fetchall() for k in t};"
        "c=[r[0] for r in db.execute('SELECT id FROM cases ORDER BY created,id')];"
        "print(json.dumps({'cases_count':len(c),'cases':c,"
        "'history_sha256':hashlib.sha256(json.dumps(h,sort_keys=True).encode()).hexdigest()}))")
    result = subprocess.run([sys.executable, "-c", program, str(database)], capture_output=True, text=True)
    assert result.returncode == 0
    assert json.loads(result.stdout) == expected


def test_ssh_prefix_uses_identity():
    command = se.ssh_prefix("abykov@192.168.1.88", "/home/op/.ssh/id")
    assert command[-1] == "abykov@192.168.1.88"
    assert command[command.index("-i") + 1] == "/home/op/.ssh/id"
    assert "-T" in command and "BatchMode=yes" in command


def test_prepare_writes_fragment_and_staged_without_touching_live_client(tmp_path, capsys):
    database = make_store(tmp_path)
    credentials = tmp_path / "credentials.json"
    settings_path = tmp_path / "service.json"
    original = json.dumps(settings(tmp_path, database.parent, credentials))
    settings_path.write_text(original, encoding="utf-8")
    assert se.command_prepare(SimpleNamespace(settings=settings_path, principal="abykov")) == 0
    out = capsys.readouterr().out
    assert "security_lab_enabled: true" in (tmp_path / "local-overrides.security-lab.yml").read_text()
    staged = json.loads((tmp_path / "credentials.json.staged").read_text())
    assert staged["codex"] != staged["opencode"] and len(staged["codex"]) >= 40
    assert settings_path.read_text(encoding="utf-8") == original  # running client unchanged
    assert not credentials.exists()
    assert staged["codex"] not in out  # no secret leakage


def test_deploy_uploads_private_files_and_prints_scoped_command(tmp_path, monkeypatch):
    database = make_store(tmp_path)
    settings_path = tmp_path / "service.json"
    settings_path.write_text(json.dumps(settings(tmp_path, database.parent, tmp_path / "c.json")), encoding="utf-8")
    se.command_prepare(SimpleNamespace(settings=settings_path, principal="abykov"))
    uploads = []
    monkeypatch.setattr(se, "_scp_upload", lambda src, ssh, ident, dest: uploads.append((str(src), dest)))
    snapshot = tmp_path / "snap.db"
    snapshot.write_bytes(b"db")
    import io
    from contextlib import redirect_stdout
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        assert se.command_deploy(SimpleNamespace(settings=settings_path, ssh="abykov@192.168.1.88",
            identity="/k", snapshot=snapshot, server_overrides="/home/abykov/security-lab-overrides.yml",
            server_seed="/home/abykov/security-lab-seed.db", repo_dir="/opt/ubuntu_ansible_palybooks")) == 0
    report = json.loads(buffer.getvalue())
    assert {dest for _, dest in uploads} == {"/home/abykov/security-lab-overrides.yml",
                                             "/home/abykov/security-lab-seed.db"}
    assert "playbooks/apps.yml" in report["apply_command"] and "--tags security-lab" in report["apply_command"]
    assert "@" + report["uploaded"]["overrides"] in report["apply_command"]


def test_switch_reuses_staged_tokens(tmp_path):
    database = make_store(tmp_path)
    credentials = tmp_path / "credentials.json"
    settings_path = tmp_path / "service.json"
    settings_path.write_text(json.dumps(settings(tmp_path, database.parent, credentials)), encoding="utf-8")
    se.command_prepare(SimpleNamespace(settings=settings_path, principal="abykov"))
    staged = json.loads((tmp_path / "credentials.json.staged").read_text())
    assert se.command_switch(SimpleNamespace(settings=settings_path, ssh="abykov@host", identity="/k",
        principal="abykov", apply=True)) == 0
    assert json.loads(credentials.read_text()) == staged


def test_switch_dry_run_writes_nothing_then_apply_preserves_namespace(tmp_path, capsys):
    database = make_store(tmp_path)
    credentials = tmp_path / "credentials.json"
    case_dir = database.parent
    settings_path = tmp_path / "service.json"
    settings_path.write_text(json.dumps(settings(tmp_path, case_dir, credentials)), encoding="utf-8")
    dry = SimpleNamespace(settings=settings_path, ssh="abykov@192.168.1.88", identity="/k",
                          principal="abykov", apply=False)
    assert se.command_switch(dry) == 0
    assert not credentials.exists() and not (tmp_path / "local-overrides.security-lab.yml").exists()
    assert "token_urlsafe" not in capsys.readouterr().out
    applied = SimpleNamespace(settings=settings_path, ssh="abykov@192.168.1.88", identity="/k",
                              principal="abykov", apply=True)
    assert se.command_switch(applied) == 0
    tokens = json.loads(credentials.read_text())
    remote = json.loads(settings_path.read_text())
    override = (tmp_path / "local-overrides.security-lab.yml").read_text()
    assert remote["remote_ssh"] == "abykov@192.168.1.88" and remote["data_dir"] == str(case_dir)
    assert remote["service_id"] == "0" * 32 and remote["port"] == 39207
    assert len(tokens["codex"]) >= 40 and tokens["codex"] != tokens["opencode"]
    assert f"security_lab_codex_token: {tokens['codex']}" in override
    assert "security_lab_enabled: true" in override
    assert (tmp_path / "service.json.pre-remote").exists()
