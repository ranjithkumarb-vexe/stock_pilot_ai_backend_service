"""Static section catalogue: titles/source labels/plain hints are taken from the finalized UI (design SECS/PLAIN),
plus the retrieval recipe used to find supporting passages for each section."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SectionMeta:
    key: str
    title: str
    source_label: str
    plain_hint: str
    query: str = ""
    items: tuple[str, ...] = ()
    forms: tuple[str, ...] = ("10-K",)


SECTIONS: dict[str, SectionMeta] = {
    "trend": SectionMeta(
        "trend",
        "Trend & growth",
        "Item 7 · MD&A",
        "In plain terms: this shows whether the company is selling more each year and keeping more of each sale as profit.",
        "revenue growth drivers segment results of operations net sales increased demand",
        ("7",),
    ),
    "bal": SectionMeta(
        "bal",
        "Balance sheet & dividend",
        "XBRL + Item 7",
        "In plain terms: debt is money owed. A company with low debt and steady cash is less likely to cut its dividend.",
        "liquidity capital resources debt cash dividends share repurchases borrowings",
        ("7",),
    ),
    "val": SectionMeta(
        "val",
        "Valuation context",
        "XBRL ratio (code)",
        "In plain terms: P/E is the share price divided by yearly profit per share. A higher number means you pay more for each dollar earned.",
    ),
    "moat": SectionMeta(
        "moat",
        "Moat",
        "Item 1 · Business",
        "In plain terms: a moat is something that makes it hard for rivals to take customers away.",
        "competitive advantages competition brand customers ecosystem platform scale differentiation",
        ("1",),
    ),
    "risk": SectionMeta(
        "risk",
        "Risks",
        "Item 1A · Risk Factors",
        "In plain terms: these are problems the company itself told regulators could hurt results.",
        "risks could adversely affect regulation competition suppliers customers litigation",
        ("1A",),
    ),
    "cat": SectionMeta(
        "cat",
        "Catalysts",
        "Item 7 + 8-K",
        "In plain terms: catalysts are dated events, such as earnings reports, that can move the share price.",
        "outlook upcoming launch expected ramp guidance announced agreement results",
        ("7", "8-K"),
        ("10-K", "8-K"),
    ),
    "bb": SectionMeta(
        "bb",
        "Bull / Bear",
        "Item 1A + Item 7",
        "In plain terms: the bull case is what must go right. The bear case is what could go wrong.",
    ),
}
