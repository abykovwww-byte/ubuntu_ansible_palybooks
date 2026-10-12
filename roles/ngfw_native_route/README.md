# Opt-in routing of the existing native Telegraf output

This role applies only through the dedicated Ansible playbook to the isolated
`ngfw_audit_source` inventory group. It is **disabled by default** and defaults
to the read-only `plan` action when explicitly enabled. No `site.yml` or server
configuration includes it automatically.

The one permitted content change is the literal URL of the single existing
`[[outputs.influxdb]]` in `/etc/telegraf/telegraf.conf`:

```text
http://127.0.0.1:8086 -> http://10.77.0.1:18086
```

Both source and destination are fixed allowlists. The role refuses other URLs,
multiple `outputs.influxdb`, additional influxdb outputs in included `*.conf`,
ambiguous syntax, unrelated edits, a stale/missing expected SHA256 or an
incomplete earlier transaction. It parses the complete TOML before and after
and requires semantic identity except for this one URL list. The actual edit
replaces only the URL's byte span: comments, line endings, credentials, database,
skip-database-creation setting, interval, inputs and all remaining bytes survive.
It does not export or print the configuration or any authentication values.

## Preparation and operator gate

1. Deploy the compatible local receiver and confirm its `/ping` on the allowed
   management address/port. The route role does not provision that server.
2. Obtain a fresh sanitized discovery receipt and the exact full source-file
   SHA256. Historical `6fb60be5...` from2026-10-12 is context, not an automatic
   default or a waiver of the on-guest hash check.
3. Supply private inventory/connection parameters using the existing authorized
   mechanism. This role neither reads nor stores SSH/sudo credentials.
4. Use the normal branch -> PR -> green CI -> merge -> Ansible delivery. Remote
   interactive privilege escalation remains the established operator gate.

Example using a private inventory whose values are not committed:

```text
ansible-playbook -i <private-lab-inventory> playbooks/ngfw-native-route.yml --ask-become-pass -e ngfw_native_route_enabled=true -e ngfw_native_route_action=plan -e ngfw_native_route_expected_sha256=<exact-current-64-hex>
```

After reviewing the safe plan, the same command with `action=apply` is the
explicit mutation. `action=readback` verifies the source/candidate hash relationship
and, when routed, the retained backup metadata/status. `action=restore` restores
the exact approved prior bytes, mode, uid and gid. `--check` maps an apply to
plan and skips restore. Ansible transfers the helper as a private temporary
script; no persistent helper or credentials need to be installed.

## Checks, restart and rollback

The source must already have active Telegraf. Before apply, the helper verifies
the mandatory expected SHA256, calls the installed binary against the original
and staged candidate, and requires direct HTTP204 from the allowlisted `/ping`
(no proxy and no redirects). The validation uses Telegraf1.33
`--test --input-filter system --quiet` with a 45-second process timeout. It requires
`inputs.system` in the main file or a loaded `.conf` include, and at least one
emitted `> system` metric row; an empty probe cannot pass. This is a limited
configuration/input smoke test, not validation of every native input. It avoids
rerunning vendor `inputs.exec` alongside the active service. Repeated full tests
produced exec timeouts during diagnosis while the real service showed no exec
errors; this does not establish that `--test-wait 1` caused those timeouts.
Test mode does not run outputs. Schedule route transactions outside measured
windows. Captured stdout is checked only in memory and never returned or saved;
stderr is discarded and failures expose only fixed diagnostic codes.
After apply, independently verify fresh local DB points for every expected native
measurement family and check the real service journal. Neither this filtered
preflight nor `/ping` establishes complete collection or successful metric writes.

The helper rechecks bytes and metadata after validation and again immediately
before atomic apply. It keeps a durable guest-private0700 backup directory with
0600 original bytes and transaction metadata. The metadata contains the exact
original hash, resulting hash, original uid/gid/mode and prior active state.
The backup is never transferred to the controller, printed or included in a
public report. Backup/state and replacement files use flush/fsync and atomic
rename. The resulting authoritative configuration retains the original owner,
group and mode. Symlink or hardlinked source files are rejected.

During apply, only an actual URL change triggers a Telegraf restart. Apply then verifies
active service, destination ping, exact resulting file hash and original file
metadata. Failure restores the original bytes/metadata, restarts the original
service configuration and verifies restoration. The safe failure code is either
`apply_failed_original_restored` or `apply_failed_and_rollback_unconfirmed`.
An unexpected concurrent third-party edit is never overwritten by rollback.
After an interrupted PREPARED transaction, explicit restore is required; an
already-routed file is not silently called applied or restarted.

An identical second apply verifies the inverse transformation's source SHA256
and the APPLIED backup state, reports UNCHANGED and does not restart. Explicit
restore always restarts Telegraf after checking/writing the original bytes:
an active process can still hold the routed configuration after an earlier
interruption between restoring the file and restarting the service. Restore
also rechecks bytes/metadata immediately before writing and refuses an unrelated
current hash or concurrent edit. All Ansible execution output is
`no_log` with `diff:false`; the normal visible summary contains only the fixed
safe status. Failed helper results contain only fixed error codes and an
unknown `changed` value, never an invented assertion that nothing changed.

## Acceptance boundary

HTTP204 and an active Telegraf process prove destination reachability and local
service state. They do not prove successful write or actual native
points in the local database. The follow-up read-back must verify new native
measurements/timestamps and collection errors using the existing configured
credentials locally. This role deliberately leaves those credentials, official
inputs and10s interval intact. No external export acceptance follows from this
source routing change.

Focused tests:

```text
python -m unittest discover -s roles/ngfw_native_route/tests -p test_native_route.py -v
```

Windows tests exercise exact byte transformation, hash/multiple-output/destination
gates, plan/apply/idempotency, concurrent-edit rejection and rollback logic.
The POSIX uid/gid/fsync/rename test runs on Linux and is explicitly skipped on
Windows. No local test result is evidence of guest deployment or native write.
