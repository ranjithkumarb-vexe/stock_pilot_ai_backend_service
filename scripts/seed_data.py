"""Seed content transcribed from the finalized UI design (`design/sp-data.js`).

Pure data (no I/O). Numbers are the design's illustrative values; prices are SIMULATED. Persona ids/emails come
from `app/core/personas.py`. `scripts/seed.py` turns this into rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

D = Decimal


@dataclass(frozen=True, slots=True)
class SeedTicker:
    symbol: str
    name: str
    sector: str | None
    cik: str  # 10-digit zero padded (EDGAR canonical form)
    industry: str
    exchange: str
    px: Decimal  # last price anchor (design `px`)
    ch: Decimal  # last-day change in percent (design `ch`)
    beta: Decimal  # design beta, drives the synthetic price generator
    in_universe: bool = True


def _t(
    symbol: str, name: str, sector: str, px: str, ch: str, cik: str, beta: str, industry: str, exchange: str
) -> SeedTicker:
    return SeedTicker(symbol, name, sector, cik.zfill(10), industry, exchange, D(px), D(ch), D(beta))


UNIVERSE: tuple[SeedTicker, ...] = (
    _t("NVDA", "NVIDIA Corp.", "Technology", "182.40", "2.14", "1045810", "1.72", "Semiconductors", "NASDAQ"),
    _t(
        "AMD",
        "Advanced Micro Devices",
        "Technology",
        "164.20",
        "-1.32",
        "2488",
        "1.90",
        "Semiconductors",
        "NASDAQ",
    ),
    _t(
        "TSLA",
        "Tesla Inc.",
        "Consumer Discretionary",
        "342.10",
        "3.05",
        "1318605",
        "2.30",
        "Automobile Manufacturers",
        "NASDAQ",
    ),
    _t(
        "META",
        "Meta Platforms",
        "Communication",
        "718.50",
        "0.62",
        "1326801",
        "1.25",
        "Interactive Media & Services",
        "NASDAQ",
    ),
    _t(
        "AMZN",
        "Amazon.com",
        "Consumer Discretionary",
        "228.90",
        "0.88",
        "1018724",
        "1.30",
        "Broadline Retail",
        "NASDAQ",
    ),
    _t(
        "AVGO", "Broadcom Inc.", "Technology", "352.30", "1.40", "1730168", "1.20", "Semiconductors", "NASDAQ"
    ),
    _t(
        "GOOGL",
        "Alphabet Inc.",
        "Communication",
        "281.40",
        "-0.35",
        "1652044",
        "1.05",
        "Interactive Media & Services",
        "NASDAQ",
    ),
    _t(
        "AAPL",
        "Apple Inc.",
        "Technology",
        "268.70",
        "0.41",
        "320193",
        "1.10",
        "Technology Hardware, Storage & Peripherals",
        "NASDAQ",
    ),
    _t(
        "MSFT",
        "Microsoft Corp.",
        "Technology",
        "521.60",
        "0.27",
        "789019",
        "0.90",
        "Systems Software",
        "NASDAQ",
    ),
    _t(
        "JNJ",
        "Johnson & Johnson",
        "Healthcare",
        "171.80",
        "-0.22",
        "200406",
        "0.50",
        "Pharmaceuticals",
        "NYSE",
    ),
    _t(
        "KO",
        "Coca-Cola Co.",
        "Consumer Staples",
        "69.40",
        "0.15",
        "21344",
        "0.55",
        "Soft Drinks & Non-alcoholic Beverages",
        "NYSE",
    ),
    _t(
        "PG",
        "Procter & Gamble",
        "Consumer Staples",
        "156.30",
        "-0.48",
        "80424",
        "0.40",
        "Household Products",
        "NYSE",
    ),
    _t(
        "PEP",
        "PepsiCo Inc.",
        "Consumer Staples",
        "148.60",
        "0.72",
        "77476",
        "0.50",
        "Soft Drinks & Non-alcoholic Beverages",
        "NASDAQ",
    ),
    _t(
        "VZ",
        "Verizon Communications",
        "Communication",
        "41.90",
        "-4.10",
        "732712",
        "0.35",
        "Integrated Telecommunication Services",
        "NYSE",
    ),
    _t("XOM", "Exxon Mobil", "Energy", "114.20", "0.35", "34088", "0.70", "Integrated Oil & Gas", "NYSE"),
    _t("JPM", "JPMorgan Chase", "Financials", "301.50", "0.19", "19617", "1.05", "Diversified Banks", "NYSE"),
    # 17th large cap (the design list has 16; TASKS BE-04 asks for >= 17). Illustrative price like the others.
    _t(
        "V",
        "Visa Inc.",
        "Financials",
        "345.20",
        "0.30",
        "1403161",
        "0.95",
        "Transaction & Payment Processing Services",
        "NYSE",
    ),
)

# Benchmark for the equity curve / beta. Not part of the supported universe (cannot be traded or watched).
BENCHMARK = SeedTicker(
    "SPY", "SPDR S&P 500 ETF Trust", None, "0000884394", "Exchange Traded Fund", "NYSE ARCA",
    D("690.00"), D("0.30"), D("1.00"), in_universe=False,
)  # fmt: skip

UNIVERSE_BY_SYMBOL: dict[str, SeedTicker] = {t.symbol: t for t in UNIVERSE}

# ---- onboarding content (design QUESTIONS / TIERS / TIER_RULES) --------------------------------

QUESTIONS: tuple[tuple[str, tuple[tuple[str, int], ...]], ...] = (
    ("Your portfolio falls 20% in one month. What do you do?",
     (("Sell everything", 1), ("Sell some to reduce risk", 2), ("Hold and wait", 4), ("Buy more at lower prices", 5))),
    ("Which one-year outcome range would you accept?",
     (("+4% best case, -2% worst case", 1), ("+8% / -8%", 2), ("+14% / -16%", 3), ("+25% / -30%", 5))),
    ("How stable is your income?",
     (("Variable; I may need this money soon", 1), ("Stable", 3), ("Very stable with regular surplus", 4))),
    ("A stock you own rises 30% in two weeks.",
     (("Sell all to lock in the gain", 1), ("Sell part", 2), ("Hold", 3), ("Add to the position", 5))),
    ("How would you feel about one stock being 20% of your portfolio?",
     (("Uncomfortable", 1), ("Acceptable for a high-conviction idea", 3), ("Comfortable", 5))),
    ("Have you been invested through a market decline?",
     (("No", 2), ("Yes, and I held", 3), ("Yes, and I bought more", 5))),
)  # fmt: skip

TIER_LABELS: tuple[str, ...] = ("", "Very conservative", "Conservative", "Balanced", "Growth", "Aggressive")

# percentages for pos/sec/dd/cash, integer trades per rolling 7 days (SPEC 7.6; identical to design TIER_RULES)
TIER_RULES: dict[int, dict[str, int]] = {
    1: {"pos": 10, "sec": 25, "dd": 10, "trades": 3, "cash": 10},
    2: {"pos": 10, "sec": 25, "dd": 10, "trades": 3, "cash": 10},
    3: {"pos": 15, "sec": 35, "dd": 15, "trades": 5, "cash": 5},
    4: {"pos": 25, "sec": 50, "dd": 25, "trades": 10, "cash": 0},
    5: {"pos": 25, "sec": 50, "dd": 25, "trades": 10, "cash": 0},
}

# ---- personas ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SeedOrder:
    days_ago: int
    symbol: str
    side: str
    qty: int
    price: Decimal


@dataclass(frozen=True, slots=True)
class SeedPersona:
    key: str
    tag: str
    tier: int
    horizon: str
    goals: tuple[str, ...]
    experience: str
    jargon: int
    depth: str
    excluded: tuple[str, ...]  # ESG exclusions
    tone: str
    metric: str
    language: str
    region: str
    section_order: tuple[str, ...]
    cash: Decimal  # cash AFTER all seeded fills (design `cash`)
    rules: dict[str, int]
    holdings: tuple[tuple[str, int, Decimal], ...]  # (symbol, qty, avg cost)
    watchlist: tuple[str, ...]
    interest: tuple[tuple[str, float], ...]
    arms: dict[str, tuple[int, int]]
    stats: dict[str, float]
    orders: tuple[SeedOrder, ...]
    alerts: tuple[dict[str, object], ...]
    memories: tuple[dict[str, object], ...]
    bias: tuple[tuple[str, str, str, str], ...]
    revealed_tier: float
    gap_days: int
    rejections: tuple[tuple[str, str, str], ...] = field(default=())  # (proposal label, symbol, reason chip)
    likes: tuple[str, ...] = field(default=())


def _o(*rows: tuple[int, str, str, int, str]) -> tuple[SeedOrder, ...]:
    return tuple(SeedOrder(d, s, side, q, D(p)) for d, s, side, q, p in rows)


def _h(*rows: tuple[str, int, str]) -> tuple[tuple[str, int, Decimal], ...]:
    return tuple((s, q, D(c)) for s, q, c in rows)


PRIYA = SeedPersona(
    key="priya",
    tag="Conservative income · 10-year horizon",
    tier=2, horizon="10+ years", goals=("Income", "Preservation"), experience="Beginner", jargon=1,
    depth="standard", excluded=("Tobacco",), tone="Plain language", metric="Dividend yield",
    language="English", region="US · USD",
    section_order=("bal", "risk", "moat", "val", "trend", "cat", "bb"),
    cash=D("22014"), rules={"pos": 10, "sec": 25, "dd": 10, "trades": 3, "cash": 10},
    holdings=_h(("JNJ", 62, "152.4"), ("KO", 130, "61.8"), ("PG", 60, "149.1"), ("VZ", 215, "39.6"),
                ("PEP", 55, "151.2"), ("JPM", 28, "248.3"), ("MSFT", 17, "468.0"), ("XOM", 70, "108.9"),
                ("AAPL", 24, "231.5")),
    watchlist=("JNJ", "KO", "PG", "VZ", "PEP", "JPM", "MSFT", "XOM", "AAPL", "GOOGL"),
    interest=(("Consumer Staples", 0.82), ("Healthcare", 0.70), ("Dividend", 0.90), ("Quality", 0.74),
              ("Financials", 0.50), ("Value", 0.45), ("Technology", 0.25), ("Growth", 0.20)),
    arms={"breach": (2, 1), "proposal": (3, 2), "filing": (4, 2), "move": (1, 4), "earnings": (3, 2),
          "mirror": (2, 2), "brief": (3, 2)},
    stats={"ytd": 6.8, "spy": 11.9, "vol": 11.4, "beta": 0.62, "dd": 3.1, "maxdd": 5.8, "turnover": 12,
           "win": 67, "real": 412, "revT": 2.2, "pgr": 0.02, "seed": 3, "amp": 0.012},
    orders=_o((3, "KO", "buy", 20, "69.1"), (18, "VZ", "buy", 40, "41.2"), (31, "PG", "buy", 10, "154.0"),
              (44, "JNJ", "buy", 12, "168.3"), (58, "XOM", "buy", 20, "112.4"), (73, "JPM", "sell", 8, "296.0")),
    alerts=(
        {"id": "A1", "type": "breach", "symbol": "JNJ", "crit": True, "mat": 0.95, "title": "Position cap breached: JNJ", "body": "JNJ is above your 10% maximum position rule.", "src": "Risk engine · rule R-POS", "age_min": 120},
        {"id": "A2", "type": "breach", "symbol": "KO", "crit": True, "mat": 0.90, "title": "Sector rule breached: Consumer Staples", "body": "Consumer Staples exceeds your 25% sector cap.", "src": "Risk engine · rule R-SEC", "age_min": 120},
        {"id": "A3", "type": "filing", "symbol": "KO", "crit": False, "mat": 0.72, "title": "KO filed 8-K (Item 2.02): quarterly results", "body": "Coca-Cola reported Q3 results and reaffirmed full-year guidance.", "src": "SEC EDGAR 8-K + Finnhub news (2 sources, 1 alert)", "age_min": 41},
        {"id": "A4", "type": "move", "symbol": "VZ", "crit": False, "mat": 0.66, "title": "VZ fell 4.1% (z = -2.3)", "body": "Largest one-day decline in 11 months, following the Q3 report.", "src": "Alpaca IEX · 15-min delayed", "age_min": 60},
        {"id": "A5", "type": "filing", "symbol": "XOM", "crit": False, "mat": 0.48, "title": "XOM declared its quarterly dividend", "body": "8-K Item 8.01: dividend unchanged at the prior quarter rate.", "src": "SEC EDGAR 8-K", "age_min": 180},
        {"id": "A6", "type": "earnings", "symbol": "PG", "crit": False, "mat": 0.55, "title": "PG reports Q1 on Oct 31", "body": "Earnings fall in 2 days. You hold 60 shares (9.4% of portfolio).", "src": "Earnings calendar", "age_min": 360},
        {"id": "A7", "type": "mirror", "symbol": None, "crit": False, "mat": 0.30, "title": "Weekly mirror: you made 1 trade in 7 days", "body": "Your rule allows 3 per week. Turnover over the last 12 months is 12%.", "src": "Behaviour detector · overtrading", "age_min": 5760},
    ),
    memories=(
        {"id": "m1", "kind": "semantic", "text": "Prefers dividend-paying large caps and wants income to be stable.", "conf": 0.9, "src": "Onboarding"},
        {"id": "m2", "kind": "semantic", "text": "Excludes tobacco companies.", "conf": 1.0, "src": "Onboarding"},
        {"id": "m3", "kind": "episodic", "text": "Expanded the Balance sheet & dividend section on 9 of the last 10 briefs.", "conf": 0.8, "src": "Events e-1004 to e-1031"},
        {"id": "m4", "kind": "procedural", "text": "Reads TL;DR first, then opens Risks. Skips Catalysts.", "conf": 0.7, "src": "Section dwell times"},
        {"id": "m5", "kind": "episodic", "text": 'Rejected one "reduce exposure" proposal for KO, reason: already know.', "conf": 0.6, "src": "Proposal P-1988"},
    ),
    bias=(
        ("Overtrading", "Trades per week", "1 (rule 3)", "ok"),
        ("Disposition effect", "PGR - PLR", "0.02 (flag at 0.10)", "ok"),
        ("Concentration", "HHI", "calc", "calc"),
        ("Herding", "Buys within 2 days of a spike", "8% (flag at 40%)", "ok"),
        ("Loss aversion", "Reject ratio, losers vs winners", "1.1 (flag at 2.0)", "ok"),
    ),
    revealed_tier=2.3, gap_days=0,
    rejections=(("P-1988", "KO", "already_know"),),
    likes=("Consumer Staples", "Healthcare", "Financials"),
)  # fmt: skip

ALEX = SeedPersona(
    key="alex",
    tag="Aggressive growth · 2-year horizon",
    tier=4, horizon="1-3 years", goals=("Growth",), experience="Intermediate", jargon=2,
    depth="deep", excluded=(), tone="Direct", metric="Revenue growth", language="English", region="US · USD",
    section_order=("trend", "cat", "val", "risk", "moat", "bal", "bb"),
    cash=D("7983"), rules={"pos": 25, "sec": 50, "dd": 20, "trades": 10, "cash": 0},
    holdings=_h(("NVDA", 219, "121.5"), ("AMD", 97, "158.9"), ("TSLA", 38, "372.4"), ("META", 14, "642.0"),
                ("AMZN", 31, "219.6"), ("AVGO", 17, "301.8")),
    watchlist=("NVDA", "AMD", "TSLA", "META", "AMZN", "AVGO", "GOOGL", "AAPL"),
    interest=(("Technology", 0.91), ("Growth", 0.88), ("Momentum", 0.72), ("Communication", 0.55),
              ("Consumer Discretionary", 0.50), ("Quality", 0.35), ("Dividend", 0.08), ("Healthcare", 0.10)),
    arms={"breach": (2, 1), "proposal": (2, 3), "filing": (3, 2), "move": (5, 1), "earnings": (4, 1),
          "mirror": (1, 3), "brief": (4, 1)},
    stats={"ytd": 24.5, "spy": 11.9, "vol": 48.3, "beta": 1.74, "dd": 18.2, "maxdd": 21.6, "turnover": 340,
           "win": 54, "real": 3180, "revT": 4.6, "pgr": 0.24, "seed": 11, "amp": 0.035},
    orders=_o((1, "TSLA", "buy", 10, "338.2"), (1, "AMD", "sell", 20, "166.4"), (2, "NVDA", "buy", 15, "178.9"),
              (3, "AMZN", "buy", 8, "226.7"), (3, "META", "sell", 4, "712.0"), (4, "AVGO", "buy", 5, "348.8"),
              (5, "TSLA", "sell", 6, "355.3"), (6, "AMD", "buy", 25, "159.1"), (6, "NVDA", "sell", 10, "176.0"),
              (12, "NVDA", "buy", 40, "171.2")),
    alerts=(
        {"id": "A1", "type": "breach", "symbol": "NVDA", "crit": True, "mat": 0.97, "title": "Position cap breached: NVDA", "body": "NVDA is above your 25% maximum position rule.", "src": "Risk engine · rule R-POS", "age_min": 60},
        {"id": "A2", "type": "breach", "symbol": "AMD", "crit": True, "mat": 0.92, "title": "Sector rule breached: Technology", "body": "Technology exceeds your 50% sector cap.", "src": "Risk engine · rule R-SEC", "age_min": 60},
        {"id": "A3", "type": "move", "symbol": "TSLA", "crit": False, "mat": 0.78, "title": "TSLA rose 6.8% (z = +2.6)", "body": "Largest one-day gain in 5 months on no company filing.", "src": "Alpaca IEX · 15-min delayed", "age_min": 34},
        {"id": "A4", "type": "filing", "symbol": "AMD", "crit": False, "mat": 0.74, "title": "AMD filed 8-K (Item 1.01): material agreement", "body": "Multi-year accelerator supply agreement with a cloud customer.", "src": "SEC EDGAR 8-K + Finnhub news (2 sources, 1 alert)", "age_min": 120},
        {"id": "A5", "type": "earnings", "symbol": "META", "crit": False, "mat": 0.70, "title": "META reports today after the close", "body": "You hold 14 shares (10.1% of portfolio).", "src": "Earnings calendar", "age_min": 300},
        {"id": "A6", "type": "earnings", "symbol": "NVDA", "crit": False, "mat": 0.52, "title": "NVDA reports Nov 19", "body": "Your largest position reports in 21 days.", "src": "Earnings calendar", "age_min": 300},
        {"id": "A7", "type": "mirror", "symbol": None, "crit": False, "mat": 0.58, "title": "Overtrading mirror: 9 trades in 7 days", "body": "Annualized turnover is 340%. The study average for households is 75%.", "src": "Behaviour detector · overtrading", "age_min": 180},
        {"id": "A8", "type": "mirror", "symbol": "TSLA", "crit": False, "mat": 0.50, "title": "Disposition mirror: winners sold early", "body": "You realized 31% of gains and 7% of losses (difference 0.24, flag at 0.10).", "src": "Behaviour detector · disposition", "age_min": 1440},
    ),
    memories=(
        {"id": "m1", "kind": "semantic", "text": "Targets growth and accepts large drawdowns; stated risk tier 4.", "conf": 0.6, "src": "Onboarding"},
        {"id": "m2", "kind": "episodic", "text": 'Rejected 2 "reduce exposure" proposals for NVDA with reason: disagree with thesis.', "conf": 0.8, "src": "Proposals P-3011, P-3052"},
        {"id": "m3", "kind": "procedural", "text": "Opens Catalysts first and spends most time on Trend & growth.", "conf": 0.85, "src": "Section dwell times"},
        {"id": "m4", "kind": "episodic", "text": "Bought TSLA within 2 days of a 5% move in 4 of the last 6 buys.", "conf": 0.7, "src": "Trade log"},
        {"id": "m5", "kind": "semantic", "text": "Prefers revenue growth over dividend metrics.", "conf": 0.75, "src": "Metric clicks"},
    ),
    bias=(
        ("Overtrading", "Trades per week", "9 (rule 10)", "watch"),
        ("Disposition effect", "PGR - PLR", "0.24 (flag at 0.10)", "flag"),
        ("Concentration", "HHI", "calc", "calc"),
        ("Herding", "Buys within 2 days of a spike", "67% (flag at 40%)", "flag"),
        ("Loss aversion", "Reject ratio, losers vs winners", "2.4 (flag at 2.0)", "flag"),
    ),
    revealed_tier=5.0, gap_days=17,
    rejections=(("P-3011", "NVDA", "disagree"), ("P-3052", "NVDA", "disagree")),
    likes=("Technology", "Communication", "Consumer Discretionary"),
)  # fmt: skip

# Sam: tier 3, 8-stock watchlist, ~$100k cash, no positions (dev persona for "fresh but onboarded" flows).
SAM = SeedPersona(
    key="sam",
    tag="Balanced · 3-10 year horizon",
    tier=3, horizon="3-10 years", goals=("Growth",), experience="Intermediate", jargon=2,
    depth="standard", excluded=(), tone="Plain language", metric="P/E", language="English", region="US · USD",
    section_order=("trend", "bal", "val", "risk", "moat", "cat", "bb"),
    cash=D("100000"), rules=dict(TIER_RULES[3]),
    holdings=(),
    watchlist=("AAPL", "MSFT", "GOOGL", "AMZN", "JNJ", "KO", "JPM", "XOM"),
    interest=(("Technology", 0.60), ("Healthcare", 0.40), ("Financials", 0.40), ("Growth", 0.60), ("Quality", 0.50)),
    arms=dict.fromkeys(("breach", "proposal", "filing", "move", "earnings", "mirror", "brief"), (1, 1)),
    stats={"ytd": 0.0, "spy": 11.9, "vol": 0.0, "beta": 0.0, "dd": 0.0, "maxdd": 0.0, "turnover": 0,
           "win": 0, "real": 0, "revT": 3.0, "pgr": 0.0, "seed": 5, "amp": 0.0},
    orders=(),
    alerts=(),
    memories=(
        {"id": "m1", "kind": "semantic", "text": "Stated risk tier 3 (Balanced) from the questionnaire.", "conf": 0.6, "src": "Onboarding"},
        {"id": "m2", "kind": "semantic", "text": "Goals: Growth. Horizon: 3-10 years.", "conf": 0.9, "src": "Onboarding"},
    ),
    bias=(
        ("Overtrading", "Trades per week", "No data yet", "none"),
        ("Disposition effect", "PGR - PLR", "Needs 10 sells", "none"),
        ("Concentration", "HHI", "calc", "calc"),
        ("Herding", "Buys near a spike", "No data yet", "none"),
        ("Loss aversion", "Reject ratio", "No data yet", "none"),
    ),
    revealed_tier=3.0, gap_days=0,
    likes=("Technology", "Healthcare", "Financials"),
)  # fmt: skip

PERSONA_SEEDS: dict[str, SeedPersona] = {p.key: p for p in (PRIYA, ALEX, SAM)}

# Design `interest` names that are factors/themes rather than GICS-like sectors.
FACTOR_INTERESTS = {"Dividend", "Quality", "Value", "Growth", "Momentum"}


def interest_dim(name: str) -> str:
    """`Technology` -> `sector:Technology`; `Dividend` -> `factor:dividend` (SPEC 10.3F dims)."""
    return f"factor:{name.lower()}" if name in FACTOR_INTERESTS else f"sector:{name}"
