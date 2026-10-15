"""Exact per-run selection from immutable HTTP and TCP scope, never a wildcard."""
from .contracts import Denied, Scope


def select_native_targets(scope: Scope, targets: list[str] | None = None) -> list[str]:
    allowed = {f'{t.scheme}://{t.host}' + ('' if t.port == (443 if t.scheme == 'https' else 80) else f':{t.port}')
               + path.rstrip('/') for t in scope.targets for path in t.paths}
    allowed.update(f'tcp://{t.host}:{port}' for t in scope.network_targets for port in t.tcp_ports)
    selected = sorted(allowed) if targets is None else targets
    if (not isinstance(selected, list) or not selected or len(selected) > 16 or
            any(type(t) is not str or t not in allowed for t in selected) or len(set(selected)) != len(selected)):
        raise Denied('Select 1..16 distinct exact HTTP/TCP services from the saved scope using targets; no scope rewrite is needed')
    return sorted(selected)
