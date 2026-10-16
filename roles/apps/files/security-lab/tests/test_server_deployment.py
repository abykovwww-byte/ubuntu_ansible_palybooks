"""Server-resident control plane: runtime selection and remote client tunnel."""
import pytest

from client_proxy import ssh_tunnel_command
from shared_server import open_service_log, runtime_for_settings


def test_service_log_prefers_configured_writable_file(tmp_path):
    settings_path = tmp_path / "service.json"
    settings_path.write_text("{}", encoding="utf-8")
    target = tmp_path / "data" / "service.log"
    target.parent.mkdir()
    handle = open_service_log(settings_path, {"log_file": str(target)})
    assert handle is not None and handle.name == str(target)
    handle.close()


def test_service_log_falls_back_to_settings_dir_then_none(tmp_path):
    settings_path = tmp_path / "service.json"
    settings_path.write_text("{}", encoding="utf-8")
    # Unwritable/nonexistent configured path falls back to the settings-adjacent file.
    handle = open_service_log(settings_path, {"log_file": "/no/such/dir/service.log"})
    assert handle is not None and handle.name == str(settings_path.with_suffix(".service.log"))
    handle.close()


def test_local_runtime_selects_host_compose_without_starting_anything():
    runtime = runtime_for_settings({
        "pentestgpt_mode": "local",
        "pentestgpt_directory": "/srv/apps/pentestgpt",
        "pentestgpt_runs_directory": "/srv/app-data/pentestgpt/runs",
        "pentestgpt_workspace_directory": "/srv/app-data/pentestgpt/workspace",
        "pentestgpt_tool_service": "pentest-tools"})
    assert runtime is not None and runtime.config.mode == "local"
    assert runtime.config.compose_directory == "/srv/apps/pentestgpt"
    assert runtime.config.tool_service == "pentest-tools"


def test_ssh_runtime_and_absent_runtime():
    assert runtime_for_settings({"pentestgpt_mode": "ssh"}) is None
    runtime = runtime_for_settings({"pentestgpt_mode": "ssh", "pentestgpt_host": "abykov@192.168.1.88",
        "pentestgpt_identity": "/home/op/.ssh/id"})
    assert runtime.config.mode == "ssh" and runtime.config.ssh_host == "abykov@192.168.1.88"


def test_unknown_mode_is_refused():
    with pytest.raises(ValueError, match="pentestgpt_mode"):
        runtime_for_settings({"pentestgpt_mode": "container"})


def test_remote_tunnel_argv_forwards_loopback_only():
    assert ssh_tunnel_command({"port": 39207}) is None
    command = ssh_tunnel_command({"port": 39207, "remote_ssh": "abykov@192.168.1.88",
        "remote_ssh_identity": "/home/op/.ssh/id"})
    assert command[-1] == "abykov@192.168.1.88"
    assert "-N" in command and "BatchMode=yes" in command and "ExitOnForwardFailure=yes" in command
    forward = command[command.index("-L") + 1]
    assert forward == "127.0.0.1:39207:127.0.0.1:39207"
    assert command[command.index("-i") + 1] == "/home/op/.ssh/id"


def test_remote_tunnel_without_identity_omits_only_the_key():
    command = ssh_tunnel_command({"port": 40000, "remote_ssh": "abykov@host"})
    assert "-i" not in command and command[-1] == "abykov@host"
