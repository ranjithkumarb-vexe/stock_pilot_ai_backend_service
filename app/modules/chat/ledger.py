"""The chat numeric ledger: every figure in an answer is registered here by code, and the final text is
verified against it (SPEC 7.5 "numbers come from code, words come from the model").

Verifier rules (the chat analogue of the brief verifier):
* every numeric token in the final answer must be one of the tokens of a ledger display/exact value;
* dates, accession numbers, form names (10-K, 8-K), fiscal-year labels, "Item 1A" and citation markers `[1]`
  are not figures and are removed before the check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

_ACCESSION = re.compile(r"\b\d{10}-\d{2}-\d{6}\b")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_FY = re.compile(r"\b(?:FY|CY|Q)\s?\d{1,4}\b", re.IGNORECASE)
_FORM = re.compile(r"\b\d{1,2}-?[KQ]\b|\bForm \d+[A-Z]?\b", re.IGNORECASE)
_ITEM = re.compile(r"\bItems? \d+(?:\.\d+)?[A-C]?\b", re.IGNORECASE)
_MARKER = re.compile(r"\[\d{1,2}\]")
_TICKER_DIGITS = re.compile(r"\b[A-Z]{1,5}\d+[A-Z]*\b")
_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")


def normalise_number(raw: str) -> str | None:
    s = raw.replace(",", "").rstrip(".")
    try:
        d = Decimal(s)
    except InvalidOperation:
        return None
    return format(d.normalize(), "f")


def strip_non_figures(text: str) -> str:
    for rx in (_ACCESSION, _DATE, _FY, _FORM, _ITEM, _MARKER, _TICKER_DIGITS):
        text = rx.sub(" ", text)
    return text


def numeric_tokens(text: str) -> list[str]:
    """Normalised numeric tokens of `text` that count as figures."""
    out: list[str] = []
    for m in _NUMBER.finditer(strip_non_figures(text)):
        n = normalise_number(m.group(0))
        if n is not None:
            out.append(n)
    return out


@dataclass(slots=True)
class Entry:
    id: str
    label: str
    display: str
    source: str
    value: float | None = None
    exact: str | None = None


@dataclass(slots=True)
class ChatLedger:
    entries: dict[str, Entry] = field(default_factory=dict)

    def add(
        self,
        label: str,
        display: str,
        *,
        source: str,
        value: float | Decimal | None = None,
        exact: str | None = None,
    ) -> str:
        """Register a figure and return the text to put in the answer (its display string)."""
        eid = f"m{len(self.entries) + 1}"
        self.entries[eid] = Entry(eid, label, display, source, None if value is None else float(value), exact)
        return display

    def allowed_tokens(self) -> set[str]:
        toks: set[str] = set()
        for e in self.entries.values():
            toks.update(numeric_tokens(e.display))
            if e.exact:
                toks.update(numeric_tokens(e.exact))
        return toks

    def unverified(self, text: str) -> list[str]:
        allowed = self.allowed_tokens()
        return [t for t in numeric_tokens(text) if t not in allowed]

    def as_json(self) -> dict[str, Any]:
        return {
            e.id: {
                "label": e.label,
                "display": e.display,
                "exact": e.exact,
                "value": e.value,
                "source": e.source,
            }
            for e in self.entries.values()
        }

    def __len__(self) -> int:
        return len(self.entries)
