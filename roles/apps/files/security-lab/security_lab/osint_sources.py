"""Bounded public-source plans and pure parsers. No target traffic or network IO.

API references are listed by source_catalog; responses are observations, never
test authorization or ownership proof. Limits represent partial coverage.
"""
from __future__ import annotations

import ipaddress
import json
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from .contracts import hostname
from .discovery_contracts import Observation, ParsedSource, Relation, SourceRequest

MAX_ITEMS = 200
MAX_CT_NAMES = 500
DNS_TYPES = ("A", "AAAA", "CNAME", "MX", "NS", "TXT", "SOA")


def _request(source: str, category: str, url: str, subject: str) -> SourceRequest:
    return SourceRequest(source=source, category=category, url=url, parser=source, subject=subject)


def dns_requests(host: str) -> list[SourceRequest]:
    host = hostname(host)
    return [_request("dns", "dns", "https://dns.google/resolve?" + urlencode(
        {"name": host, "type": kind, "edns_client_subnet": "0.0.0.0/0"}), host) for kind in DNS_TYPES]


def initial_requests(seed: str) -> list[SourceRequest]:
    seed = hostname(seed)
    return [
        _request("crtsh", "subdomains", "https://crt.sh/?" + urlencode({"q": "%." + seed, "output": "json"}), seed),
        _request("wayback", "historical_urls", "https://web.archive.org/cdx/search/cdx?" + urlencode(
            {"url": "*." + seed + "/*", "output": "json", "fl": "original,timestamp,statuscode",
             "collapse": "urlkey", "limit": MAX_ITEMS}), seed),
        _request("github_repositories", "git_projects", "https://api.github.com/search/repositories?" + urlencode(
            {"q": '"' + seed + '" in:name,description,readme', "per_page": MAX_ITEMS // 2, "page": 1}), seed),
        _request("rdap_bootstrap", "registration", "https://data.iana.org/rdap/dns.json", seed),
        *dns_requests(seed),
    ]


def ip_requests(ip: str) -> list[SourceRequest]:
    address = ipaddress.ip_address(ip)
    if not address.is_global:
        raise ValueError("Public-source IP enrichment requires a global IP")
    normalized = str(address)
    requests = [_request("ripestat", "networks", "https://stat.ripe.net/data/network-info/data.json?" +
                         urlencode({"resource": normalized}), normalized)]
    # InternetDB documents IPv4; do not represent IPv6 as attempted coverage.
    if address.version == 4:
        requests.append(_request("internetdb", "indexed_services", "https://internetdb.shodan.io/" + normalized, normalized))
    return requests


def _json(body: str):
    return json.loads(body)


def select_registry(seed: str, body: str) -> SourceRequest | None:
    """Use IANA bootstrap HTTPS service, longest label suffix, without fetching it."""
    seed = hostname(seed)
    payload = _json(body)
    if not isinstance(payload, dict) or not isinstance(payload.get("services"), list):
        raise ValueError("Invalid IANA bootstrap services")
    matches = []
    for row in payload["services"][:2000]:
        if not isinstance(row, list) or len(row) != 2 or not all(isinstance(v, list) for v in row):
            continue
        for suffix in row[0]:
            if not isinstance(suffix, str):
                continue
            suffix = suffix.lower().strip(".")
            if not suffix or not (seed == suffix or seed.endswith("." + suffix)):
                continue
            for base in row[1]:
                if not isinstance(base, str):
                    continue
                parsed = urlsplit(base)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                    continue
                if parsed.port not in (None, 443):
                    continue
                try:
                    hostname(parsed.hostname)
                except ValueError:
                    continue
                matches.append((len(suffix.split(".")), base))
    if not matches:
        return None
    base = sorted(matches, key=lambda item: (-item[0], item[1]))[0][1]
    return _request("rdap_domain", "registration", base.rstrip("/") + "/domain/" + quote(seed, safe=""), seed)


def _within(value: str, seed: str) -> bool:
    return value == seed or value.endswith("." + seed)


class _Builder:
    def __init__(self):
        self.observations: list[Observation] = []
        self.relations: list[Relation] = []
        self.truncated = False
        self.invalid = 0
        self._seen: set[tuple] = set()

    def add(self, kind, value, pointer, attribution="candidate", freshness="unknown"):
        if not isinstance(value, str) or not value or len(value) > 2000:
            self.invalid += 1
            return
        key = (kind, value, attribution, freshness)
        if key not in self._seen:
            if len(self.observations) >= 2000:
                self.truncated = True
                return
            self._seen.add(key)
            self.observations.append(Observation(kind=kind, value=value, attribution=attribution,
                                                 freshness=freshness, pointer=pointer))

    def link(self, source_kind, source, relation, target_kind, target, pointer, basis, freshness="unknown"):
        if len(self.relations) >= 4000:
            self.truncated = True
            return
        if len(target) > 2000:
            self.invalid += 1
            return
        self.relations.append(Relation(source_kind=source_kind, source_value=source, relation=relation,
                                       target_kind=target_kind, target_value=target, pointer=pointer,
                                       basis=basis, freshness=freshness))

    def result(self, reason=""):
        if self.invalid:
            reason = (reason + "; " if reason else "") + f"Skipped {self.invalid} invalid rows/values"
        partial = self.truncated or bool(self.invalid)
        return ParsedSource(observations=self.observations, relations=self.relations,
                            status="partial" if partial else ("success" if self.observations else "empty"),
                            reason=reason, truncated=self.truncated)


def _rows(value, builder: _Builder, maximum=MAX_ITEMS):
    if not isinstance(value, list):
        raise ValueError("Expected an array")
    if len(value) > maximum:
        builder.truncated = True
    return value[:maximum]


def _crt(request, data):
    b = _Builder()
    seed = hostname(request.subject)
    names = 0
    for i, row in enumerate(_rows(data, b)):
        if not isinstance(row, dict) or not isinstance(row.get("name_value"), str):
            b.invalid += 1
            continue
        for j, raw in enumerate(row["name_value"].splitlines()):
            names += 1
            if names > MAX_CT_NAMES:
                b.truncated = True
                break
            if raw.startswith("*."):
                raw = raw[2:]
            try:
                name = hostname(raw)
            except ValueError:
                b.invalid += 1
                continue
            if not _within(name, seed):
                continue
            pointer = f"$[{i}].name_value[{j}]"
            b.add("domain", name, pointer, freshness="historical")
            b.link("domain", seed, "certificate_name", "domain", name, pointer,
                   "Certificate Transparency name; does not prove current DNS or ownership", "historical")
    return b.result("CT results bounded; certificate names are historical candidates")


def _wayback(request, data):
    b = _Builder()
    seed = hostname(request.subject)
    if data == []:
        return b.result()
    if not isinstance(data, list) or not data or not isinstance(data[0], list) or "original" not in data[0]:
        raise ValueError("Expected CDX JSON header with original")
    column = data[0].index("original")
    rows = _rows(data[1:], b)
    if len(rows) >= MAX_ITEMS:
        b.truncated = True  # limit exactly reached cannot prove no further records
    for i, row in enumerate(rows, 1):
        if not isinstance(row, list) or len(row) <= column or not isinstance(row[column], str):
            b.invalid += 1
            continue
        value = row[column]
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            b.invalid += 1
            continue
        try:
            name = hostname(parsed.hostname)
        except ValueError:
            b.invalid += 1
            continue
        if not _within(name, seed):
            continue
        pointer = f"$[{i}][{column}]"
        b.add("url", value, pointer, freshness="historical")
        b.add("domain", name, pointer, freshness="historical")
        b.link("domain", name, "archived_url", "url", value, pointer, "Wayback archive index only", "historical")
    return b.result("Archive index only; no archived or current target pages fetched")


def _github(request, data):
    b = _Builder()
    if not isinstance(data, dict) or type(data.get("total_count")) is not int or data["total_count"] < 0 or not isinstance(data.get("incomplete_results"), bool):
        raise ValueError("Expected GitHub repository search response")
    rows = _rows(data.get("items"), b, 100)
    b.truncated |= data["incomplete_results"] or data["total_count"] > len(rows)
    for i, row in enumerate(rows):
        value = row.get("html_url") if isinstance(row, dict) else None
        if not isinstance(value, str):
            b.invalid += 1
            continue
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.netloc != "github.com" or len(parsed.path.strip("/").split("/")) != 2 or parsed.query or parsed.fragment:
            b.invalid += 1
            continue
        pointer = f"$.items[{i}].html_url"
        b.add("repository", value, pointer, freshness="current_observation")
        b.link("domain", request.subject, "repository_search_match", "repository", value, pointer,
               "Repository search match for quoted domain; does not prove ownership or a code-content match", "current_observation")
    return b.result("Public repository metadata/README search, first page only; code search not attempted")


def _dns(request, data):
    b = _Builder()
    if not isinstance(data, dict) or type(data.get("Status")) is not int:
        raise ValueError("Expected DNS JSON response code")
    code = data["Status"]
    if code == 3:
        return ParsedSource(status="empty", reason="DNS NXDOMAIN; resolver observation only")
    if code != 0:
        return ParsedSource(status="error", reason=f"DNS response code {code} (resolver failure/refusal, not absence)")
    b.truncated = data.get("TC") is True
    requested_type = parse_qs(urlsplit(request.url).query).get("type", [""])[0]
    for i, row in enumerate(_rows(data.get("Answer", []), b)):
        if not isinstance(row, dict) or type(row.get("type")) is not int or not isinstance(row.get("data"), str):
            b.invalid += 1
            continue
        raw, kind = row["data"], row["type"]
        pointer = f"$.Answer[{i}].data"
        b.add("dns_record", f"{requested_type}/{kind} {raw}", pointer, "supported", "current_observation")
        target_kind = None
        try:
            if kind in {1, 28}:
                address = ipaddress.ip_address(raw)
                if address.version != (4 if kind == 1 else 6):
                    raise ValueError("Wrong address family")
                target_kind, target = "ip", str(address)
                attribution = "supported"
            elif kind in {5, 2, 15, 6}:
                fields = raw.split()
                if kind == 15 and (len(fields) != 2 or not fields[0].isdigit()):
                    raise ValueError("Invalid MX")
                target = hostname(fields[1] if kind == 15 else fields[0])
                target_kind = "domain"
                attribution = "candidate" if _within(target, request.subject) else "shared_provider"
        except (ValueError, IndexError):
            b.invalid += 1
            continue
        if target_kind:
            b.add(target_kind, target, pointer, attribution, "current_observation")
            b.link("domain", request.subject, {1: "resolves_to", 28: "resolves_to", 5: "cname", 2: "nameserver", 15: "mail_exchange", 6: "soa_primary"}[kind],
                   target_kind, target, pointer, "Public recursive resolver answer; DNS linkage is not ownership", "current_observation")
    return b.result("DNS NODATA" if not data.get("Answer") else "Public resolver answer, not a direct target probe")


def _ripe(request, data):
    b = _Builder()
    if not isinstance(data, dict) or data.get("status") != "ok" or not isinstance(data.get("data"), dict):
        raise ValueError("RIPEstat did not return an ok data object")
    payload = data["data"]
    prefix = payload.get("prefix")
    if prefix:
        network = ipaddress.ip_network(prefix)
        if ipaddress.ip_address(request.subject) not in network:
            raise ValueError("RIPE prefix does not contain requested IP")
        b.add("network", str(network), "$.data.prefix", "shared_provider", "current_observation")
        b.link("ip", request.subject, "routed_in", "network", str(network), "$.data.prefix", "Containing routed prefix; no ownership inference", "current_observation")
    for i, raw in enumerate(_rows(payload.get("asns", []), b)):
        if not isinstance(raw, (str, int)) or isinstance(raw, bool) or not str(raw).isdigit() or not 0 < int(raw) <= 4294967295:
            b.invalid += 1
            continue
        value = "AS" + str(int(raw))
        pointer = f"$.data.asns[{i}]"
        b.add("asn", value, pointer, "shared_provider", "current_observation")
        b.link("ip", request.subject, "announced_by", "asn", value, pointer, "RIPE RIS announcing ASN; hosting/shared provider, not asset ownership", "current_observation")
    return b.result("RIPE RIS routing metadata (periodic dataset), no network expansion")


def _internetdb(request, data):
    b = _Builder()
    if not isinstance(data, dict) or data.get("ip") != request.subject:
        raise ValueError("InternetDB response IP mismatch or missing")
    if not all(isinstance(data.get(field), list) for field in ("ports", "hostnames", "cpes", "vulns", "tags")):
        raise ValueError("Incomplete InternetDB array fields")
    for field, kind in (("ports", "service"), ("hostnames", "domain"), ("cpes", "technology"), ("vulns", "vulnerability_claim"), ("tags", "tag")):
        for i, raw in enumerate(_rows(data.get(field, []), b)):
            pointer = f"$.{field}[{i}]"
            if field == "ports":
                if type(raw) is not int or not 1 <= raw <= 65535:
                    b.invalid += 1
                    continue
                value = request.subject + ":" + str(raw)
            elif field == "hostnames":
                try:
                    value = hostname(raw)
                except (ValueError, TypeError, AttributeError):
                    b.invalid += 1
                    continue
            elif isinstance(raw, str):
                value = raw
            else:
                b.invalid += 1
                continue
            b.add(kind, value, pointer, freshness="historical")
            b.link("ip", request.subject, "indexed_" + field, kind, value, pointer,
                   "InternetDB periodically indexed hint; vulnerabilities may be unverified; no current scan", "historical")
    return b.result("Periodic third-party index; IP co-hosts and vulnerability hints require independent verification")


def _rdap(request, data):
    b = _Builder()
    if not isinstance(data, dict) or data.get("objectClassName") != "domain":
        raise ValueError("Expected RDAP domain object")
    name = hostname(data.get("ldhName", ""))
    if name != request.subject:
        raise ValueError("RDAP response domain mismatch")
    b.add("domain", name, "$.ldhName", "supported", "current_observation")
    for i, status in enumerate(_rows(data.get("status", []), b)):
        b.add("registration", str(status) if isinstance(status, str) else "", f"$.status[{i}]", "supported", "current_observation")
    for i, event in enumerate(_rows(data.get("events", []), b)):
        if isinstance(event, dict) and isinstance(event.get("eventAction"), str) and isinstance(event.get("eventDate"), str):
            b.add("registration", event["eventAction"] + ": " + event["eventDate"], f"$.events[{i}]", "supported", "current_observation")
        else:
            b.invalid += 1
    for i, entry in enumerate(_rows(data.get("nameservers", []), b)):
        try:
            value = hostname(entry.get("ldhName", ""))
        except (ValueError, AttributeError):
            b.invalid += 1
            continue
        pointer = f"$.nameservers[{i}].ldhName"
        b.add("domain", value, pointer, "shared_provider", "current_observation")
        b.link("domain", name, "registered_nameserver", "domain", value, pointer, "Registry RDAP delegation metadata; not ownership", "current_observation")
    # Do not map registrant contact data into the surface graph or infer ownership.
    return b.result("Registry domain metadata; registrant contacts not extracted; no ownership assertion")


def parse_source(request: SourceRequest, body: str) -> ParsedSource:
    """Parse a receipt body; malformed payloads are explicit errors, not zero findings."""
    try:
        data = _json(body)
        if request.parser == "rdap_bootstrap":
            selected = select_registry(request.subject, body)
            truncated = len(data["services"]) > 2000
            return ParsedSource(status="partial" if truncated else ("success" if selected else "empty"),
                                truncated=truncated,
                                reason="IANA HTTPS registry selected" if selected else "No supported HTTPS registry for seed")
        parser = {"crtsh": _crt, "wayback": _wayback, "github_repositories": _github,
                  "dns": _dns, "ripestat": _ripe, "internetdb": _internetdb, "rdap_domain": _rdap}.get(request.parser)
        if not parser:
            raise ValueError("Unsupported source parser")
        return parser(request, data)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        return ParsedSource(status="error", reason="Invalid source payload: " + str(exc)[:300])


def source_catalog() -> list[dict]:
    """Actual implemented coverage and explicitly absent optional integrations."""
    definitions = [
        ("crtsh", "Certificate Transparency names", "https://crt.sh/", "Historical candidates; bounded 200 rows/500 names"),
        ("wayback", "Wayback CDX URL index", "https://github.com/internetarchive/wayback/tree/master/wayback-cdx-server", "200 collapsed rows; no page retrieval"),
        ("github_repositories", "GitHub public repository search", "https://docs.github.com/en/rest/search/search", "First 100 results; metadata/README only; no code search, ownership or clone"),
        ("dns", "Google public DNS JSON", "https://developers.google.com/speed/public-dns/docs/doh/json", "A AAAA CNAME MX NS TXT SOA; recursive resolution may contact authoritative DNS"),
        ("rdap_bootstrap", "IANA DNS bootstrap", "https://www.iana.org/assignments/rdap-dns/", "HTTPS registry routing only"),
        ("rdap_domain", "Registry RDAP", "https://www.rfc-editor.org/rfc/rfc9083", "Domain/delegation metadata; no registrant contact extraction"),
        ("ripestat", "RIPE RIS network info", "https://stat.ripe.net/docs/data-api/api-endpoints/network-info.html", "Containing prefix/announcing ASN only; never ownership or expansion"),
        ("internetdb", "Shodan InternetDB", "https://book.shodan.io/developer-apis/internetdb/", "IPv4 weekly index; unverified hints; HTTP404 means no indexed information, not safe/closed"),
    ]
    categories = {"crtsh": "subdomains", "wayback": "historical_urls", "github_repositories": "git_projects",
                  "dns": "dns", "rdap_bootstrap": "registration", "rdap_domain": "registration",
                  "ripestat": "networks", "internetdb": "indexed_services"}
    result = [dict(source=s, name=n, category=categories[s], implemented=True, implemented_method="public_https_get",
                   subscription_required=False, authentication_required=False, reference=r, limitations=l) for s, n, r, l in definitions]
    result.append(dict(source="subfinder", name="Passive Subfinder CLI", category="subdomains",
        implemented=True, implemented_method="host_configured_compose_cli", status="not_attempted",
        subscription_required=False, authentication_required=False,
        reference="https://github.com/projectdiscovery/subfinder",
        limitations="Separate start_subfinder_discovery tool; applied bundle/service required; actual provider coverage unknown"))
    for source, reason in (("dnsdumpster", "Free account/API adapter and per-adapter secret isolation pending"),
                           ("github_code", "Authenticated public code-search adapter pending; repository search is not code coverage"),
                           ("searxng", "Configured search instance adapter pending")):
        result.append(dict(source=source, category={"dnsdumpster": "subdomains", "subfinder": "subdomains",
                                                  "github_code": "git_projects", "searxng": "web_search"}[source],
                           implemented=False, implemented_method=None, status="not_attempted", subscription_required=False, reason=reason))
    return result
