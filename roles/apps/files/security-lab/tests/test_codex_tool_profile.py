"""Pure profile boundaries; synthetic metadata/receipts are not live qualification."""
import copy
import json

import pytest

from security_lab.codex_tool_profile import (
    NEGATIVE_PROBES, PINNED_CODEX_SOURCE, PINNED_CODEX_VERSION,
    build_codex_tool_profile, qualify_codex_tool_profile,
)
from security_lab.contracts import Denied


def catalog():
    # Deliberately synthetic, schema-shaped model metadata; no real model claims.
    return {"models": [{"slug": "fixture-model", "display_name": "Synthetic fixture",
                        "description": None, "supported_reasoning_levels": [],
                        "shell_type": "unified_exec", "visibility": "list", "supported_in_api": True,
                        "priority": 1, "support_verbosity": True, "apply_patch_tool_type": "freeform",
                        "truncation_policy": {"mode": "tokens", "limit": 1000},
                        "experimental_supported_tools": ["browser"], "context_window": 12345}]}


def profile(source=None, **changes):
    kwargs = {"model": "fixture-model", "catalog_path": "/host-owned/catalog.json",
              "mcp_url": "http://127.0.0.1:9132/mcp"}
    kwargs.update(changes)
    return build_codex_tool_profile(source or catalog(), **kwargs)


def receipt(candidate):
    return {"profile_hash": candidate.profile_hash, "model": candidate.model,
            "binary_version": PINNED_CODEX_VERSION, "source_commit": PINNED_CODEX_SOURCE,
            "binary_sha256": "a" * 64, "full_tool_inventory_observed": True,
            "used_fallback_model_metadata": False, "observed_tools": list(candidate.expected_tools),
            "negative_probes": {name: True for name in NEGATIVE_PROBES},
            "qualification_environment": "isolated_lab"}


def test_restricts_both_native_shell_and_patch_without_inventing_model_metadata():
    source = catalog()
    before = copy.deepcopy(source)
    candidate = profile(source)
    model = json.loads(candidate.catalog_json)["models"][0]
    assert source == before
    assert model["shell_type"] == "disabled" and model["apply_patch_tool_type"] is None
    assert model["experimental_supported_tools"] == [] and model["node_repl_disabled"] is True
    assert model["context_window"] == 12345 and model["supported_reasoning_levels"] == []
    assert "features.shell_tool=false" in candidate.config_overrides
    assert "features.unified_exec=false" in candidate.config_overrides
    assert "web_search=\"disabled\"" in candidate.config_overrides
    assert "tools.update_plan.enabled=false" in candidate.config_overrides
    assert "tools.experimental_request_user_input.enabled=false" in candidate.config_overrides
    assert len(candidate.expected_tools) == 5
    assert all(name.startswith("mcp__pentest_checks__") for name in candidate.expected_tools)


def test_catalog_only_contains_exact_selected_model_and_hash_binds_endpoint_and_path():
    source = catalog()
    source["models"].append({**source["models"][0], "slug": "other"})
    candidate = profile(source)
    assert len(json.loads(candidate.catalog_json)["models"]) == 1
    assert candidate.profile_hash != profile(mcp_url="http://127.0.0.1:9133/mcp").profile_hash
    assert candidate.profile_hash != profile(catalog_path="/other/catalog.json").profile_hash


@pytest.mark.parametrize("change", ["missing", "duplicate", "fallback", "incomplete", "malformed"])
def test_rejects_catalog_metadata_that_can_resolve_to_native_tool_fallback(change):
    source = catalog()
    if change == "missing":
        source["models"][0]["slug"] = "another"
    elif change == "duplicate":
        source["models"].append(copy.deepcopy(source["models"][0]))
    elif change == "fallback":
        source["models"][0]["used_fallback_model_metadata"] = True
    elif change == "incomplete":
        del source["models"][0]["truncation_policy"]
    else:
        source["models"][0]["supported_in_api"] = 1
    with pytest.raises(Denied):
        profile(source)


@pytest.mark.parametrize("changes", [{"catalog_path": "relative.json"}, {"mcp_url": "https://user:secret@host/mcp"},
    {"mcp_url": "https://host/mcp?token=secret"}, {"mcp_url": "https://host/mcp#fragment"},
    {"mcp_url": "file:///tmp/mcp"}, {"mcp_server": "untrusted.name"}, {"binary_version": "0.160.1"}])
def test_host_configuration_rejects_changed_binary_or_embedded_credentials(changes):
    with pytest.raises(Denied):
        profile(**changes)


def test_windows_catalog_path_and_json_values_are_arguments_not_shell_fragments():
    candidate = profile(catalog_path=r"C:\host-owned\model catalog.json")
    value = next(item.split("=", 1)[1] for item in candidate.config_overrides if item.startswith("model_catalog_json="))
    assert json.loads(value) == r"C:\host-owned\model catalog.json"


def test_exact_inventory_receipt_never_grants_production_worker_execution():
    candidate = profile()
    result = qualify_codex_tool_profile(candidate, receipt(candidate))
    assert result["qualified_for_isolated_lab"] is True
    assert result["production_execution_authorized"] is False


@pytest.mark.parametrize("tool", ["exec_command", "shell", "apply_patch", "mcp__another__execute_shell", "read_file"])
def test_additional_native_or_other_mcp_tool_invalidates_inventory(tool):
    candidate = profile()
    observed = receipt(candidate)
    observed["observed_tools"].append(tool)
    with pytest.raises(Denied):
        qualify_codex_tool_profile(candidate, observed)


@pytest.mark.parametrize("field,value", [
    ("profile_hash", "b" * 64), ("model", "another"), ("binary_version", "0.160.1"),
    ("source_commit", "b" * 40), ("binary_sha256", "unknown"), ("full_tool_inventory_observed", 1),
    ("used_fallback_model_metadata", None), ("qualification_environment", "production"),
])
def test_stale_partial_or_production_receipt_cannot_qualify(field, value):
    candidate = profile()
    observed = receipt(candidate)
    observed[field] = value
    with pytest.raises(Denied):
        qualify_codex_tool_profile(candidate, observed)


@pytest.mark.parametrize("probe", NEGATIVE_PROBES)
def test_each_scope_and_native_boundary_requires_denial_observation(probe):
    candidate = profile()
    observed = receipt(candidate)
    observed["negative_probes"][probe] = False
    with pytest.raises(Denied):
        qualify_codex_tool_profile(candidate, observed)


def test_duplicate_inventory_cannot_replace_missing_facade_tool():
    candidate = profile()
    observed = receipt(candidate)
    observed["observed_tools"][-1] = observed["observed_tools"][0]
    with pytest.raises(Denied):
        qualify_codex_tool_profile(candidate, observed)


def test_unmodified_pinned_http_mcp_native_resource_tools_block_qualification():
    candidate = profile()
    observed = receipt(candidate)
    # spec_plan.rs:1131 registers these whenever a configured MCP server exists.
    observed["observed_tools"].extend(["list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource"])
    with pytest.raises(Denied, match="exactly the five"):
        qualify_codex_tool_profile(candidate, observed)


def test_nonfinite_model_metadata_cannot_form_a_valid_catalog():
    source = catalog()
    source["models"][0]["context_window"] = float("nan")
    with pytest.raises(Denied, match="finite JSON"):
        profile(source)
