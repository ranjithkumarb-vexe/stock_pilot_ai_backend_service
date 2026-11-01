"""Resolve company mentions ("NVDA", "Nvidia", "Johnson & Johnson") to universe symbols - in code."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.db import Connection

_SUFFIX = re.compile(
    r"\b(?:inc|corp|corporation|company|co|plc|ltd|holdings|group|platforms|communications|\.com)\b\.?",
    re.IGNORECASE,
)
_GENERIC = frozenset({"the", "general", "american", "united", "bank", "first", "new", "global"})
_WORD = re.compile(r"[A-Za-z][A-Za-z.&'-]*")


@dataclass(frozen=True, slots=True)
class Company:
    symbol: str
    name: str
    sector: str | None


def aliases_for(c: Company) -> set[str]:
    name = c.name.lower().replace("&", " and ")
    name = _SUFFIX.sub(" ", name)
    name = re.sub(r"[^a-z0-9 -]", "", name).strip()
    out = {re.sub(r"\s+", " ", name)}
    first = name.split()[0] if name.split() else ""
    if len(first) >= 4 and first not in _GENERIC:
        out.add(first)
    out.add(c.name.lower().replace("&", "and"))
    return {a for a in out if a}


async def load_universe(conn: Connection) -> list[Company]:
    rows = await conn.fetch("select symbol, name, sector from tickers where in_universe order by symbol")
    return [Company(r["symbol"], r["name"], r["sector"]) for r in rows]


def find_companies(message: str, universe: list[Company]) -> list[Company]:
    """Companies mentioned by upper-case ticker or by (first) name, in order of appearance, deduplicated."""
    found: dict[str, tuple[int, Company]] = {}
    by_symbol = {c.symbol: c for c in universe}
    for m in _WORD.finditer(message):
        tok = re.sub(r"['\u2019]s$", "", m.group(0)).strip(".'-&")
        if tok in by_symbol and (tok.isupper() and (len(tok) > 1 or tok == message.strip(" ?.!"))):
            found.setdefault(tok, (m.start(), by_symbol[tok]))
    low = message.lower().replace("&", "and")
    for c in universe:
        if c.symbol in found:
            continue
        for a in aliases_for(c):
            i = low.find(a)
            if i >= 0 and re.search(rf"(?<![a-z0-9]){re.escape(a)}(?![a-z0-9])", low):
                found[c.symbol] = (i, c)
                break
    return [c for _, c in sorted(found.values(), key=lambda t: t[0])]
