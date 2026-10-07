"""Versioned public-source receipts, independent of scope and checks approval."""
from __future__ import annotations

from typing import Literal
from pydantic import Field
from .contracts import Contract

CollectionStatus = Literal['success', 'empty', 'partial', 'error', 'rate_limited', 'blocked', 'not_attempted']


class SourceRequest(Contract):
    source: str
    category: str
    url: str
    parser: str
    subject: str


class Observation(Contract):
    kind: str
    value: str = Field(min_length=1, max_length=2000)
    attribution: Literal['supported', 'candidate', 'shared_provider', 'unrelated', 'unknown'] = 'candidate'
    freshness: Literal['historical', 'current_observation', 'stale', 'unknown'] = 'unknown'
    pointer: str = Field(default='$', max_length=1000)


class Relation(Contract):
    source_kind: str
    source_value: str
    relation: str
    target_kind: str
    target_value: str
    basis: str = Field(max_length=2000)
    freshness: Literal['historical', 'current_observation', 'stale', 'unknown'] = 'unknown'
    pointer: str = Field(default='$', max_length=1000)


class ParsedSource(Contract):
    observations: list[Observation] = Field(default_factory=list, max_length=2000)
    relations: list[Relation] = Field(default_factory=list, max_length=4000)
    status: CollectionStatus = 'success'
    reason: str = Field(default='', max_length=1000)
    truncated: bool = False


class SourceResult(Contract):
    schema_version: Literal[1] = 1
    request: SourceRequest
    collected_at: float
    status: CollectionStatus
    http_status: int | None = None
    body: str = Field(default='', max_length=2_000_000)
    body_hash: str
    parser_version: str = '1'
    reason: str = Field(default='', max_length=1000)
    truncated: bool = False
    synthetic: bool = False
    observations: list[Observation] = Field(default_factory=list, max_length=2000)
    relations: list[Relation] = Field(default_factory=list, max_length=4000)
