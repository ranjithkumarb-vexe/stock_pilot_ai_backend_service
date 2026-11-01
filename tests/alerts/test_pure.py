"""Pure alert logic: materiality, detectors (risk-engine breaches, filings, moves, earnings), adaptive threshold."""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.modules.alerts import adaptive
from app.modules.alerts import materiality as mat
from app.modules.alerts.detectors import (
    AlertDraft,
    FilingSignal,
    daily_sigma,
    detect_breaches,
    detect_concentration,
    detect_earnings,
    detect_filings,
    detect_move,
    largest_move_note,
    merge_signals,
    violation_subject,
)
from app.modules.alerts.earnings import estimate_next
from app.modules.alerts.filings import items_from_title, parse_8k_signals
from app.modules.risk import Portfolio, Position, Rules, compute_metrics, current_violations

NOW = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)
D = Decimal


def portfolio(positions: list[tuple[str, str, int, int]], cash: int = 0) -> Portfolio:
    return Portfolio(D(cash), tuple(Position(s, sec, D(q), D(p)) for s, sec, q, p in positions), trades_7d=0)


RULES = Rules(pos=D(10), sec=D(25), dd=D(10), trades=3, cash=D(5))


# --------------------------------------------------------------------------------- materiality
def test_materiality_weights_sum_to_one_and_stay_in_unit_interval() -> None:
    assert pytest.approx(1.0) == mat.W_TYPE + mat.W_MAGNITUDE + mat.W_EXPOSURE + mat.W_RECENCY
    rng = random.Random(1)
    for _ in range(500):
        m = mat.materiality(rng.uniform(-1, 2), rng.uniform(-1, 2), rng.uniform(-1, 2), rng.uniform(-1, 2))
        assert 0.0 <= m <= 1.0
    assert mat.materiality(1, 1, 1, 1) == 1.0 and mat.materiality(0, 0, 0, 0) == 0.0


def test_high_impact_8k_items_outweigh_boilerplate() -> None:
    for item in ("2.02", "1.01", "5.02", "4.02", "2.06"):
        assert mat.ITEM_WEIGHTS[item] >= mat.HIGH_ITEM  # SPEC 7.7 "high" list
    assert (
        mat.filing_type_weight(["9.01"])
        < mat.filing_type_weight(["8.01"])
        < mat.filing_type_weight(["2.02", "9.01"])
    )
    assert mat.filing_type_weight([]) == mat.DEFAULT_ITEM_WEIGHT
    assert (
        mat.filing_magnitude(["2.02", "5.02"])
        > mat.filing_magnitude(["2.02"])
        > mat.filing_magnitude(["9.01"])
    )
    assert mat.split_items("2.02, 9.01") == ["2.02", "9.01"] and mat.split_items(None) == []


def test_exposure_watchlist_lower_than_holding_and_recency_decays() -> None:
    assert (
        mat.exposure(None, held=False) < mat.exposure(1.0, held=True) < mat.exposure(30.0, held=True) == 1.0
    )
    assert mat.recency(0) == 1.0 and mat.recency(60 * 24) == pytest.approx(0.3679, abs=1e-3)
    assert mat.recency(-5) == 1.0
    assert mat.move_magnitude(2.0, 2.0) == 0.0 and mat.move_magnitude(4.0, 2.0) == 1.0
    assert mat.breach_materiality(0) == 0.9 and mat.breach_materiality(50) == 1.0


# --------------------------------------------------------------------------------- breaches (risk engine)
def test_breach_alerts_come_from_the_risk_engine_verdicts() -> None:
    pf = portfolio(
        [
            ("JNJ", "Healthcare", 100, 150),
            ("KO", "Consumer Staples", 200, 70),
            ("PG", "Consumer Staples", 100, 150),
            ("MSFT", "Technology", 5, 400),
        ],
        cash=5_000,
    )
    metrics = compute_metrics(pf)
    verdicts = current_violations(metrics, RULES)
    drafts = detect_breaches(metrics, RULES, pf.positions, NOW)
    assert len(drafts) == len(verdicts) >= 2
    assert all(d.critical and d.type == "breach" for d in drafts)
    by_key = {d.payload["breach_key"]: d for d in drafts}
    assert "pos:JNJ" in by_key and "sec:Consumer Staples" in by_key
    pos = by_key["pos:JNJ"]
    assert (
        pos.title == "Position cap breached: JNJ"
        and pos.body == "JNJ is above your 10% maximum position rule."
    )
    assert pos.source_label == "Risk engine · rule R-POS" and pos.symbol == "JNJ"
    sec = by_key["sec:Consumer Staples"]
    assert (
        sec.title == "Sector rule breached: Consumer Staples"
        and sec.body == "Consumer Staples exceeds your 25% sector cap."
    )
    assert sec.symbol in {"KO", "PG"}  # largest holding of the sector, not the position-cap offender
    assert 0.9 <= pos.materiality <= 1.0
    assert pos.dedupe_key == f"breach:pos:JNJ:{NOW.date()}" and pos.suppress_key == "breach:pos:JNJ"


def test_no_breach_no_alert_and_subject_parsing_tracks_the_engine_message() -> None:
    pf = portfolio([(s, f"S{i}", 10, 100) for i, s in enumerate("ABCDEFGHIJKL")], cash=3000)
    metrics = compute_metrics(pf)
    assert detect_breaches(metrics, RULES, pf.positions, NOW) == []
    pf2 = portfolio([("AAA", "Big Sector Name", 100, 100), ("BBB", "Other", 1, 1)], cash=0)
    for v in current_violations(compute_metrics(pf2), RULES):
        if v.rule in ("pos", "sec"):
            assert violation_subject(v) in {"AAA", "BBB", "Big Sector Name", "Other"}


def test_drawdown_trades_and_cash_breaches_are_alerts_too() -> None:
    pf = Portfolio(
        D(0), (Position("AAA", "X", D(10), D(100)), Position("BBB", "Y", D(10), D(100))), trades_7d=9
    )
    metrics = compute_metrics(pf, equity_curve=[D(100), D(85)])
    rules = Rules(pos=D(60), sec=D(60), dd=D(10), trades=3, cash=D(5))
    drafts = {d.payload["rule"]: d for d in detect_breaches(metrics, rules, pf.positions, NOW)}
    assert set(drafts) == {"dd", "trades", "cash"}
    assert drafts["dd"].title == "Drawdown limit reached" and drafts["dd"].symbol is None
    assert drafts["trades"].title == "Weekly trade limit exceeded"
    assert drafts["cash"].title == "Cash buffer below your minimum"


def test_concentration_alert_threshold_is_hhi_025() -> None:
    conc = compute_metrics(portfolio([("AAA", "X", 90, 100), ("BBB", "Y", 10, 100)], cash=0))
    [d] = detect_concentration(conc, NOW)
    assert d.type == "concentration" and not d.critical and 0.5 < d.materiality <= 1
    assert "HHI" in d.body and d.dedupe_key.startswith("concentration:hhi:")
    spread = compute_metrics(portfolio([(s, f"S{i}", 10, 100) for i, s in enumerate("ABCDE")], cash=0))
    assert detect_concentration(spread, NOW) == []
    assert detect_concentration(compute_metrics(portfolio([], cash=100)), NOW) == []


# --------------------------------------------------------------------------------- filings (T-ALERT-01 pure)
def sig(
    source: str,
    acc: str = "0000320193-26-000123",
    symbol: str = "KO",
    minutes_ago: int = 3,
    items: tuple[str, ...] = ("2.02", "9.01"),
) -> FilingSignal:
    return FilingSignal(
        symbol, acc, items, NOW - timedelta(minutes=minutes_ago), source, "https://www.sec.gov/x"
    )


def test_same_8k_from_two_sources_is_one_alert() -> None:
    """T-ALERT-01."""
    signals = [
        sig("sec_edgar", minutes_ago=3),
        sig("raw_documents", minutes_ago=2),
        sig("finnhub_news", minutes_ago=1),
    ]
    merged = merge_signals(signals)
    assert len(merged) == 1 and merged[0][1] == ["finnhub_news", "raw_documents", "sec_edgar"]
    drafts = detect_filings(signals, names={"KO": "Coca-Cola"}, weights_pct={"KO": 9.0}, watch=set(), now=NOW)
    assert len(drafts) == 1
    d = drafts[0]
    assert d.dedupe_key == "filing:KO:0000320193-26-000123"
    assert d.title == "KO filed 8-K (Item 2.02): quarterly results"
    assert d.source_label == "SEC EDGAR 8-K + Finnhub news, ingested filings (3 sources, 1 alert)"
    assert d.event_at == NOW - timedelta(minutes=3)  # earliest publication = latency baseline
    assert d.payload["sources"] == ["finnhub_news", "raw_documents", "sec_edgar"]


def test_filing_for_an_untracked_ticker_is_ignored_and_watchlist_scores_lower() -> None:
    held = detect_filings([sig("sec_edgar")], names={}, weights_pct={"KO": 9.0}, watch=set(), now=NOW)[0]
    watch = detect_filings([sig("sec_edgar")], names={}, weights_pct={}, watch={"KO"}, now=NOW)[0]
    assert watch.materiality < held.materiality
    assert (
        detect_filings(
            [sig("sec_edgar", symbol="ZZZ")], names={}, weights_pct={"KO": 9.0}, watch={"AAPL"}, now=NOW
        )
        == []
    )
    assert (
        detect_filings([sig("sec_edgar")], names={}, weights_pct={"KO": 1.0}, watch=set(), now=NOW)[
            0
        ].source_label
        == "SEC EDGAR 8-K"
    )


def test_item_weights_drive_filing_materiality() -> None:
    results = detect_filings(
        [sig("sec_edgar", items=("2.02",))], names={}, weights_pct={"KO": 9.0}, watch=set(), now=NOW
    )[0]
    boiler = detect_filings(
        [sig("sec_edgar", acc="b", items=("9.01",))], names={}, weights_pct={"KO": 9.0}, watch=set(), now=NOW
    )[0]
    assert results.materiality > boiler.materiality + 0.2


def test_parse_submissions_payload_and_titles() -> None:
    payload = {
        "filings": {
            "recent": {
                "accessionNumber": ["0000021344-26-000100", "0000021344-26-000099", "0000021344-26-000050"],
                "form": ["8-K", "10-Q", "8-K"],
                "filingDate": ["2026-10-30", "2026-10-29", "2026-10-01"],
                "acceptanceDateTime": ["2026-10-30T14:57:12.000Z", "2026-10-29T10:00:00.000Z", ""],
                "items": ["2.02,9.01", "", "8.01"],
                "primaryDocument": ["ko-8k.htm", "ko-10q.htm", "ko-8k2.htm"],
            }
        }
    }
    out = parse_8k_signals(payload, "KO", "21344")
    assert [s.accession_no for s in out] == [
        "0000021344-26-000100",
        "0000021344-26-000050",
    ]  # the 10-Q is skipped
    assert out[0].published_at == datetime(2026, 10, 30, 14, 57, 12, tzinfo=UTC)
    assert out[0].items == ("2.02", "9.01") and out[0].url and out[0].url.endswith("ko-8k.htm")
    assert out[1].published_at == datetime(2026, 10, 1, tzinfo=UTC)  # no acceptance time: the filing date
    assert parse_8k_signals({}, "KO", "1") == []
    assert items_from_title("8-K (Items 2.02,9.01)") == ("2.02", "9.01") and items_from_title(None) == ()


# --------------------------------------------------------------------------------- moves
def closes(n: int = 80, daily: float = 0.01, seed: int = 3) -> list[float]:
    rng = random.Random(seed)
    px, out = 100.0, []
    for _ in range(n):
        px *= 1 + rng.gauss(0, daily)
        out.append(px)
    return out


def test_daily_sigma_needs_history_and_variance() -> None:
    assert daily_sigma(closes(20)) is None
    assert daily_sigma([100.0] * 80) is None  # zero variance
    s = daily_sigma(closes(200))
    assert s is not None and 0.007 < s < 0.014


def mv(**over: Any) -> AlertDraft | None:
    args: dict[str, Any] = {
        "symbol": "TSLA", "price": 106.8, "prev_close": 100.0, "sigma": 0.026, "k": 2.0, "quote_allows": True,
        "weight_pct": 12.0, "held": True, "now": NOW, "source": "alpaca", "freshness": "live",
    }  # fmt: skip
    args.update(over)
    return detect_move(**args)


def test_move_alert_fires_above_k_and_is_suppressed_when_stale_or_closed() -> None:
    d = mv()
    assert d is not None and d.title == "TSLA rose 6.8% (z = +2.6)" and d.type == "move" and not d.critical
    assert d.dedupe_key == f"move:TSLA:{NOW.date()}" and d.payload["z"] == 2.62
    assert mv(quote_allows=False) is None  # T-DATA-02: stale / closed
    assert mv(price=102.0) is None  # |z| below k
    fell = mv(price=93.2)
    assert fell is not None and "fell" in fell.title
    assert mv(sigma=None) is None and mv(prev_close=None) is None
    assert mv(sigma=0.0) is None and mv(prev_close=0) is None


def test_higher_k_suppresses_marginal_moves() -> None:
    assert mv(price=105.0, sigma=0.02, k=2.0) is not None  # z = 2.5
    assert mv(price=105.0, sigma=0.02, k=3.0) is None


def test_largest_move_note() -> None:
    today = date(2026, 10, 30)
    past = [(today - timedelta(days=d), 0.01) for d in range(1, 200)]
    assert largest_move_note(0.068, past, today) == "Largest one-day gain in about 7 months."
    assert largest_move_note(-0.04, past, today) is not None
    recent_big = [*past, (today - timedelta(days=10), 0.08)]
    assert largest_move_note(0.068, recent_big, today) is None
    assert largest_move_note(0.068, [], today) is None


# --------------------------------------------------------------------------------- earnings
def test_earnings_alert_window_and_wording() -> None:
    today = date(2026, 10, 29)
    d = detect_earnings(
        symbol="PG",
        name="Procter & Gamble",
        report_date=date(2026, 10, 31),
        today=today,
        qty=60,
        weight_pct=9.4,
        estimated=True,
        now=NOW,
    )
    assert d is not None and d.title == "PG reports earnings on Oct 31"
    assert "You hold 60 shares (9.4% of portfolio)." in d.body and "estimated" in d.body
    assert d.source_label == "Earnings calendar (estimated)" and d.dedupe_key == "earnings:PG:2026-10-31"
    now_d = detect_earnings(
        symbol="PG",
        name="P",
        report_date=today,
        today=today,
        qty=None,
        weight_pct=None,
        estimated=False,
        now=NOW,
    )
    assert now_d is not None and now_d.title == "PG reports earnings today"
    tomorrow = detect_earnings(
        symbol="PG",
        name="P",
        report_date=date(2026, 10, 30),
        today=today,
        qty=None,
        weight_pct=None,
        estimated=False,
        now=NOW,
    )
    assert (
        tomorrow is not None
        and tomorrow.title.endswith("tomorrow")
        and tomorrow.body == "P is on your watchlist."
    )
    assert (
        detect_earnings(
            symbol="PG",
            name="P",
            report_date=date(2026, 11, 20),
            today=today,
            qty=1,
            weight_pct=1,
            estimated=False,
            now=NOW,
        )
        is None
    )
    assert (
        detect_earnings(
            symbol="PG",
            name="P",
            report_date=date(2026, 10, 1),
            today=today,
            qty=1,
            weight_pct=1,
            estimated=False,
            now=NOW,
        )
        is None
    )


def test_earnings_estimate_from_results_filings() -> None:
    dates = [date(2026, 1, 29), date(2026, 4, 28), date(2026, 7, 29)]
    assert estimate_next(dates, date(2026, 10, 1)) == date(2026, 10, 27)
    assert (
        estimate_next([date(2025, 1, 1)], date(2026, 10, 1)) is not None
    )  # rolled forward by whole quarters
    assert estimate_next([], date(2026, 10, 1)) is None
    assert estimate_next([date(2026, 7, 29)], date(2026, 10, 1)) == date(2026, 10, 28)  # default 91-day gap


# --------------------------------------------------------------------------------- adaptive threshold (T-ALERT-03 pure)
def test_k_rises_with_fatigue_falls_with_ctr_and_stays_in_bounds() -> None:
    k = adaptive.K_START
    for n in range(1, 30):  # a stream of dismissals
        k = adaptive.next_k(k, adaptive.Counts(sent=n, opened=0, dismissed=n))
        assert adaptive.K_MIN <= k <= adaptive.K_MAX
    assert k == adaptive.K_MAX
    for n in range(1, 30):  # then the user starts opening everything
        k = adaptive.next_k(k, adaptive.Counts(sent=20, opened=15 + min(n, 4), dismissed=0))
        assert adaptive.K_MIN <= k <= adaptive.K_MAX
    assert k == adaptive.K_MIN


def test_k_needs_evidence_and_steps_by_a_quarter() -> None:
    assert adaptive.next_k(2.0, adaptive.Counts(sent=2, dismissed=2)) == 2.0  # < MIN_ACTED
    assert adaptive.next_k(2.0, adaptive.Counts(sent=3, dismissed=3)) == 2.25
    assert adaptive.next_k(2.0, adaptive.Counts(sent=10, opened=4, dismissed=2)) == 2.0  # neither extreme
    c = adaptive.Counts(sent=10, opened=2, dismissed=6, snoozed=2)
    assert adaptive.fatigue_index(c) == pytest.approx(0.8) and adaptive.click_through(c) == pytest.approx(0.2)
    assert (
        adaptive.fatigue_index(adaptive.Counts()) == 0.0 and adaptive.click_through(adaptive.Counts()) == 0.0
    )


def test_delivery_floor_follows_k() -> None:
    assert adaptive.min_materiality(2.0) == 0.30
    assert adaptive.min_materiality(3.0) == 0.50 and adaptive.min_materiality(1.5) == 0.20
    assert adaptive.min_materiality(9.0) == adaptive.MIN_MATERIALITY_HI


def test_alert_volume_drops_at_least_30_percent_when_k_rises_from_2_to_3() -> None:
    """The volume side of T-ALERT-03 on a synthetic return stream (Student-t-ish tails, 400 symbol-days)."""
    rng = random.Random(42)
    zs = [rng.gauss(0, 1) / max(0.4, (rng.gammavariate(3, 1 / 3)) ** 0.5) for _ in range(4000)]
    low = sum(abs(z) > 2.0 for z in zs)
    high = sum(abs(z) > 3.0 for z in zs)
    assert low > 0 and (low - high) / low >= 0.30


# --------------------------------------------------------------------------------- live submissions source (recorded payload)
async def test_edgar_submissions_source_polls_through_the_hardened_client(tmp_path: Any) -> None:
    import httpx

    from app.modules.alerts.filings import EdgarSubmissionsSource
    from app.modules.edgar.client import EdgarClient

    payload = {
        "filings": {
            "recent": {
                "accessionNumber": ["0000021344-26-000100", "0000021344-26-000050"],
                "form": ["8-K", "8-K"],
                "filingDate": ["2026-10-30", "2026-09-01"],
                "acceptanceDateTime": ["2026-10-30T14:57:12.000Z", "2026-09-01T10:00:00.000Z"],
                "items": ["2.02,9.01", "8.01"],
                "primaryDocument": ["a.htm", "b.htm"],
            }
        }
    }
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if "0000000404" in str(req.url):
            return httpx.Response(404)
        assert "StockPilot" in req.headers["user-agent"]
        return httpx.Response(200, json=payload)

    async def public(_host: str) -> list[str]:
        return ["23.1.2.3"]

    client = EdgarClient(
        "StockPilot test@example.com", transport=httpx.MockTransport(handler), resolver=public, cache_dir=tmp_path,
        max_retries=0,
    )  # fmt: skip
    src = EdgarSubmissionsSource(client, {"KO": "21344", "GONE": "404"}, max_age_s=60)
    got = await src.poll(["KO", "GONE", "NOCIK"], datetime(2026, 10, 1, tzinfo=UTC))
    assert [s.accession_no for s in got] == ["0000021344-26-000100"]  # older than `since` dropped
    assert got[0].source == "sec_edgar" and src.errors == 1 and len(seen) == 2
    await client.aclose()


async def test_edgar_submissions_source_loads_ciks_lazily() -> None:
    from app.modules.alerts.filings import EdgarSubmissionsSource

    async def ciks() -> dict[str, str]:
        return {"KO": "21344"}

    class NoClient:
        async def get_json(self, *_a: Any, **_k: Any) -> dict[str, Any]:
            return {}

    src = EdgarSubmissionsSource(NoClient(), ciks)  # type: ignore[arg-type]
    assert await src.poll(["KO"], NOW) == []
