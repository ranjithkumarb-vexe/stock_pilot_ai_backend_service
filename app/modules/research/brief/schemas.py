"""Response models for the brief and citation endpoints (CONTRACT.md "Brief shape" / "Citation")."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClaimOut(_Out):
    text: str
    citation_ids: list[str]
    ledger_refs: list[str]


class RowOut(_Out):
    label: str
    value: str
    source: str


class SectionOut(_Out):
    key: Literal["trend", "bal", "val", "moat", "risk", "cat", "bb"]
    title: str
    source_label: str
    claims: list[ClaimOut]
    rows: list[RowOut]
    plain_hint: str | None


class CaseOut(_Out):
    text: str
    citation_ids: list[str]


class LedgerEntryOut(_Out):
    label: str
    value: float | None
    display: str
    unit: str
    source: str


class CitationsSummary(_Out):
    coverage_pct: float
    unverified_numbers: int


class StyleCardOut(_Out):
    order: list[str]
    jargon: int
    depth: str
    tone: str
    metric: str | None


class BriefOut(_Out):
    id: str
    symbol: str
    name: str
    as_of: str
    generated_at: str
    status: Literal["ok", "degraded"]
    stale: bool
    stale_reason: str | None
    style_card: StyleCardOut
    section_order: list[str]
    sections: list[SectionOut]
    bull: CaseOut
    bear: CaseOut
    ledger: dict[str, LedgerEntryOut]
    citations_summary: CitationsSummary
    disclaimer: str
    trace_id: str | None


class CitationRowOut(_Out):
    label: str
    value: str
    source: str


class CitationOut(_Out):
    id: str
    kind: Literal["chunk", "xbrl", "price"]
    title: str
    company: str
    doc_label: str
    accession_no: str | None
    filed_at: str | None
    fetched_at: str | None
    sha256: str | None
    url: str | None
    excerpt: str
    rows: list[CitationRowOut]
