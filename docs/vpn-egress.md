# Selective VPN egress on abykovserv

The `vpn_egress` role deploys a separate Xray client and a small controller. Only
explicitly configured consumers use its SOCKS5/HTTP proxy. The controller checks
HTTPS access to `api.telegram.org` through each candidate, keeping TLS verification
enabled. It accepts HTTP 200 or the API root's 302 without following the redirect.
This proves Bot API host connectivity, not authentication, message delivery,
MTProto access, or the geographic location advertised by a provider.

After two failed active-node probes, the client stops and the controller tests
alternatives, trying one candidate from each other declared country first. Four
candidates run concurrently. Each candidate has a 4-second startup bound and a
10-second request bound. A full 512-node failure scan can take tens of minutes;
the current scan progress is visible. Scans repeat after 30 seconds. Active-node
checks occur every 30 seconds. Existing connections close on a switch, and
consumers must reconnect. There is no direct outbound in generated client configs.

The supported subscription format is plain/base64 VLESS URI lists with WS + TLS
and UUID credentials. Unknown/insecure configurations are rejected. Subscriptions
refresh hourly over HTTPS; failures preserve the last successfully parsed pool.
The receipt date and refresh error remain visible. This is a direct bootstrap
request by the controller, not a client's data request. Nodes are deduplicated by
connection parameters; country flags are provider labels, not verified geolocation.

## Enable through Ansible

Keep the real subscription URL and credentials only in
`/etc/ansible/local-overrides.yml`. Do not paste them into Git or PRs.

```yaml
vpn_egress_enabled: true
vpn_egress_subscription_url: "<private HTTPS subscription URL>"
vpn_egress_proxy_user: "vpn-client"
vpn_egress_proxy_password: "<random password of at least 24 characters>"
# Optional: local LAN status page. Proxy listeners remain on host loopback.
vpn_egress_status_bind: "192.168.1.88"
```

Run the normal `sudo systemctl start ansible-local-apply.service`, or apply only
`playbooks/vpn-egress.yml` through `scripts/apply-local.sh`. Role defaults leave it
disabled until these private settings exist. The role does not change host routes,
firewall policy, Tailscale, or unrelated application's network configuration.

Runtime project: `/srv/apps/vpn-egress`. Private settings: server-local bind mount.
Cache and state: `/srv/app-data/vpn-egress/state` (UID 10001, directory 0700, files
0600). Xray transient config lives in a private tmpfs. Raw subscriptions, endpoints,
credentials, child output, and exceptions are not emitted to status or logs.

## Consumer contract

On Docker network `vpn_clients`, use `http://vpn-egress:8080` or
`socks5h://vpn-egress:1080`, with the private proxy username/password. This shared
network is internal. Attach a consumer only to it and other internal networks;
an additional ordinary bridge, host network, elevated network capabilities, or a
direct Internet route removes the consumer's network enforcement. Wire consumer
membership and explicit library proxy configuration in its own IaC deployment.
Proxy environment variables work only when the selected library honors them.

For a Windows consumer, forward server loopback ports through the existing SSH
access. For example, with the actual configured SSH identity:

```text
ssh -N -L 18080:127.0.0.1:18080 -L 18081:127.0.0.1:18081 abykov@192.168.1.88
```

Use the resulting local authenticated HTTP proxy in the bot's HTTP client. The
SSH tunnel encrypts the LAN connection; no proxy port is published to LAN/WAN.
Restart/reconnect handling belongs to the consumer deployment. A Windows proxy
setting alone does not enforce all Windows process traffic through this gateway.

## Status and delayed Telegram alerts

`http://127.0.0.1:18081/` is the status page; an explicitly selected LAN address
can publish only the redacted status. `/status.json` returns current state,
phase, country, subscription receipt/error, scan progress and the last 100 events.
`/healthz` returns 200 only for a recent healthy active-node probe, otherwise 503.
On complete scan failure, a stable `pool_down` event is persisted once per outage.
Recovery persists `pool_recovered` with the same `outage_id`. Restarts preserve
the unresolved outage and event IDs. Controller/file freshness is not absence
proof; a failed status request or an old timestamp means unknown status.

The existing bot runs in the separate `TG + JIRA updates` workflow on Windows,
where its token remains in Windows Credential Manager. This role does not access
that token or send Telegram messages. The bot's consumer integration must poll
`/status.json`, persist its last event cursor, and enqueue a short owner notification
using its existing durable queue. Use `vpn:<event.id>` as the stable queue ID and
deliver pending events after gateway recovery. Alert text should name outage start
and recovery time, without raw VPN URLs or bot credentials. Do not mark an event
delivered merely because it was read. Retain the Telegram API receipt/message_id.
If a consumer has missed more than 100 events, report a retention gap rather than
claim a complete notification history.

## Acceptance and rollback

CI validates generated configs with the pinned Xray core and a local synthetic
VLESS fixture, verifies HTTP proxy credentials and failure without direct fallback,
and checks subscription rejection, country order and durable event deduplication.
After apply, separately verify gateway `docker compose ps`, `/status.json`, actual
Bot API access through the consumer's proxy, and its message receipt. A green
container/HTTP test alone is not Telegram delivery. Prove consumer isolation with
a forced gateway outage and a denied direct request before treating it as enforced.

To stop the gateway explicitly, run `docker compose down` in its exact project.
This preserves private cache/events. Merely setting `vpn_egress_enabled: false`
skips future convergence and does not stop an already running container. Remove a
consumer's gateway dependency through its own IaC change; do not alter global routes.
