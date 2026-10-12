# Local validation receipt, 2026-10-12

Scope: `roles/ngfw_metrics` only; independent collection/export subtask.
Host: Windows workstation, bundled Python 3.12.13, writable role-local TEMP.

The focused unittest suite passed 16 tests in 2.481 seconds including the final
bounded-ingress and partial-readback changes. It includes actual temporary HTTP sockets bound only to
127.0.0.1, source allowlist denial, offline/reconnect and byte readback, independent
local/external cursors, lost acknowledgement, child-process `os._exit(17)` after
durable commit, quota refusal, backup/restore/tamper refusal, pending-retention
protection, raw archive export, nanosecond/tag/field comparison and rejection of
matching but explicitly partial query receipts.

The initial Windows system-TEMP attempt failed with inaccessible SQLite temp
files; using the documented bundled interpreter and a workspace TEMP resolved
that issue. The first loopback HTTP attempt was denied by sandbox WinError 10013.
A narrow elevated rerun for local temporary sockets was approved and passed.

`git diff --check -- roles/ngfw_metrics` returned zero. These files are new, so
that command is not a substitute for full tracked-diff validation after staging.
The bundled interpreter lacks `yaml` and `jinja2`; Ansible syntax/template checks
were not performed by this subtask. Integration owns their CI verification.

No Git commit/push, CI, Ansible apply, remote SSH, actual InfluxDB write, actual
Telegraf routing, external endpoint delivery or live NGFW load was performed.
The temporary endpoint is deliberately not labelled as external acceptance.
