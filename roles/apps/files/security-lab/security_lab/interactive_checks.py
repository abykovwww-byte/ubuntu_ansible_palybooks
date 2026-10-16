"""Admission for immutable interactive actions; no credentials are loaded here."""
import re
from urllib.parse import urlsplit, urlunsplit

from .contracts import (Denied, HTTPRequest, WebSocketAction, SocketIOAction, WritesAction)

INTERACTIVE = (WebSocketAction, SocketIOAction, WritesAction)


def validate_action_scope(request, scope):
    if isinstance(request, HTTPRequest):
        if not any(t.permits(request.url, request.method) for t in scope.targets):
            raise Denied('Action request outside approved HTTP scope')
        return
    from .contracts import canonical
    if '{{session.' in canonical(request.model_dump(mode='json')) and not request.session_ref:
        raise Denied('Private payload bindings require a session_ref')
    parsed = urlsplit(request.url)
    origin = urlunsplit((parsed.scheme, parsed.netloc, '', '', ''))
    known = set()
    if isinstance(request, WritesAction):
        for step in [*request.steps, *request.cleanup]:
            variables = set(re.findall(r'\{([a-z][a-z0-9_]*)\}', step.path))
            values = [*step.query.values(), *step.headers.values(), *step.form.values()]
            if step.json_body is not None:
                values.append(canonical(step.json_body))
            variables.update(v for text in values for v in re.findall(r'\{\{([a-z][a-z0-9_]*)\}\}', text))
            if not variables <= known:
                raise Denied('Workflow refers to an uncaptured variable')
            # Actual bound paths are checked again by the worker before I/O.
            example = re.sub(r'\{[a-z][a-z0-9_]*\}', 'security-lab-id', step.path)
            if not any(t.permits(origin + example, step.method) for t in scope.targets):
                raise Denied('Workflow step or cleanup exceeds HTTP paths/methods')
            if known & set(step.capture):
                raise Denied('Workflow capture variables cannot be overwritten')
            known.update(step.capture)
    else:
        methods = ['GET', 'POST'] if isinstance(request, SocketIOAction) and request.transport != 'websocket' else ['GET']
        url = request.url.rstrip('/')+'/' if isinstance(request, SocketIOAction) else request.url
        if any(not any(t.permits(url, m) for t in scope.targets) for m in methods):
            raise Denied('Socket handshake/polling exceeds HTTP paths/methods')


def interactive_policy(spec, scope, case_id):
    validate_action_scope(spec, scope)
    if not isinstance(case_id, str) or not re.fullmatch(r'[a-f0-9]{32}', case_id):
        raise Denied('Interactive action needs the Controller case identity')
    parsed = urlsplit(spec.url)
    target = next((t for t in scope.targets if (t.host,t.scheme,t.port) ==
                   (parsed.hostname,parsed.scheme,parsed.port)), None)
    if target is None:
        raise Denied('Interactive origin outside HTTP scope')
    return {'schema_version':1, 'case_id':case_id, 'request':spec.model_dump(mode='json'),
            'target':target.model_dump(mode='json'), 'rps':scope.budget.rps}
