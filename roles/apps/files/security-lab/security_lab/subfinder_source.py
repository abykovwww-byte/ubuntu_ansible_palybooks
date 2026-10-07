"""Pure Subfinder passive-source adapter; no process, DNS or network execution.

Official CLI and JSONL shapes: projectdiscovery/subfinder v2.16.0 README.md and
pkg/runner/{options,outputter}.go. JSONL reports source names, not provider
success/failure inventories. Runtime installation and source availability must
be established separately by the host. External names remain untrusted data.
"""
from __future__ import annotations

import hashlib
import json
import math
import re

from .contracts import canonical, hostname
from .discovery_contracts import Observation, Relation, SourceRequest, SourceResult

MAX_OUTPUT_BYTES = 1_000_000
MAX_NAMES = 2000
MAX_PROVIDERS_PER_NAME = 16
UPSTREAM_REFERENCE = 'https://github.com/projectdiscovery/subfinder/tree/v2.16.0'
_PROVIDER = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_VERSION = re.compile(r"v?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]{1,50})?\Z")


def _ascii_host(value: str) -> str:
    if (type(value) is not str or not value or value.endswith('..')
            or any(ord(char) <= 32 or ord(char) >= 127 for char in value)):
        raise ValueError('Expected an unambiguous ASCII hostname')
    return hostname(value)


def compile_subfinder(seed: str) -> tuple[str, ...]:
    """Fixed passive command. The host supplies a clean config/env and hard cap.

    No -active, -all, provider credentials, paths, proxy or flags are accepted.
    Subfinder's -max-time is minutes; the host must enforce a wall-clock timeout
    and output cap independently. Default provider/key coverage is unknown.
    """
    seed = _ascii_host(seed)
    return ('/usr/local/bin/subfinder', '-d', seed, '-json', '-silent', '-collect-sources',
            '-timeout', '10', '-max-time', '1', '-disable-update-check')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Ambiguous duplicate JSON member')
        result[key] = value
    return result


def parse_subfinder(seed: str, stdout_jsonl: str, exit_code: int, tool_version: str, *,
                    started_at: float, finished_at: float,
                    output_truncated: bool = False) -> SourceResult:
    """Convert bounded host-owned CLI output into a graph-compatible receipt.

    Evidence retains only recognized host/input/provider/wildcard fields, plus
    counts and the bounded captured-stdout hash. Unknown fields and discarded
    rows cannot smuggle secrets or instructions into the stored evidence body.
    This is a collection receipt, never an ownership or scope grant.
    """
    seed = _ascii_host(seed)
    if type(stdout_jsonl) is not str or type(exit_code) is not int or type(output_truncated) is not bool:
        raise ValueError('Invalid host-owned process receipt')
    if type(tool_version) is not str or len(tool_version) > 80 or not _VERSION.fullmatch(tool_version):
        raise ValueError('Expected the actual Subfinder semantic version')
    if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
           for value in (started_at, finished_at)) or finished_at < started_at:
        raise ValueError('Invalid process collection window')
    # Bound encoding work even when a caller supplies an oversized Python str.
    prefix = stdout_jsonl[:MAX_OUTPUT_BYTES + 1]
    captured = prefix.encode('utf-8')
    truncated = output_truncated or len(prefix) != len(stdout_jsonl) or len(captured) > MAX_OUTPUT_BYTES
    captured = captured[:MAX_OUTPUT_BYTES]
    text = captured.decode('utf-8', errors='ignore')
    counts = dict(malformed_lines=0, invalid_hosts=0, outside_seed=0, input_mismatch=0,
                  provenance_gaps=0, excluded_fields=0, name_cap=0)
    records: dict[str, dict] = {}
    pointers: dict[str, int] = {}
    provenance_complete = {}
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line, object_pairs_hook=_unique_object)
        except (ValueError, RecursionError):
            counts['malformed_lines'] += 1
            continue
        if not isinstance(row, dict):
            counts['malformed_lines'] += 1
            continue
        try:
            if _ascii_host(row.get('input')) != seed:
                counts['input_mismatch'] += 1
                continue
            raw_host = row.get('host')
            wildcard = type(raw_host) is str and raw_host.startswith('*.')
            name = _ascii_host(raw_host[2:] if wildcard else raw_host)
        except ValueError:
            counts['invalid_hosts'] += 1
            continue
        if not (name == seed or name.endswith('.' + seed)):
            counts['outside_seed'] += 1
            continue
        if 'wildcard_certificate' in row and type(row['wildcard_certificate']) is not bool:
            counts['malformed_lines'] += 1
            continue
        wildcard |= row.get('wildcard_certificate', False)
        # These are the two actual upstream JSONL forms, not invented fields.
        complete = 'sources' in row
        sources = row.get('sources', [row['source']] if 'source' in row else [])
        if not isinstance(sources, list):
            sources = []
            counts['provenance_gaps'] += 1
            complete = False
        valid = {value.lower() for value in sources
                 if type(value) is str and _PROVIDER.fullmatch(value)}
        if not valid or any(type(value) is not str or not _PROVIDER.fullmatch(value) for value in sources):
            counts['provenance_gaps'] += 1
            complete = False
        if not complete:
            counts['provenance_gaps'] += 1  # singular output does not report all sources
        excluded = set(row) - {'host', 'input', 'source', 'sources', 'wildcard_certificate'}
        if excluded:
            counts['excluded_fields'] += 1
        if name not in records:
            if len(records) >= MAX_NAMES:
                counts['name_cap'] += 1
                truncated = True
                continue
            records[name] = dict(host=name, input=seed, sources=[], wildcard_certificate=False)
            pointers[name] = line_number
            provenance_complete[name] = True
        combined = sorted(set(records[name]['sources']) | valid)
        if len(combined) > MAX_PROVIDERS_PER_NAME:
            truncated = True
            counts['provenance_gaps'] += 1
        records[name]['sources'] = combined[:MAX_PROVIDERS_PER_NAME]
        records[name]['wildcard_certificate'] |= wildcard
        provenance_complete[name] &= complete

    metadata = dict(tool='subfinder', tool_version=tool_version, exit_code=exit_code,
                    parser_format_reference=UPSTREAM_REFERENCE,
                    started_at=float(started_at), finished_at=float(finished_at),
                    captured_stdout_sha256=hashlib.sha256(captured).hexdigest(),
                    stdout_capture_truncated=truncated, discarded=counts,
                    provider_completion='unknown', provider_credentials='unknown',
                    coverage='bounded_default_passive_sources', external_data_trusted=False)
    # Store a compact recognized-field envelope, not raw secret-bearing noise.
    retained = []
    used = 0
    for name, row in records.items():
        row = {**row, 'stdout_line': pointers[name], 'all_reported_sources': provenance_complete[name]}
        size = len(canonical(row).encode('utf-8')) + 1
        if used + size > MAX_OUTPUT_BYTES - 4096:
            truncated = True
            counts['name_cap'] += 1
            continue
        retained.append(row)
        used += size
    metadata['stdout_capture_truncated'] = truncated
    body = canonical({'metadata': metadata, 'records': retained})
    observations = [Observation(kind='domain', value=row['host'], attribution='candidate',
                                freshness='unknown', pointer=f'$.records[{index}].host')
                    for index, row in enumerate(retained)]
    relations = [Relation(source_kind='domain', source_value=seed, relation='passive_subdomain_candidate',
                          target_kind='domain', target_value=row['host'], freshness='unknown',
                          pointer=f'$.records[{index}]',
                          basis='Subfinder passive report; reported providers: ' +
                          (', '.join(row['sources']) or 'not reported') +
                          '; does not prove current DNS, ownership or test authorization' +
                          ('; wildcard-derived name' if row['wildcard_certificate'] else ''))
                 for index, row in enumerate(retained)]
    partial = truncated or any(counts.values()) or exit_code != 0
    status = 'partial' if partial and observations else (
        'error' if exit_code != 0 or counts['malformed_lines'] or counts['invalid_hosts']
        or counts['input_mismatch'] else 'partial' if partial else 'success' if observations else 'empty')
    reason = (f'Subfinder {tool_version}; exit_code={exit_code}; ' +
              '; '.join(f'{key}={value}' for key, value in counts.items()) +
              '; provider completion/keys unknown; bounded passive coverage; empty is not absence proof')
    return SourceResult(request=SourceRequest(source='subfinder', category='subdomains',
                        url='tool://subfinder/domain/' + seed, parser='subfinder_jsonl', subject=seed),
                        collected_at=float(finished_at), status=status, body=body,
                        body_hash=hashlib.sha256(body.encode('utf-8')).hexdigest(),
                        parser_version='1', reason=reason, truncated=truncated,
                        observations=observations, relations=relations)
