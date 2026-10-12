# Native metrics: operational gates and evidence

Acceptance contract: [ТЗ](ngfw-native-metrics-TZ.md).
Analysis: [native metrics analysis](ngfw-native-metrics-analysis.md).

This delivery prepares opt-in storage/export, native source discovery, explicit
extreme workload commands, a guarded single-window controller and raw-result
normalization/analysis. It does **not** certify live native
collection, A/B results, capacity, observer impact or appliance compatibility.
An unset compatibility pin prevents deployment. Raw laboratory receipts stay
outside this public repository.

## Discovery and remaining gaps on 2026-10-12

Guest SSH and sudo discovery succeeded using explicitly authorized existing
credentials. No authentication bypass or new access was provisioned. Credentials
must not enter stdout, evidence files, process arguments, environment or Git.
Use the existing authorized authentication channel; interactive prompts below
are the manual operator path, not an additional requirement for an established
authorized session.

The guest has Telegraf 1.33.0 with a 10-second interval and an Influx v1 output
pointing to an unavailable VM-local destination. Actual vendor inputs and native
points have been captured. The PT Storage link referenced by the guide is no
longer available, so official dashboard formulas and the supplied database image
remain unverified. The external metrics endpoint has not been supplied. Local
compatibility/replay fixtures do not establish remote delivery or continuous
source collection.

## Read-only guest discovery after merged revision is available

On abykovserv, use the repository's deployed revision after normal interactive
host apply, or its documented ordinary checkout of the exact merged commit.
Do not run the whole guest playbook as host root:

```bash
cd /opt/ubuntu_ansible_palybooks
.venv/bin/ansible-playbook -i inventories/ngfw-audit-source/hosts.yml \
  playbooks/ngfw-native-discovery.yml --ask-pass --ask-become-pass
```

The playbook reads version/service/config metadata and hashes, native plugin
names and intervals, OS counters, AuditD state, and package state. It creates
only controller evidence plus Ansible's temporary execution file. It does not
edit Telegraf, AuditD, policy, EDR, routing or packages. Output is written to
`/srv/app-data/ngfw-lab/reports/native-metrics-discovery/native-source-<capture-epoch>.json`.
Each capture has a separate filename; a later profile cannot replace its baseline.
Missing native fields stay unknown. Plugin names alone are not emitted metrics.

## Storage/source integration gate

1. Obtain actual discovery and official JSON/image metadata where available.
   Select and test an exact InfluxDB digest against actual emitted types and
   HTTP transport; do not substitute `--test` display for the output serializer.
   The current pinned1.8.10 passed a bounded157-row/126-point installed-serializer
   fixture with exact read-back and outage/backup/restore/replay. Its official
   PT distribution provenance is not established. Never infer a latest version.
2. Confirm source interval and outputs, capture native metrics with original
   timestamps, and measure peak bytes/s. Provision raw 30d and archive 90d;
   calculate >=24h spool at peak with 30% reserve and database overhead.
3. The server inventory enables the tested local receiver profile with a
   restricted lab listener and source ACL. Its128GiB payload/32GiB reserve is
   bootstrap provisioning from a bounded sample, not peak/retention acceptance.
   Private overrides remain available; external endpoint stays empty until
   supplied. Secrets never enter Git.
4. Run normal Git/PR/CI/merge and interactive host Ansible apply. Source routing
   must be an independently reviewable guest Ansible change based on the exact
   discovered config hash; this initial discovery role does not rewrite it.
5. Verify native local read-back and exact replay against a disposable temporary
   destination. Exercise source/local DB/remote outage separately; distinguish
   memory buffering from durable accepted spool batches. Backup/restore and
   re-run the report. Establish actual observer impact before frequency changes.

## Opt-in guest source routing

The separate [routing role](../roles/ngfw_native_route/README.md) is disabled by
default and is not included in the host/site playbook. It changes only the
single approved Telegraf output URL; all other configuration bytes, credentials,
official inputs and collection intervals remain intact. The endpoint is fixed
to the isolated lab receiver. It does not enable that receiver, choose resource
budgets, change authentication or provision an external destination.

After normal Git/PR/CI/merge delivery and the authorized host receiver apply,
obtain a fresh sanitized source discovery and its exact full-file SHA256. On
the host's deployed checkout, prepare the guest change using existing interactive
SSH/sudo authorization:

```bash
.venv/bin/ansible-playbook -i inventories/ngfw-audit-source/hosts.yml \
  playbooks/ngfw-native-route.yml --ask-pass --ask-become-pass \
  -e ngfw_native_route_enabled=true -e ngfw_native_route_action=plan \
  -e ngfw_native_route_expected_sha256=<exact-current-64-hex>
```

Review the safe plan, then use the same explicit command with action `apply`.
Use `readback` for hash/metadata/service observations and `restore` for the exact
retained original configuration. No private inventory values or credentials
belong in the command's extra vars, chat, Git or published evidence. A historical
hash is not an automatic default. The helper refuses a mismatch, extra influxdb
outputs, unrelated edits or an unfinished prior transaction.

Plan and apply test the installed Telegraf configuration and the destination
`/ping` before mutation. Telegraf `--test` gathers inputs once, so schedule these
operations outside measured windows. Apply restarts the service only when the
URL changes. Explicit restore always reloads the original configuration, including
recovery after an interrupted file restore. The guest-local private backup preserves prior bytes, uid/gid/mode;
failed apply automatically restores and checks them, or reports rollback as
unconfirmed. Neither full configuration nor credentials are shown in diff/log.

Follow routing with local DB read-back of **new** native point
timestamps, fields and tags, plus Telegraf error checks. HTTP204, unchanged
interval and active service do not establish successful writes, complete
collection, external delivery or A/B validity. This procedure describes the
prepared delivery mechanism; it is not a claim that routing has been applied.

## Workload integration gate

The extreme planner and client are tools, not an unattended privileged profile
switcher. The leading operator must preserve a single experiment lock, guest
health/AuditD monitoring, isolation negative check, powered-off A/B snapshots,
read-back and rollback. Do not launch a stress window before these receipts.
The current legacy endpoint limits do not support the requested upper session
targets. Provision FD, source ports/addresses, server capacity and resources
through IaC, then calibrate a separate control path. Preserve endpoint identity
and matched rule evidence. A runnable load CLI is not proof of generator capacity.

The exact plan includes three comparison blocks per workload, identical offered
load per block, return controls, 300-second measurement and >=120-second recovery,
and 3600-second endurance after per-profile stable limits are measured. CPU100%
alone is not a stop. Management loss, reboot, OOM, disk or mandatory audit loss
stop the load. Save failed windows before cleanup and recovery.

The [controller contract](../roles/ngfw_lab/files/extreme/README.md) requires
hashed, scoped prerequisite artifacts, current snapshot read-back, direct-path
capacity evidence, and fresh identity/native-health producers. Those producers
still require the actual guest and native database discovery. Missing evidence
blocks execution. The controller uses the existing host lock, stops every
launched remote job even after a local exec failure, and preserves failed raw
output before recovery. Its completed process is not a validated A/B result.

The normalizer consumes controller receipts and actual workload outputs. Only
explicit measurement-phase components enter analysis; warmup is retained
separately. Missing or stopped windows remain invalid. Native completeness must
come from full-window database read-back, never from the controller sample rate.

## Delivery and resumption

The deployment skill requires the operator to run host apply interactively:

```bash
sudo systemctl start ansible-local-apply.service
```

The current server inventory enables the local receiver; this command does not
run the separate guest routing playbook or create a credential channel.
Keep local validation, CI, merge, apply,
live collection, replay and performance evidence separate. The full experiment
remains incomplete until the acceptance contract is met.
