import pytest

from configure_probe import updated_config


@pytest.mark.parametrize("existing", ["", "tool_timeout_sec = 60\n"])
def test_timeout_update_preserves_unrelated_servers_and_policy(existing):
    text = ('approval_policy = "auto-review"\n[mcp_servers.other]\ncommand = "existing"\n'
            '[mcp_servers.security-lab-probe]\n' + existing + 'command = "python"\nargs = ["server.py"]\n'
            '[mcp_servers.other.env]\nPLACEHOLDER = "unchanged"\n')
    result = updated_config(text)
    assert result.count("tool_timeout_sec = 360") == 1
    assert '[mcp_servers.other.env]\nPLACEHOLDER = "unchanged"\n' in result
    assert 'approval_policy = "auto-review"' in result
    assert updated_config(result) == result


def test_missing_registration_does_not_rewrite_config():
    with pytest.raises(ValueError):
        updated_config('[mcp_servers.other]\ncommand="existing"\n')
