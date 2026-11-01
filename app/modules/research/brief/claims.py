"""Structured claims, code-side rendering and the verifier (SPEC 7.5 "Brief generation contract").

The model (or the mock) returns claims whose `text_template` may contain numbers ONLY as `{{mN}}` placeholders.
Code substitutes the formatted ledger value. Verifier rules (hard fail):
  (a) a literal digit sequence in prose that is not a placeholder and not an allow-listed date/label,
  (b) a placeholder that refers to a ledger id that does not exist,
  (c) a factual claim with no citation,
  (d) forbidden phrases (guarantees, predictions, price targets, advice, urgency).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from app.modules.research.brief.ledger import Ledger
from app.modules.safety.guardrail import check_output

PLACEHOLDER = re.compile(r"\{\{\s*(m\d+)\s*\}\}")
HANDLE = re.compile(r"^(?:s\d+|m\d+)$")

#: date / label shapes that may appear as literal digits (everything else must be a placeholder)
ALLOWED_LITERALS = (
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),  # ISO date
    re.compile(r"\b(?:19|20)\d{2}\b"),  # year
    re.compile(r"\bFY\s?\d{2,4}\b"),  # fiscal-year label
    re.compile(
        r"\bItems?\s+\d{1,2}(?:\.\d{2}|[A-C])?(?:\s*(?:,|and|&)\s*\d{1,2}(?:\.\d{2}|[A-C])?)*", re.IGNORECASE
    ),
    re.compile(r"\b(?:10-K|10-Q|8-K|20-F|DEF 14A)\b"),
    re.compile(r"\bQ[1-4]\b"),
)
_DIGITS = re.compile(r"\d+")

#: code-authored availability notices are exempt from the citation rule (they make no claim about the company)
NOTICE_PREFIXES = (
    "Not enough information was found",
    "Free cash flow and debt to equity are not meaningful",
    "Figures for this section are not available",
    "A comparison with peers is not available",
    "No recent Form 8-K",
)


class Claim(BaseModel):
    text_template: str = Field(max_length=1200)
    ledger_refs: list[str] = Field(default_factory=list)
    citation_ids: list[str] = Field(
        default_factory=list
    )  # handles: sK = retrieved passage, mK = ledger entry
    kind: Literal["fact", "notice"] = "fact"


class SectionClaims(BaseModel):
    key: str
    claims: list[Claim] = Field(default_factory=list)


class BriefClaims(BaseModel):
    sections: list[SectionClaims]
    bull: Claim
    bear: Claim


def strip_allowed(text: str) -> str:
    for rx in ALLOWED_LITERALS:
        text = rx.sub(" ", text)
    return text


def literal_numbers(template: str) -> list[str]:
    """Digit sequences in the *template* that are neither placeholders nor allow-listed labels."""
    without_ph = PLACEHOLDER.sub(" ", template)
    return _DIGITS.findall(strip_allowed(without_ph))


def is_notice(claim: Claim) -> bool:
    return (
        claim.kind == "notice"
        and claim.text_template.startswith(NOTICE_PREFIXES)
        and not PLACEHOLDER.search(claim.text_template)
    )


@dataclass(slots=True)
class VerificationReport:
    violations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


def verify_claim(claim: Claim, ledger: Ledger, valid_handles: set[str], where: str) -> list[str]:
    out: list[str] = []
    for lit in literal_numbers(claim.text_template):
        out.append(f"{where}: (a) literal number {lit!r} outside placeholders")
    for pid in PLACEHOLDER.findall(claim.text_template):
        if ledger.get(pid) is None:
            out.append(f"{where}: (b) placeholder {pid} not in ledger")
    for ref in claim.ledger_refs:
        if ledger.get(ref) is None:
            out.append(f"{where}: (b) ledger_ref {ref} not in ledger")
    if "{{" in PLACEHOLDER.sub("", claim.text_template) or "}}" in PLACEHOLDER.sub("", claim.text_template):
        out.append(f"{where}: (a) malformed placeholder")
    if claim.kind == "notice":
        if not is_notice(claim):
            out.append(f"{where}: notice claim is not a recognised availability notice")
    else:
        cites = [c for c in claim.citation_ids if HANDLE.match(c) and c in valid_handles]
        if not cites:
            out.append(f"{where}: (c) no valid citation")
    guard = check_output(claim.text_template)
    for v in guard.violations:
        out.append(f"{where}: (d) forbidden phrase {v.phrase!r} [{v.category}]")
    return out


def verify_brief(claims: BriefClaims, ledger: Ledger, valid_handles: set[str]) -> VerificationReport:
    rep = VerificationReport()
    for sec in claims.sections:
        for i, c in enumerate(sec.claims):
            rep.violations.extend(verify_claim(c, ledger, valid_handles, f"{sec.key}[{i}]"))
    rep.violations.extend(verify_claim(claims.bull, ledger, valid_handles, "bull"))
    rep.violations.extend(verify_claim(claims.bear, ledger, valid_handles, "bear"))
    return rep


def render_text(template: str, ledger: Ledger) -> str:
    return PLACEHOLDER.sub(lambda m: ledger.entries[m.group(1)].display, template)


def placeholders_in(template: str) -> list[str]:
    return list(dict.fromkeys(PLACEHOLDER.findall(template)))


def count_unverified_numbers(rendered: str, ledger: Ledger, used_ids: Iterable[str]) -> int:
    """Independent post-render scan: remove the ledger displays that were substituted and the allow-listed
    labels; any digit sequence left over is a number nobody verified."""
    text = rendered
    for i in sorted(set(used_ids), key=lambda x: -len(ledger.entries[x].display)):
        text = text.replace(ledger.entries[i].display, " ")
    return len(_DIGITS.findall(strip_allowed(text)))
