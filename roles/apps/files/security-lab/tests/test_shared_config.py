import json
import tomllib

from configure_shared import legacy_settings, update_codex, update_opencode


def test_client_config_updates_preserve_other_servers_models_permissions_and_skills(tmp_path):
    codex = ('model = "existing-model"\n[mcp_servers.other]\ncommand = "other"\n'
        '[mcp_servers.security-lab-probe]\ncommand = "python"\n'
        'args = ["server.py", "--data-dir", "cases", "--principal", "operator", '
        '"--pentestgpt-host", "abykov@192.168.1.88", "--pentestgpt-identity", "identity-path"]\n'
        'startup_timeout_sec = 20\n[features]\nexisting = true\n')
    settings = tmp_path / "service.json"
    parsed = tomllib.loads(codex)
    derived = legacy_settings(parsed["mcp_servers"]["security-lab-probe"])
    assert derived["data_dir"] == "cases" and derived["principal"] == "operator"
    assert derived["pentestgpt_identity"] == "identity-path"
    revised = update_codex(codex, "C:/path with space/python.exe", settings)
    result = tomllib.loads(revised)
    assert result["mcp_servers"]["other"] == parsed["mcp_servers"]["other"]
    assert result["model"] == parsed["model"] and result["features"] == parsed["features"]
    assert result["mcp_servers"]["security-lab-probe"]["args"][-1] == "codex"
    assert update_codex(revised, "C:/path with space/python.exe", settings) == revised
    before = {"$schema": "https://opencode.ai/config.json", "model": "provider/model",
        "mcp": {"other": {"type": "local", "command": ["other"]}},
        "permission": {"edit": "ask"}, "skills": {"paths": ["existing"], "urls": ["https://example.test/skills"]}}
    revised = update_opencode(json.dumps(before), "C:/path with space/python.exe", settings)
    after = json.loads(revised)
    assert after["model"] == before["model"] and after["permission"] == before["permission"]
    assert after["mcp"]["other"] == before["mcp"]["other"]
    assert after["skills"]["paths"][0] == "existing" and after["skills"]["urls"] == before["skills"]["urls"]
    assert after["mcp"]["security-lab-probe"]["command"][-1] == "opencode"
    assert update_opencode(revised, "C:/path with space/python.exe", settings) == revised
