"""Render only a dedicated job namespace table. Never alter Docker/host rules."""

import ipaddress
import math

from .contracts import Denied
from .http_policy import HTTPJobPolicy, Lease


def render_job_firewall(policy: HTTPJobPolicy, lease: Lease, now: float) -> str:
    if (lease.job_id, lease.generation, lease.scope_hash) != (policy.job_id, policy.generation, policy.scope_hash):
        raise Denied("Firewall lease identity mismatch")
    policy.check(now)
    seconds = min(lease.valid_until, policy.deadline) - now
    if not lease.revoked and not 0 < seconds <= 30:
        raise Denied("Invalid firewall lease")
    # Floor so firewall never grants longer access than the host lease.
    timeout = math.floor(seconds * 1000)
    workers = {4: [], 6: []}
    targets = {4: [], 6: []}
    if not lease.revoked:
        if timeout < 1:
            raise Denied("Lease is too short to install")
        for v in policy.worker_addresses:
            ip = ipaddress.ip_address(v)
            workers[ip.version].append(f"{ip} timeout {timeout}ms")
        for u in policy.upstreams:
            for v in u.addresses:
                ip = ipaddress.ip_address(v)
                targets[ip.version].append(f"{ip} . {u.port} timeout {timeout}ms")
    lines = ["table inet sl_job {"]
    for version in (4, 6):
        for name, values, typ in ((f"worker{version}", workers[version], f"ipv{version}_addr"),
                                  (f"upstream{version}", targets[version], f"ipv{version}_addr . inet_service")):
            elements = " elements = { " + ", ".join(sorted(set(values))) + " };" if values else ""
            lines.append(f" set {name} {{ type {typ}; flags timeout;{elements} }}")
    lines += [
        " chain input { type filter hook input priority 0; policy drop;",
        '  iifname "lo" accept',
        "  ip saddr @worker4 tcp dport 8080 accept",
        "  ip6 saddr @worker6 tcp dport 8080 accept",
        "  ip saddr . tcp sport @upstream4 ct state established accept",
        "  ip6 saddr . tcp sport @upstream6 ct state established accept",
        # Needed for IPv6 neighbor discovery, only link-local ICMP (no data egress).
        "  icmpv6 type { nd-neighbor-solicit, nd-neighbor-advert, nd-router-solicit, nd-router-advert } accept",
        "  counter drop", " }",
        " chain output { type filter hook output priority 0; policy drop;",
        '  oifname "lo" accept',
        "  ip daddr @worker4 tcp sport 8080 accept",
        "  ip6 daddr @worker6 tcp sport 8080 accept",
        "  ip daddr . tcp dport @upstream4 accept",
        "  ip6 daddr . tcp dport @upstream6 accept",
        "  icmpv6 type { nd-neighbor-solicit, nd-neighbor-advert, nd-router-solicit, nd-router-advert } accept",
        "  counter drop", " }",
        " chain forward { type filter hook forward priority 0; policy drop; counter drop; }", "}",
    ]
    return "\n".join(lines) + "\n"


def revoke_job_firewall() -> str:
    # Existing TCP packets also need membership: no blanket established accept.
    return "\n".join(f"flush set inet sl_job {name}{v}" for v in (4, 6) for name in ("worker", "upstream")) + "\n"


def render_worker_firewall(proxy_address: str) -> str:
    """Bootstrap inside the worker namespace before dropping all capabilities.

    No loopback allow: Docker's embedded DNS must not bypass the selected job
    resolver. HTTP workers need no DNS, since the trusted proxy owns resolution.
    Gateway lease is the authority and still covers established connections.
    """
    address = ipaddress.ip_address(proxy_address)
    family = "ip" if address.version == 4 else "ip6"
    ndp = "icmpv6 type { nd-neighbor-solicit, nd-neighbor-advert } accept"
    return f"""table inet sl_worker {{
 chain output {{ type filter hook output priority 0; policy drop;
  {family} daddr {address} tcp dport 8080 accept
  {ndp}
  counter drop
 }}
 chain input {{ type filter hook input priority 0; policy drop;
  {family} saddr {address} tcp sport 8080 ct state established accept
  {ndp}
  counter drop
 }}
 chain forward {{ type filter hook forward priority 0; policy drop; }}
}}
"""
