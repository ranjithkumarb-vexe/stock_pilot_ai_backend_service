"""Split a 10-K's text into Items 1, 1A, 7, 7A (and 8-K body) - SPEC 7.5 step 4.

The table of contents repeats every Item heading with almost nothing after it, so for each Item we keep the
heading occurrence that owns the *longest* span (the real section). Cross-references such as
"see Item 1A. Risk Factors" are ignored because headings must be short lines that start with "Item".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.modules.research.htmltext import TABLE_MARK

TARGET_ITEMS: tuple[str, ...] = ("1", "1A", "7", "7A")
ITEM_TITLES: dict[str, str] = {
    "1": "Business",
    "1A": "Risk Factors",
    "7": "Management's Discussion and Analysis",
    "7A": "Quantitative and Qualitative Disclosures About Market Risk",
}
_HEADING = re.compile(
    r"^\W*(?:part\s+[ivx]+\W+)?item\s*(\d{1,2}\s?[abc]?)\b\W*(.*)$",
    re.IGNORECASE,
)
_KEYWORDS: dict[str, tuple[str, ...]] = {
    "1": ("business",),
    "1A": ("risk factor",),
    "1B": ("unresolved",),
    "1C": ("cybersecurity",),
    "2": ("properties",),
    "3": ("legal",),
    "4": ("mine",),
    "5": ("market for",),
    "6": ("reserved", "selected"),
    "7": ("management",),
    "7A": ("quantitative",),
    "8": ("financial statements",),
    "9": ("changes in",),
    "9A": ("controls",),
    "9B": ("other information",),
    "10": ("directors",),
    "11": ("executive compensation",),
    "12": ("security ownership",),
    "13": ("certain relationships",),
    "14": ("principal account",),
    "15": ("exhibit",),
    "16": ("form 10-k summary",),
}
MAX_HEADING_CHARS = 200


@dataclass(frozen=True, slots=True)
class Section:
    item: str
    title: str
    text: str  # may contain table markers

    @property
    def prose_chars(self) -> int:
        return len(TABLE_MARK.sub("", self.text).strip())


def _norm_item(raw: str) -> str:
    return re.sub(r"\s+", "", raw).upper()


def find_headings(lines: list[str]) -> list[tuple[int, str, str]]:
    """(line_index, item_key, title_text) for every plausible Item heading line."""
    out: list[tuple[int, str, str]] = []
    for i, line in enumerate(lines):
        if len(line) > MAX_HEADING_CHARS:
            continue
        m = _HEADING.match(line)
        if not m:
            continue
        key = _norm_item(m.group(1))
        if key not in _KEYWORDS:
            continue
        title = m.group(2).strip()
        if not title:  # heading text on the next line
            nxt = next((ln for ln in lines[i + 1 : i + 3] if ln.strip()), "")
            title = nxt.strip() if len(nxt) < MAX_HEADING_CHARS else ""
        if not any(k in title.lower() for k in _KEYWORDS[key]):
            continue
        out.append((i, key, title))
    return out


def split_10k(text: str) -> dict[str, Section]:
    """Map target Item key -> Section (missing Items are simply absent)."""
    lines = text.split("\n")
    heads = find_headings(lines)
    if not heads:
        return {}
    offsets = [0]
    for ln in lines:
        offsets.append(offsets[-1] + len(ln) + 1)
    best: dict[str, tuple[int, int, int, str]] = {}  # item -> (span_chars, start, end, title)
    for n, (idx, key, title) in enumerate(heads):
        if key not in TARGET_ITEMS:
            continue
        end_line = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        # a following heading of the same item (e.g. page-header repeats) must not truncate
        j = n + 1
        while j < len(heads) and heads[j][1] == key:
            j += 1
        end_line = heads[j][0] if j < len(heads) else len(lines)
        span = offsets[end_line] - offsets[idx]
        if key not in best or span > best[key][0]:
            best[key] = (span, idx, end_line, title)
    sections: dict[str, Section] = {}
    for key, (_span, start, end, title) in best.items():
        body = "\n".join(lines[start + 1 : end]).strip()
        sections[key] = Section(item=key, title=title or ITEM_TITLES[key], text=body)
    return sections
