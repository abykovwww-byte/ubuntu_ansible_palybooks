#!/bin/sh
# Read-only probe sent to the appliance over SSH stdin. No packages or files installed.
# Needs permission to read audit status and audit.log; missing fields fail audit preflight.
export LC_ALL=C
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
printf 'boot_id=%s\n' "$(cat /proc/sys/kernel/random/boot_id)"
awk '/^cpu / {printf "cpu_total=%.0f\ncpu_idle=%.0f\n", $2+$3+$4+$5+$6+$7+$8+$9, $5+$6}' /proc/stat
awk '/MemTotal:/ {t=$2} /MemAvailable:/ {a=$2} END {if(t>0) print "memory_used_pct=" 100*(t-a)/t}' /proc/meminfo
for point in / /var/log/audit; do
    if [ -d "$point" ]; then
        df -Pk "$point" | awk -v point="$point" 'NR==2 {gsub(/%/,"",$5); if(point=="/") print "root_used_pct=" $5; else {print "audit_disk_used_pct=" $5; print "audit_free_bytes=" $4*1024}}'
    fi
done
if command -v auditctl >/dev/null 2>&1; then
    auditctl -s 2>/dev/null | awk '$1 ~ /^(enabled|lost|backlog|backlog_limit)$/ {print "audit_" $1 "=" $2}'
fi
if [ -r /var/log/audit/audit.log ]; then
    stat -c 'log_inode=%i' /var/log/audit/audit.log
    stat -c 'log_bytes=%s' /var/log/audit/audit.log
    # Serial is a diagnostic estimate of event rate, not proof of complete delivery.
    tail -c 8192 /var/log/audit/audit.log | sed -n 's/.*msg=audit([^:]*:\([0-9]*\)).*/\1/p' | tail -n 1 | awk '{print "event_serial=" $1}'
fi
