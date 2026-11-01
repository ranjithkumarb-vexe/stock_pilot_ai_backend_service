"""Built-in copy of the design's supported universe (CIKs from `design/sp-data.js`).

Stream A's seed owns the `tickers` table; this list is only a fallback so `make ingest-xbrl` works on an
empty database (`insert ... on conflict do nothing`) and so tests know real CIKs.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UniverseEntry:
    symbol: str
    name: str
    sector: str
    cik: str
    exchange: str


UNIVERSE: tuple[UniverseEntry, ...] = (
    UniverseEntry("NVDA", "NVIDIA Corp.", "Technology", "1045810", "NASDAQ"),
    UniverseEntry("AMD", "Advanced Micro Devices", "Technology", "2488", "NASDAQ"),
    UniverseEntry("TSLA", "Tesla Inc.", "Consumer Discretionary", "1318605", "NASDAQ"),
    UniverseEntry("META", "Meta Platforms", "Communication", "1326801", "NASDAQ"),
    UniverseEntry("AMZN", "Amazon.com", "Consumer Discretionary", "1018724", "NASDAQ"),
    UniverseEntry("AVGO", "Broadcom Inc.", "Technology", "1730168", "NASDAQ"),
    UniverseEntry("GOOGL", "Alphabet Inc.", "Communication", "1652044", "NASDAQ"),
    UniverseEntry("AAPL", "Apple Inc.", "Technology", "320193", "NASDAQ"),
    UniverseEntry("MSFT", "Microsoft Corp.", "Technology", "789019", "NASDAQ"),
    UniverseEntry("JNJ", "Johnson & Johnson", "Healthcare", "200406", "NYSE"),
    UniverseEntry("KO", "Coca-Cola Co.", "Consumer Staples", "21344", "NYSE"),
    UniverseEntry("PG", "Procter & Gamble", "Consumer Staples", "80424", "NYSE"),
    UniverseEntry("PEP", "PepsiCo Inc.", "Consumer Staples", "77476", "NASDAQ"),
    UniverseEntry("VZ", "Verizon Communications", "Communication", "732712", "NYSE"),
    UniverseEntry("XOM", "Exxon Mobil", "Energy", "34088", "NYSE"),
    UniverseEntry("JPM", "JPMorgan Chase", "Financials", "19617", "NYSE"),
)

BY_SYMBOL: dict[str, UniverseEntry] = {u.symbol: u for u in UNIVERSE}
