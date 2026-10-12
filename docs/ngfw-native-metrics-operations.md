# Native metrics: operational gates and evidence

Acceptance contract: [ТЗ](ngfw-native-metrics-TZ.md).
Analysis: [native metrics analysis](ngfw-native-metrics-analysis.md).

This delivery prepares opt-in storage/export, native source discovery, explicit
extreme workload commands, a guarded single-window controller and raw-result
normalization/analysis. It does **not** certify live native
collection, A/B results, capacity, observer impact or appliance compatibility.
An unset compatibility pin prevents deployment. Raw laboratory receipts stay
outside this public repository.

## Verified impediments on 2026-10-12

The existing guest SSH rejects batch authentication. The former ephemeral broker
is gone. Do not use hypervisor/disk access to bypass guest authentication.
The operator must authenticate to guest SSH/sudo in their terminal; passwords
must not enter chat, evidence files or Git. The PT Storage link referenced by
the guide says the link does not exist, so native dashboard definitions and the
bundled InfluxDB version are unavailable. The user confirmed there is no remote
metrics endpoint yet. Local replay tests are not remote delivery acceptance.

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

1. Obtain actual discovery and official JSON/image metadata. Select a compatible
   exact InfluxDB image digest from that evidence; never infer a latest version.
2. Confirm source interval and outputs, capture native metrics with original
   timestamps, and measure peak bytes/s. Provision raw 30d and archive 90d;
   calculate >=24h spool at peak with 30% reserve and database overhead.
3. Configure the opt-in role using server-private overrides, with local listener
   on the lab management interface, source ACL, and bounded resource budgets.
   External endpoint stays empty until supplied. Secrets never enter Git.
4. Run normal Git/PR/CI/merge and interactive host Ansible apply. Source routing
   must be an independently reviewable guest Ansible change based on the exact
   discovered config hash; this initial discovery role does not rewrite it.
5. Verify native local read-back and exact replay against a disposable temporary
   destination. Exercise source/local DB/remote outage separately; distinguish
   memory buffering from durable accepted spool batches. Backup/restore and
   re-run the report. Establish actual observer impact before frequency changes.

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

This command alone will not enable an unset metrics role or supply missing guest
authentication/compatibility data. Keep local validation, CI, merge, apply,
live collection, replay and performance evidence separate. The full experiment
remains incomplete until the acceptance contract is met.
