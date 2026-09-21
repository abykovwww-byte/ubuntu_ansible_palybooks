#!/bin/sh
# Only the dedicated ephemeral statistics files; never audit.log or vendor logs.
set -eu
mv -f /run/ngfw-audit-forwarder/metrics.jsonl /run/ngfw-audit-forwarder/metrics.jsonl.1
