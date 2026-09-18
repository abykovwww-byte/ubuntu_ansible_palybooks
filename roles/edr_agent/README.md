# MaxPatrol EDR agent

This role installs the pinned Linux MaxPatrol EDR agent package on a Debian-family
host and connects it to the configured agent server. The distribution archive is
downloaded over verified TLS with a role-local trust anchor and is independently
checked against the configured SHA-256 digest. The CA is not added to the host's
global trust store.

The abykovserv inventory points the agent to `xdr-ext.ptsecurity.ru:8443`, the
external server designated for personal equipment by the SOCIT `Установка EDR`
page. The role enables and starts `vxagent.service`; central authorization,
policy assignment, and proof that events reached EDR remain live acceptance
checks after the Ansible apply.

Useful read-only checks on the host:

```bash
systemctl status vxagent.service --no-pager
/opt/vxagent/bin/vxagent -version
grep '^CONNECT=' /opt/vxagent/service.env
tail -n 100 /opt/vxagent/logs/agent.log
```
