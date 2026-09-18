# MaxPatrol EDR agent

This role installs or removes the pinned Linux MaxPatrol EDR agent package on a
Debian-family host. Set `edr_agent_state` to `present` or `absent`. Installation
connects the agent to the configured server. The distribution archive is
downloaded over verified TLS with a role-local trust anchor and is independently
checked against the configured SHA-256 digest. The CA is not added to the host's
global trust store.

The abykovserv inventory sets `edr_agent_state: absent` because the EDR target is
the disposable `pt-ngfw-auditd` guest, not its hypervisor. Removal purges the
package and deletes `/opt/vxagent` plus the role download cache. The stale host
record in the central console, if present, is not deleted by this role.

Useful read-only checks on the host:

```bash
systemctl status vxagent.service --no-pager
/opt/vxagent/bin/vxagent -version
grep '^CONNECT=' /opt/vxagent/service.env
tail -n 100 /opt/vxagent/logs/agent.log
```
