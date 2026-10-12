"""Pure, qualification-gated configuration for a checks-only Codex role.

No process, provider request, file write, authentication, or MCP listener is
started here. The trusted host supplies real model metadata and owns the
catalog file, clean CODEX_HOME, binary verification, authenticated role facade,
and observation of the actual complete tool inventory. A configuration and a
caller-supplied receipt are not proof of production worker isolation.

Pinned source: openai/codex a956835d020762cb2b570053af06f643a11c0ecc
(rust-v0.160.0). shell_tool gates shells; ApplyPatchHandler is registered
separately when apply_patch_tool_type is non-null. The authoritative static
model catalog disables both. Unknown model slugs must never use fallback.
HTTP MCP currently adds three native MCP-resource tools in this pinned release;
the exact five-tool gate consequently rejects an unmodified MCP runtime. This
module does not bypass that gap or label candidate configuration qualified.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import PurePosixPath, PureWindowsPath
import re
from urllib.parse import urlsplit

from .contracts import Denied, canonical, digest


PINNED_CODEX_VERSION = "0.160.0"
PINNED_CODEX_SOURCE = "a956835d020762cb2b570053af06f643a11c0ecc"
ROLE_TOOLS = ("get_context", "list_tools", "execute_tool_job", "get_job_status", "get_evidence")
NEGATIVE_PROBES = ("native_shell", "file_write", "outside_scope", "poc", "stale_binding", "foreign_evidence")
_REQUIRED_MODEL_FIELDS = {
    "slug", "display_name", "supported_reasoning_levels", "shell_type", "visibility",
    "supported_in_api", "priority", "support_verbosity", "truncation_policy", "experimental_supported_tools",
}


@dataclass(frozen=True)
class CodexToolProfile:
    model: str
    mcp_server: str
    catalog_json: str
    config_overrides: tuple[str, ...]
    profile_hash: str
    expected_tools: tuple[str, ...]
    binary_version: str = PINNED_CODEX_VERSION
    source_commit: str = PINNED_CODEX_SOURCE


def _absolute_path(value: str) -> str:
    if (not isinstance(value, str) or not value or any(c in value for c in "\x00\r\n")
            or not (PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute())):
        raise Denied("Host must supply an absolute immutable model catalog path")
    return value


def _model_entry(catalog: dict, model: str) -> dict:
    if not isinstance(catalog, dict) or not isinstance(catalog.get("models"), list):
        raise Denied("Host must supply an actual ModelsResponse catalog")
    if not isinstance(model, str) or not model or any(c in model for c in "\x00\r\n"):
        raise Denied("An exact selected model slug is required")
    entries = [entry for entry in catalog["models"] if isinstance(entry, dict) and entry.get("slug") == model]
    if len(entries) != 1:
        raise Denied("Selected model must occur exactly once; fallback metadata is forbidden")
    try:
        entry = json.loads(canonical(entries[0]))  # Copy, preserve real metadata.
    except (ValueError, TypeError) as error:
        raise Denied("Model metadata must be finite JSON data") from error
    if _REQUIRED_MODEL_FIELDS - entry.keys() or entry.get("used_fallback_model_metadata"):
        raise Denied("Incomplete or fallback metadata cannot qualify a role model")
    if (not isinstance(entry["display_name"], str) or not entry["display_name"]
            or not isinstance(entry["supported_reasoning_levels"], list)
            or not isinstance(entry["experimental_supported_tools"], list)
            or type(entry["supported_in_api"]) is not bool or type(entry["support_verbosity"]) is not bool
            or type(entry["priority"]) is not int or entry["visibility"] not in {"list", "hide", "none"}
            or entry["shell_type"] not in {"unified_exec", "disabled"}):
        raise Denied("Model catalog fields do not match the pinned ModelInfo schema")
    truncation = entry["truncation_policy"]
    if (not isinstance(truncation, dict) or truncation.get("mode") not in {"bytes", "tokens"}
            or type(truncation.get("limit")) is not int or truncation["limit"] <= 0):
        raise Denied("Model truncation policy is invalid")
    return entry


def build_codex_tool_profile(catalog: dict, *, model: str, catalog_path: str, mcp_url: str,
                             mcp_server: str = "pentest_checks",
                             binary_version: str = PINNED_CODEX_VERSION) -> CodexToolProfile:
    """Build a candidate profile from trusted host metadata, without enabling execution.

    The host must write catalog_json to catalog_path in a read-only role mount,
    verify the pinned binary, and start a fresh isolated configuration home with
    no additional MCP/plugins/hooks. Existing resumed provider sessions are not
    implicitly qualified. The URL is trusted host configuration, never model
    input; endpoint authentication is provisioned separately and is not placed
    in these returned strings.
    """
    if binary_version != PINNED_CODEX_VERSION:
        raise Denied("Codex version changed; a new source review and qualification are required")
    catalog_path = _absolute_path(catalog_path)
    if not isinstance(mcp_server, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", mcp_server):
        raise Denied("Invalid fixed MCP server identifier")
    if not isinstance(mcp_url, str) or any(c in mcp_url for c in "\x00\r\n"):
        raise Denied("Invalid host-owned MCP endpoint")
    try:
        endpoint = urlsplit(mcp_url)
        valid_url = (endpoint.scheme in {"http", "https"} and endpoint.hostname and not endpoint.username
                     and not endpoint.password and not endpoint.fragment and not endpoint.query)
        endpoint.port
    except ValueError as error:
        raise Denied("Invalid host-owned MCP endpoint") from error
    if not valid_url:
        raise Denied("MCP URL must have no embedded authentication, query, or fragment")
    selected = _model_entry(catalog, model)
    selected.update({"shell_type": "disabled", "apply_patch_tool_type": None,
                     "experimental_supported_tools": [], "node_repl_disabled": True,
                     "supports_search_tool": False, "tool_mode": None, "multi_agent_version": None,
                     "include_skills_usage_instructions": False, "include_plugin_usage_instructions": False,
                     "include_apps_usage_instructions": False})
    selected.pop("used_fallback_model_metadata", None)
    catalog_json = canonical({"models": [selected]})
    values = {
        "model": model,
        "model_catalog_json": catalog_path,
        "features.shell_tool": False,
        "features.unified_exec": False,
        "features.apps": False,
        "features.hooks": False,
        "features.multi_agent": False,
        "tools.update_plan.enabled": False,
        "tools.experimental_request_user_input.enabled": False,
        "web_search": "disabled",
        f"mcp_servers.{mcp_server}.url": mcp_url,
        f"mcp_servers.{mcp_server}.enabled_tools": list(ROLE_TOOLS),
        f"mcp_servers.{mcp_server}.required": True,
        f"mcp_servers.{mcp_server}.startup_timeout_sec": 30,
        f"mcp_servers.{mcp_server}.tool_timeout_sec": 120,
        f"mcp_servers.{mcp_server}.default_tools_approval_mode": "auto",
    }
    # These are config values, not shell fragments. The host passes them to the
    # pinned Codex SDK config_overrides tuple and must not interpolate a shell.
    overrides = tuple(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in values.items())
    tools = tuple(f"mcp__{mcp_server}__{name}" for name in ROLE_TOOLS)
    profile_hash = digest({"model": model, "catalog": json.loads(catalog_json), "overrides": overrides,
                           "binary_version": binary_version, "source_commit": PINNED_CODEX_SOURCE,
                           "expected_tools": tools})
    return CodexToolProfile(model, mcp_server, catalog_json, overrides, profile_hash, tools)


def qualify_codex_tool_profile(profile: CodexToolProfile, receipt: dict) -> dict:
    """Validate a trusted host's qualification observations for this exact profile.

    This function does not authenticate the caller or observe a provider. It is
    never a model-facing approval tool. Only a protected host qualification
    harness may supply a receipt. Test-shaped receipts cannot grant production
    access; worker/network qualification is independently required.
    """
    if not isinstance(profile, CodexToolProfile) or not isinstance(receipt, dict):
        raise Denied("An exact profile and host qualification receipt are required")
    if (receipt.get("profile_hash") != profile.profile_hash or receipt.get("model") != profile.model
            or receipt.get("binary_version") != profile.binary_version
            or receipt.get("source_commit") != profile.source_commit):
        raise Denied("Qualification receipt does not match the exact model/config/binary source")
    binary_hash = receipt.get("binary_sha256")
    if not isinstance(binary_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", binary_hash):
        raise Denied("Host qualification must identify the verified binary digest")
    if receipt.get("full_tool_inventory_observed") is not True or receipt.get("used_fallback_model_metadata") is not False:
        raise Denied("Partial inventory or unknown/fallback metadata cannot qualify")
    observed = receipt.get("observed_tools")
    if (not isinstance(observed, list) or any(not isinstance(name, str) for name in observed)
            or len(observed) != len(profile.expected_tools) or set(observed) != set(profile.expected_tools)):
        raise Denied("Observed model tools must be exactly the five fixed MCP facade tools")
    probes = receipt.get("negative_probes")
    if not isinstance(probes, dict) or any(probes.get(name) is not True for name in NEGATIVE_PROBES):
        raise Denied("Host must observe denial of every native and scope boundary probe")
    if receipt.get("qualification_environment") != "isolated_lab":
        raise Denied("This profile gate supports isolated lab qualification only")
    return {"qualified_for_isolated_lab": True, "production_execution_authorized": False,
            "profile_hash": profile.profile_hash, "model": profile.model,
            "binary_version": profile.binary_version, "binary_sha256": binary_hash,
            "source_commit": profile.source_commit, "observed_tools": sorted(observed),
            "negative_probes": {name: True for name in NEGATIVE_PROBES}}
