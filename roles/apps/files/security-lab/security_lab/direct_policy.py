"""Host-local direct curl compilation and dedicated worker netns nft scripts.

These functions neither execute commands nor grant production qualification.
Policy pins and leases come from the host controller, never model tool inputs.
The launcher applies each nft script atomically inside one disposable worker
network namespace, with no competing tables granting access. Namespace setup,
process lifetime, monotonic lease sequencing and receiving-side qualification
remain launcher/controller responsibilities. No host-global rules are flushed.
"""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import math
from urllib.parse import urlsplit

from .contracts import Denied, digest, hostname
from .http_policy import HTTPJobPolicy, Lease
from .tool_catalog import CurlCheck, ToolPlan, build_check


@dataclass(frozen=True)
class DirectExecution:
    argv: tuple[str, ...]
    destination: str
    port: int
    hostname: str
    policy_hash: str


def _validated(policy: HTTPJobPolicy, lease: Lease, now: float) -> HTTPJobPolicy:
    try:
        if not isinstance(policy, HTTPJobPolicy) or not isinstance(lease, Lease):
            raise ValueError("Host-owned policy and lease required")
        # model_copy/model_construct and nested mutable lists must not bypass
        # policy validators at this execution boundary.
        policy = HTTPJobPolicy.model_validate(policy.model_dump(mode="python"))
        lease = Lease.model_validate(lease.model_dump(mode="python"))
        if isinstance(now, bool) or not math.isfinite(now):
            raise ValueError("Finite host time required")
        if not math.isfinite(policy.deadline) or not math.isfinite(lease.valid_until):
            raise ValueError("Finite policy/lease window required")
        policy.check(now)
        if (lease.job_id, lease.generation, lease.scope_hash) != (
                policy.job_id, policy.generation, policy.scope_hash):
            raise ValueError("Lease binding differs from policy")
        if lease.revoked or not 0 < lease.valid_until - now <= 30:
            raise ValueError("Lease revoked, expired or exceeds thirty seconds")
        if lease.valid_until > policy.deadline:
            raise ValueError("Lease exceeds job deadline")
        return policy
    except (ValueError, TypeError, AttributeError) as exc:
        raise Denied("Invalid direct policy, binding or lease window") from exc


def _selected(policy: HTTPJobPolicy, host: str, port: int) -> str:
    return min(policy.upstream(host, port).addresses,
               key=lambda value: (ipaddress.ip_address(value).version,
                                  int(ipaddress.ip_address(value))))


def compile_direct_execution(plan: ToolPlan, policy: HTTPJobPolicy, lease: Lease,
                             *, now: float) -> DirectExecution:
    """Accept only a complete canonical curl plan and pin its sole service.

    --resolve preserves the URL hostname for Host, SNI and certificate checks.
    It supplies one literal address, preventing DNS or alternate address retry.
    The typed plan remains an unqualified preview; only the host can launch it.
    """
    policy = _validated(policy, lease, now)
    try:
        if not isinstance(plan, ToolPlan):
            raise ValueError("ToolPlan required")
        plan = ToolPlan.model_validate(plan.model_dump(mode="python"))
        if plan.tool != "curl" or plan.profile != "target_http" or plan.argv[-2:-1] != ("--url",):
            raise ValueError("One canonical curl URL required")
        method = "HEAD" if "--head" in plan.argv else "GET"
        spec = CurlCheck(url=plan.argv[-1], method=method, seconds=plan.seconds)
        # Controller reserves this exact duration when it fixes job deadline.
        # Setup consumes that reservation; revalidating the original plan at
        # the later launch time would reject every otherwise valid real job.
        reserved_start = policy.deadline - plan.seconds
        if not math.isfinite(reserved_start) or now < reserved_start:
            raise ValueError("Execution predates the job reservation")
        canonical = build_check(spec, policy.scope, stage="checks", now=reserved_start)
        if plan != canonical:
            raise ValueError("Plan differs from canonical typed curl arguments")
        milliseconds = min(plan.seconds * 1000, math.floor((policy.deadline - now) * 1000))
        if milliseconds < 1:
            raise ValueError("No usable job execution time remains")
        effective_seconds = f"{milliseconds / 1000:.3f}".rstrip("0").rstrip(".")
        parsed = urlsplit(spec.url)
        host, port = parsed.hostname, parsed.port
        address = _selected(policy, host, port)
        ip = ipaddress.ip_address(address)
        resolve = f"{host}:{port}:{'[' + address + ']' if ip.version == 6 else address}"
        bounded = list(plan.argv)
        bounded[bounded.index("--max-time") + 1] = effective_seconds
        argv = tuple(bounded[:-2]) + ("--resolve", resolve, "--ipv4" if ip.version == 4 else "--ipv6") + tuple(bounded[-2:])
        return DirectExecution(argv, address, port, host, digest(policy.model_dump(mode="json")))
    except (ValueError, TypeError, IndexError, AttributeError) as exc:
        raise Denied("Direct execution requires an exact canonical typed curl plan") from exc


def _execution_address(execution: DirectExecution):
    try:
        if not isinstance(execution, DirectExecution) or type(execution.port) is not int or not 1 <= execution.port <= 65535:
            raise ValueError("Direct execution endpoint required")
        if hostname(execution.hostname) != execution.hostname:
            raise ValueError("Canonical hostname required")
        address = ipaddress.ip_address(execution.destination)
        if str(address) != execution.destination or address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast:
            raise ValueError("Canonical safe literal required")
        return address
    except (ValueError, TypeError, AttributeError) as exc:
        raise Denied("Invalid direct execution endpoint") from exc


def nft_initial_rules(execution: DirectExecution) -> str:
    """Create empty timed sets and drop-default hooks in the worker netns.

    Replies are allowed only while their exact source IP/TCP port remains in
    the same expiring membership used for outgoing requests. Conntrack alone
    never extends access past expiry. IPv6 requires only local NDP messages;
    router discovery, ICMP echo, DNS and loopback traffic remain blocked.
    """
    address = _execution_address(execution)
    ndp_input = ndp_output = ""
    if address.version == 6:
        ndp_input = "        meta l4proto ipv6-icmp ip6 hoplimit 255 icmpv6 type { nd-neighbor-solicit, nd-neighbor-advert } accept\n"
        ndp_output = ndp_input
    return (
        "add table inet security_lab_direct\n"
        "table inet security_lab_direct {\n"
        "    set allowed4 { type ipv4_addr . inet_service; flags timeout; }\n"
        "    set allowed6 { type ipv6_addr . inet_service; flags timeout; }\n"
        "    chain input {\n"
        "        type filter hook input priority 0; policy drop;\n"
        + ndp_input +
        "        ct state established ip saddr . tcp sport @allowed4 accept\n"
        "        ct state established ip6 saddr . tcp sport @allowed6 accept\n"
        "    }\n"
        "    chain output {\n"
        "        type filter hook output priority 0; policy drop;\n"
        + ndp_output +
        "        ct state { new, established } ip daddr . tcp dport @allowed4 accept\n"
        "        ct state { new, established } ip6 daddr . tcp dport @allowed6 accept\n"
        "    }\n"
        "    chain forward { type filter hook forward priority 0; policy drop; }\n"
        "}\n"
    )


def nft_revoke_rules() -> str:
    """Empty both sets atomically; established traffic loses membership too."""
    return ("flush set inet security_lab_direct allowed4\n"
            "flush set inet security_lab_direct allowed6\n")


def nft_lease_rules(execution: DirectExecution, policy: HTTPJobPolicy, lease: Lease,
                    *, now: float) -> str:
    """Replace sole allowed endpoint with a bounded kernel expiry.

    The caller validates monotonic lease sequence with Controller before this
    stateless renderer. Replaying a receipt never creates fresh expiration:
    timeout is recomputed from its absolute host deadline and rounded down.
    """
    policy = _validated(policy, lease, now)
    address = _execution_address(execution)
    if (execution.policy_hash != digest(policy.model_dump(mode="json"))
            or execution.destination != _selected(policy, execution.hostname, execution.port)):
        raise Denied("Direct execution differs from current bound policy")
    milliseconds = math.floor((lease.valid_until - now) * 1000)
    if milliseconds < 1:
        raise Denied("No usable lease time remains")
    name = "allowed4" if address.version == 4 else "allowed6"
    return (nft_revoke_rules()
            + f"add element inet security_lab_direct {name} {{ {address} . {execution.port} timeout {milliseconds}ms }}\n")
