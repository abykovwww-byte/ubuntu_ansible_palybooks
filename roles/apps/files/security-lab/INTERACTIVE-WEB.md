# Saved WebSocket, Socket.IO and writes actions

These profiles use the existing `propose_actions` →
`activate_actions_from_task` → `start_poc_action` lifecycle, job leases and
evidence store. They are immutable executable actions, not unrestricted browser
scripts. A software-installation task does not authorize actions on an external
target. Current operator task, HTTP scope and separately activated action plan
must cover the selected service and effects. No extra approval UI is introduced.

## Common contract

`action.request.tool` selects `websocket`, `socketio`, or `writes`. `url` is an
HTTP(S) URL with an explicit or canonical default port; WebSocket also accepts
WS/WSS input, canonicalized to its HTTP(S) handshake service for scope matching.
The scope continues to declare HTTP(S) service/path/method grants.
Messages and workflow requests cannot add origins. Plain checks refuse these
profiles before DNS. An attempted action cannot automatically be replayed.

The host selects one DNS address, supplies a read-only hosts mapping and mounts
only the new job's artifacts. aiohttp uses standard TLS validation, no environment
proxy and no redirects. The worker has a wall deadline; request/frame/byte caps
add bounded application behaviour, not a claim of independent network-worker
qualification. HTTP requests and application emits obey scope RPS; protocol
heartbeat/control traffic is handled by the pinned libraries and bounded by
frame/time budgets.

Full `transcript.jsonl` and `summary.json` stay in the job artifact directory.
Transcript entries contain timestamps, request metadata, response status/body
hashes, bounded redacted messages/events/acknowledgements and cleanup outcomes.
Known selected-session values and captured variables are scrubbed; JSON keys
containing credential/token/cookie/CSRF names are redacted. Binary frames are
represented by size/hash. Do not put credentials into action bodies or messages;
use the server-side grant below. A completed action or matching predicate is an
observation, not vulnerability proof.

## WebSocket

Example `action.request`:

```json
{
  "tool": "websocket",
  "url": "https://app.example.test/ws",
  "session_ref": "test_reader",
  "messages": [{"text": "{\"type\":\"status\"}", "expect_contains": "status"}],
  "max_frames": 32,
  "max_bytes": 65536,
  "message_seconds": 5,
  "listen_seconds": 1,
  "seconds": 30
}
```

Scope must permit GET on the exact endpoint. One connection, declared messages,
bounded reception and protocol close are supported; reconnect and arbitrary
callbacks are not accepted.

## Socket.IO / Engine.IO

```json
{
  "tool": "socketio",
  "url": "https://app.example.test/socket.io/",
  "namespace": "/",
  "transport": "polling_upgrade",
  "session_ref": "test_reader",
  "events": [{"name": "status", "data": {}, "expect_ack": true, "ack_contains": "ok"}],
  "max_frames": 64,
  "max_bytes": 65536,
  "max_requests": 32,
  "seconds": 60
}
```

The pinned `python-socketio`/`python-engineio` client handles Engine.IO handshake,
session identifiers, namespaces, acknowledgements, heartbeat and transport
upgrade. Select `websocket`, `polling` or `polling_upgrade` explicitly. Polling
and upgrade require both GET and POST on the declared Socket.IO path. Reserved
event names such as `connect`/`disconnect` cannot be supplied as application
emits. Protocol packet counters and the event transcript do not authorize any
additional business operation.

## Writes workflows

`url` declares the origin root. Every step declares a method and absolute path;
JSON, form-urlencoded and inline base64 multipart parts are supported. No host
file or arbitrary upload path can be selected. All main and cleanup paths and
methods are admitted before DNS, then checked again with actual captured values.

Example request:

```json
{
  "tool": "writes",
  "url": "https://app.example.test/",
  "session_ref": "test_writer",
  "seconds": 60,
  "steps": [
    {"name": "csrf", "method": "GET", "path": "/api/csrf", "capture": {"csrf": "/token"}, "expect_status": 200},
    {"name": "create", "method": "POST", "path": "/api/test-items", "headers": {"x-csrf-token": "{{csrf}}"}, "json_body": {"name": "owned-test"}, "capture": {"item": "/id"}, "expect_status": 201},
    {"name": "update", "method": "PATCH", "path": "/api/test-items/{item}", "form": {"name": "updated-test"}, "expect_status": 200}
  ],
  "cleanup": [
    {"name": "remove", "method": "DELETE", "path": "/api/test-items/{item}", "expect_status": 204},
    {"name": "verify-absence", "method": "GET", "path": "/api/test-items/{item}", "expect_status": 404}
  ]
}
```

`capture` values are JSON pointers into a response. `{{variable}}` references an
earlier capture in JSON/form/query/public headers; `{variable}` fills exactly
one path segment, restricted to alphanumeric/underscore/hyphen characters.
Undefined, overwritten, ambiguous or out-of-scope bindings fail closed.

Cleanup is required unless the saved request explicitly sets
`cleanup_required=false` for an operation with no persistent test data. A
reserved 1–8 second cleanup window runs after success, failed expectations or an
internal timeout. Failed cleanup makes the action fail and stays explicit in the
report. An operator stop closes the worker rather than initiating new cleanup
requests; cleanup may then need a separately planned owner action. No cleanup
success or deletion is inferred merely from an HTTP response without the chosen
predicate/absence check.

## Server-owned test sessions

`{{session.name}}` references a value from the selected private grant inside an
event, text frame, JSON/form/query or public header. This supports protocols
with a login event instead of connect-time authentication without putting a
test password/token into the action manifest. Missing references fail before
network contact; resolved payloads are bounded again before sending.

Ansible creates `<pentestgpt_runs_dir parent>/web-sessions` with mode 0700.
The owner provisions `<reference>.json` there as a regular, non-symlink 0600
file. Session references are identifiers, never paths. There is no MCP endpoint
to read or create arbitrary credentials.

Example **shape only**, with placeholders rather than real values:

```json
{
  "schema_version": 1,
  "case_ids": ["EXACT_CONTROLLER_CASE_ID"],
  "origins": ["https://app.example.test:443"],
  "expires_at": 2000000000,
  "headers": {"authorization": "Bearer SERVER_PRIVATE_TEST_ACCOUNT_TOKEN"},
  "cookies": {"session": "SERVER_PRIVATE_TEST_SESSION"},
  "socketio_auth": {"token": "SERVER_PRIVATE_SOCKETIO_TEST_TOKEN"},
  "values": {"event_token": "SERVER_PRIVATE_EVENT_TOKEN"}
}
```

Only Authorization/X-API-Key/X-Auth-Token headers are allowed in the grant; it
cannot override Host or proxy routing. Case, exact origin and expiry are checked
by the host before mounting an ephemeral private copy read-only at
`/run/web-session.json`. Raw values never enter the Controller plan, CLI arguments
or artifact directory. The worker checks expiry; the host adapter removes the
temporary copy after confirmed container closure.
Provision and rotate real session files on
the server; never send their contents to either model or commit them.

## Delivery and acceptance

The new worker/dependencies are installed by the existing Ansible app/image
workflow. Apply the merged revision before claiming the new profiles available;
do not hot-edit server files or install dependencies directly on the host.
Use an idle-job maintenance window: the Ansible role now restarts the shared
service when its source changes, so the new contracts actually load. A restart
pauses campaigns and never automatically replays saved actions.
CI exercises real loopback WebSocket echo, Socket.IO websocket/polling/upgrade,
ack/heartbeat, JSON/CSRF/form/multipart workflows, positive and failed cleanup,
scope rejection and transcript secret-redaction with test-only credentials.
