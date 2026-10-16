"""Exact per-run selection from immutable HTTP and TCP scope, never a wildcard."""
from .contracts import Denied, Scope
from .native_preflight import native_services


def select_native_targets(scope: Scope, targets: list[str] | None = None) -> list[str]:
    allowed = native_services(scope.model_dump(mode='json'))
    selected = sorted(allowed) if targets is None else targets
    if (not isinstance(selected, list) or not selected or len(selected) > 16 or
            any(type(t) is not str or t not in allowed for t in selected) or len(set(selected)) != len(selected)):
        raise Denied('Select 1..16 distinct exact HTTP/TCP services from the saved scope using targets; no scope rewrite is needed')
    return sorted(selected)
