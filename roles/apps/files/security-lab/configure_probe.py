"""Set only registered probe timeouts; preserve all other Codex settings."""

import argparse
from pathlib import Path
import re
import tomllib


def updated_config(text: str) -> str:
    data = tomllib.loads(text)
    if "security-lab-probe" not in data.get("mcp_servers", {}):
        raise ValueError("Register security-lab-probe before setting its timeout")
    pattern = r"(?ms)^(\[mcp_servers\.security-lab-probe\][ \t]*\r?\n)(.*?)(?=^\[|\Z)"
    found = re.search(pattern, text)
    if not found:
        raise ValueError("Unsupported probe TOML layout; refusing a broad rewrite")
    body = found.group(2)
    if re.search(r"(?m)^tool_timeout_sec\s*=", body):
        body = re.sub(r"(?m)^tool_timeout_sec\s*=.*$", "tool_timeout_sec = 120", body)
    else:
        body = "tool_timeout_sec = 120\n" + body
    if re.search(r"(?m)^startup_timeout_sec\s*=", body):
        body = re.sub(r"(?m)^startup_timeout_sec\s*=.*$", "startup_timeout_sec = 30", body)
    else:
        body = "startup_timeout_sec = 30\n" + body
    result = text[:found.start()] + found.group(1) + body + text[found.end():]
    parsed = tomllib.loads(result)
    # Structural read-back: only these exact settings may change.
    parsed["mcp_servers"]["security-lab-probe"].pop("tool_timeout_sec", None)
    data["mcp_servers"]["security-lab-probe"].pop("tool_timeout_sec", None)
    parsed["mcp_servers"]["security-lab-probe"].pop("startup_timeout_sec", None)
    data["mcp_servers"]["security-lab-probe"].pop("startup_timeout_sec", None)
    if parsed != data:
        raise ValueError("Unexpected change outside the probe timeout")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    text = args.config.read_text(encoding="utf-8")
    revised = updated_config(text)
    if revised != text:
        args.config.write_text(revised, encoding="utf-8", newline="")
    print("security-lab-probe startup timeout: 30 seconds; tool timeout: 120 seconds; other settings preserved")
