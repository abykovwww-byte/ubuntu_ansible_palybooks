"""Strict check plans, never shell commands or production qualification.

The launcher supplies executable/image policy. Model inputs cannot supply
flags, environments, image names, files or arbitrary code. HTTP plans describe
direct requests with standard TLS verification; execution remains unqualified.
TCP scan permission is declared separately in Scope.network_targets. Compilation
does not qualify a runner, deploy an image or authorize production execution.
"""
from __future__ import annotations

import ipaddress
import math
from typing import Annotated, Literal, Union
from urllib.parse import urlsplit

from pydantic import Field, TypeAdapter, field_validator

from .contracts import Contract, Denied, Scope, hostname, is_private_network_address, network_address, safe_path

MAX_OUTPUT_BYTES = 1_000_000
CHECK_STAGES = {"scope_approved", "checking", "checks_review", "checks"}


class CurlCheck(Contract):
    tool: Literal["curl"] = "curl"
    url: str = Field(min_length=1, max_length=2000)
    method: Literal["GET", "HEAD"] = "HEAD"
    seconds: int = Field(default=15, ge=1, le=60)

    @field_validator("url")
    @classmethod
    def public_navigation_url(cls, value: str) -> str:
        if any(ord(char) <= 32 or ord(char) >= 127 for char in value) or any(char in value for char in "\\%;?#"):
            raise ValueError("Queries, encodings, fragments and ambiguous URL characters are unsupported")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Expected an HTTP service URL without credentials")
        host = hostname(parsed.hostname)
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        if not 1 <= port <= 65535:
            raise ValueError("Invalid service port")
        path = safe_path(parsed.path or "/")
        # Canonical spelling is built here; one exact URL is passed with globbing
        # disabled. No user supplied request-target/headers or redirects.
        return f"{parsed.scheme}://{host}:{port}{path}"


class NmapCheck(Contract):
    tool: Literal["nmap"] = "nmap"
    host: str
    addresses: list[str] = Field(min_length=1, max_length=8)
    ports: list[int] = Field(min_length=1, max_length=16)
    seconds: int = Field(default=30, ge=1, le=60)

    _host = field_validator("host")(hostname)

    @field_validator("addresses")
    @classmethod
    def explicit_unicast_addresses(cls, values: list[str]) -> list[str]:
        normalized = [network_address(value) for value in values]
        if len({ipaddress.ip_address(value).version for value in normalized}) != 1:
            raise ValueError("IPv4 and IPv6 require separate bounded plans")
        return sorted(set(normalized))

    @field_validator("ports")
    @classmethod
    def explicit_ports(cls, values: list[int]) -> list[int]:
        if any(type(value) is not int or not 1 <= value <= 65535 for value in values):
            raise ValueError("Expected explicit integer TCP ports")
        return sorted(set(values))


CheckSpec = Annotated[Union[CurlCheck, NmapCheck], Field(discriminator="tool")]
_CHECK_ADAPTER = TypeAdapter(CheckSpec)


def parse_check(value: dict) -> CurlCheck | NmapCheck:
    return _CHECK_ADAPTER.validate_python(value, strict=True)


class ToolPlan(Contract):
    tool: Literal["curl", "nmap"]
    profile: Literal["target_http", "target_network"]
    argv: tuple[str, ...]
    seconds: int
    max_output_bytes: int = MAX_OUTPUT_BYTES
    qualification: Literal["pending_image_and_receiving_side_checks", "pending_nmap_runner_and_receiving_side_checks"]
    execution_authorized: Literal[False] = False
    # No production image/version is invented or selected from model input.
    image_digest: None = None
    tool_version: None = None


def _check_stage(scope: Scope, stage: str, now: float, seconds: int):
    if stage not in CHECK_STAGES:
        raise Denied("Tool checks require an activated checks stage")
    scope.check_window(now)
    if scope.allowed_actions != ["checks"] or "checks" not in scope.authorization.actions:
        raise Denied("Checks are not authorized")
    if seconds > scope.budget.seconds or now + seconds > min(scope.expires_at, scope.authorization.valid_until):
        raise Denied("Check duration exceeds scope or authorization budget")


def build_check(spec: CurlCheck | NmapCheck | dict, scope: Scope, *, stage: str, now: float,
                pinned_addresses: dict[str, list[str]] | None = None) -> ToolPlan:
    """Compile only. Stage and scope originate in Controller.

    Returned direct curl plan still needs a qualified worker image, isolated
    execution and receiving-side checks. Compilation is not an execution grant.
    Nmap uses independent TCP permissions and host-owned exact address pins;
    HTTP permissions alone never authorize a network scan.
    """
    if not isinstance(spec, (CurlCheck, NmapCheck)):
        if not isinstance(spec, dict):
            raise Denied("Expected a strict typed check")
    # Pydantic model_copy/model_construct and mutable nested lists do not run
    # validation. Revalidate plain data at this host-owned compilation boundary.
    spec = parse_check(spec if isinstance(spec, dict) else spec.model_dump(mode="python", warnings=False))
    scope = Scope.model_validate(scope.model_dump(mode="python", warnings=False), strict=True)
    _check_stage(scope, stage, now, spec.seconds)
    if isinstance(spec, NmapCheck):
        return _build_nmap(spec, scope, pinned_addresses)
    if not any(target.permits(spec.url, spec.method) for target in scope.targets):
        raise Denied("HTTP check outside approved host, service, path or method")
    argv = ["/usr/bin/curl", "--disable", "--globoff", "--silent", "--show-error", "--http1.1",
            "--noproxy", "*",
            "--proto", "=" + urlsplit(spec.url).scheme, "--max-redirs", "0", "--retry", "0",
            "--connect-timeout", str(min(5, spec.seconds)), "--max-time", str(spec.seconds),
            "--max-filesize", str(MAX_OUTPUT_BYTES)]
    argv += ["--head"] if spec.method == "HEAD" else ["--request", "GET", "--include"]
    argv += ["--url", spec.url]
    return ToolPlan(tool="curl", profile="target_http", argv=tuple(argv), seconds=spec.seconds,
                    qualification="pending_image_and_receiving_side_checks")


def _build_nmap(spec: NmapCheck, scope: Scope,
                pinned_addresses: dict[str, list[str]] | None) -> ToolPlan:
    target = next((target for target in scope.network_targets if target.host == spec.host), None)
    if target is None:
        raise Denied("Nmap requires an explicit canonical network scope")
    if not set(spec.ports) <= set(target.tcp_ports):
        raise Denied("Nmap exceeds explicitly approved TCP ports")
    grants = set(target.addresses)
    if grants and not set(spec.addresses) <= grants:
        raise Denied("Nmap exceeds explicitly approved address grants")
    if any(is_private_network_address(value) and value not in grants for value in spec.addresses):
        raise Denied("Private Nmap addresses require explicit approved address grants")
    if not isinstance(pinned_addresses, dict) or spec.host not in pinned_addresses:
        raise Denied("Nmap requires host-owned address pins")
    try:
        values = pinned_addresses[spec.host]
        if not isinstance(values, list) or not values or any(type(value) is not str for value in values):
            raise ValueError("Invalid pins")
        pins = {network_address(value) for value in values}
    except ValueError as exc:
        raise Denied("Invalid host-owned unicast address pins") from exc
    if not set(spec.addresses) <= pins:
        raise Denied("Nmap addresses exceed host-owned pins")
    # --max-rate and --scan-delay bound port-scan attempts; there is no -sV
    # phase whose application probes evade these bounds or auto-select NSE.
    # A runner must independently enforce ToolPlan.seconds across every host
    # and max_output_bytes; --host-timeout is only a per-host Nmap timeout.
    argv = ["/usr/bin/nmap", "--unprivileged", "-sT", "-Pn", "-n", "--disable-arp-ping",
            "--max-retries", "0", "--max-parallelism", "1", "--max-rate", str(scope.budget.rps),
            "--scan-delay", str(math.ceil(1000 / scope.budget.rps)) + "ms",
            "--host-timeout", str(spec.seconds) + "s", "-p", ",".join(map(str, spec.ports)), "-oX", "-"]
    if ipaddress.ip_address(spec.addresses[0]).version == 6:
        argv += ["-6"]
    argv += spec.addresses
    return ToolPlan(tool="nmap", profile="target_network", argv=tuple(argv), seconds=spec.seconds,
                    qualification="pending_nmap_runner_and_receiving_side_checks")


def preview_nmap(spec: NmapCheck | dict, scope: Scope, *, stage: str, now: float,
                 pinned_addresses: dict[str, list[str]]) -> ToolPlan:
    """Compatibility entry point for the same non-executable canonical planner."""
    if isinstance(spec, dict):
        spec = NmapCheck.model_validate(spec, strict=True)
    if not isinstance(spec, NmapCheck):
        raise Denied("Expected an NmapCheck")
    return build_check(spec, scope, stage=stage, now=now, pinned_addresses=pinned_addresses)


def tool_catalog() -> list[dict]:
    entries = [
        {"tool": "curl", "planner_implemented": True, "profile": "target_http", "production_qualified": False,
         "status": "pending_image_and_receiving_side_checks", "image_digest": None, "version": None,
         "methods": ["GET", "HEAD"], "limitations": "One direct URL with standard TLS verification; no query, redirects, credentials, custom headers or bodies; worker qualification pending",
         "reference": "https://curl.se/docs/manpage.html"},
        {"tool": "nmap", "planner_implemented": True, "preview_implemented": True, "profile": "target_network",
         "production_qualified": False, "status": "pending_nmap_runner_and_receiving_side_checks", "image_digest": None, "version": None,
         "limitations": "TCP port scan of explicitly scoped ports and host-owned IP pins; private RFC1918/ULA IPs additionally require exact approved address grants; no DNS, NSE, version detection, UDP or raw scans; runner must enforce total time/output budgets and qualify receiving-side isolation",
         "reference": "https://nmap.org/book/man-port-scanning-techniques.html"},
    ]
    for tool, requirement in (("httpx", "Qualified direct worker and typed request bounds"), ("katana", "Bounded crawl and forms/navigation policy"),
                              ("nuclei", "Versioned effect-safe template manifest"), ("zap", "Versioned Automation Framework plan"),
                              ("ffuf", "Versioned bounded wordlist and request budget"), ("testssl.sh", "Canonical target-network/TLS scope"),
                              ("gitleaks", "Isolated repository worker and redaction/evidence policy"),
                              ("sqlmap", "Separate action/PoC contract and authorization")):
        entries.append({"tool": tool, "planner_implemented": False, "production_qualified": False,
                        "status": "pending", "image_digest": None, "version": None, "requirement": requirement})
    return entries
