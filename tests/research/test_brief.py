"""BE-14: brief generator end to end (MockProvider + real recorded XBRL + ingested chunks)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest

from app.core.clock import FakeClock
from app.core.pricesource import StaticPriceSource
from app.modules.edgar import ratios as r
from app.modules.edgar.xbrl import FactSet, parse_companyfacts
from app.modules.llm.client import LlmClient, client_for_tests
from app.modules.llm.providers.mock import MockProvider
from app.modules.llm.settings import LlmSettings
from app.modules.llm.trace import TraceRecorder
from app.modules.research.brief import templates
from app.modules.research.brief.claims import (
    BriefClaims,
    Claim,
    SectionClaims,
    count_unverified_numbers,
    literal_numbers,
    verify_brief,
    verify_claim,
)
from app.modules.research.brief.generate import generate_brief
from app.modules.research.brief.ledger import KEY_TO_ID, build_ledger
from app.modules.research.brief.style import (
    CONSERVATIVE_ORDER,
    GROWTH_ORDER,
    StyleCard,
    default_style_from_profile,
)
from tests.edgar.conftest import load_companyfacts_json
from tests.research.conftest import GOLDEN, clean_golden, load_jsonl, seed_golden

PRIYA = StyleCard(order=CONSERVATIVE_ORDER, jargon=1, depth="standard")
ALEX = StyleCard(order=GROWTH_ORDER, jargon=3, depth="deep")
NOW = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
AS_OF = NOW.date()
assert templates.NOT_ENOUGH  # module import registers the mock handler


@pytest.fixture
async def golden(db_admin: Any) -> AsyncIterator[Any]:
    await seed_golden(db_admin)
    yield db_admin
    await clean_golden(db_admin)


def llm_for(settings: Any, provider: MockProvider | None = None) -> LlmClient:
    return client_for_tests(settings, provider, clock=FakeClock(NOW), llm_settings=LlmSettings())


async def make_brief(
    db: Any,
    settings: Any,
    user_id: UUID,
    symbol: str,
    style: StyleCard,
    provider: MockProvider | None = None,
    price_source: Any = None,
) -> Any:
    rec = TraceRecorder(f"brief:{symbol}", user_id=user_id, clock=FakeClock(NOW))
    return await generate_brief(
        db, user_id=user_id, symbol=symbol, as_of=AS_OF, style=style, llm=llm_for(settings, provider),
        price_source=price_source, recorder=rec,
    )  # fmt: skip


def all_text(content: dict[str, Any]) -> str:
    parts = [c["text"] for s in content["sections"] for c in s["claims"]]
    return "\n".join([*parts, content["bull"]["text"], content["bear"]["text"]])


# ------------------------------------------------------------------ T-RAG-01 numeric golden set
def test_golden_set_has_30_questions_over_5_companies() -> None:
    qa = load_jsonl("numeric_qa.jsonl")
    assert len(qa) == 30 and {q["symbol"] for q in qa} == set(GOLDEN)


def test_t_rag_01_numeric_match_is_100_percent_with_accession_numbers() -> None:
    qa = load_jsonl("numeric_qa.jsonl")
    aliases = {
        "revenue": (r.REVENUE, "annual", "USD"),
        "net_income": (r.NET_INCOME, "annual", "USD"),
        "operating_cash_flow": (r.OCF, "annual", "USD"),
        "eps_diluted": (r.EPS_DILUTED, "annual", "USD/shares"),
        "equity": (r.EQUITY, "instant", "USD"),
        "operating_or_pretax_income": ((*r.OPERATING_INCOME, *r.PRETAX_INCOME), "annual", "USD"),
    }
    sets = {s: FactSet(parse_companyfacts(load_companyfacts_json(s))) for s in GOLDEN}
    misses = []
    for q in qa:
        al, kind, unit = aliases[str(q["metric"])]
        fact = sets[str(q["symbol"])].latest(list(al), kind=kind, unit=unit)
        ok = (
            fact is not None
            and fact.value == Decimal(str(q["answer"]))
            and fact.accession_no == q["accession_no"]
        )
        if not ok:
            misses.append(
                (q["id"], q["metric"], None if fact is None else (fact.value, fact.accession_no), q["answer"])
            )
    assert not misses, misses


async def test_ledger_values_equal_xbrl_and_cite_accession(golden: Any) -> None:
    qa = {(q["symbol"], q["metric"]): q for q in load_jsonl("numeric_qa.jsonl")}
    for sym in GOLDEN:
        ledger, _ = await build_ledger(golden, sym, AS_OF, None)
        rev = ledger.by_key("revenue")
        assert rev.value == Decimal(str(qa[(sym, "revenue")]["answer"])), sym
        assert rev.accession_nos == (qa[(sym, "revenue")]["accession_no"],)
        assert rev.source.startswith("us-gaap:") and rev.id == KEY_TO_ID["revenue"]
        assert ledger.by_key("price").value == Decimal(GOLDEN[sym])
        assert ledger.by_key("price").source.startswith("prices_daily close")


async def test_banks_show_not_meaningful_for_fcf_and_debt_to_equity(
    golden: Any, make_user: Any, settings: Any
) -> None:
    uid = await make_user()
    ledger, _ = await build_ledger(golden, "JPM", AS_OF, None)
    assert ledger.by_key("fcf").display == "n/m" and ledger.by_key("debt_to_equity").display == "n/m"
    built = await make_brief(golden, settings, uid, "JPM", PRIYA)
    bal = next(s for s in built.content["sections"] if s["key"] == "bal")
    assert any("not meaningful" in c["text"] for c in bal["claims"])
    assert not any("n/m" in c["text"] for c in bal["claims"])  # never prints a placeholder-less n/m sentence


# ------------------------------------------------------------------ T-RAG-02 / 03 / 05 over the golden set
async def test_t_rag_02_no_brief_in_the_golden_set_has_unverified_numbers(
    golden: Any, make_user: Any, settings: Any
) -> None:
    """Build-failing guard: every golden brief, every persona, re-scanned independently of the verifier."""
    uid = await make_user()
    for sym in GOLDEN:
        for style in (PRIYA, ALEX, StyleCard(depth="tldr")):
            built = await make_brief(golden, settings, uid, sym, style)
            assert built.status == "ok" and not built.used_safe_template, (sym, built.violations)
            assert built.content["citations_summary"]["unverified_numbers"] == 0, (sym, style)
            # independent re-scan of the rendered prose: strip the ledger displays + date labels, nothing may remain
            ledger, _ = await build_ledger(golden, sym, AS_OF, None)
            text = all_text(built.content)
            used = {i for i, e in ledger.entries.items() if e.display in text}
            assert count_unverified_numbers(text, ledger, used) == 0, (sym, text)


async def test_t_rag_03_citation_coverage_at_least_95_percent(
    golden: Any, make_user: Any, settings: Any
) -> None:
    uid = await make_user()
    for sym in GOLDEN:
        built = await make_brief(golden, settings, uid, sym, ALEX)
        assert built.content["citations_summary"]["coverage_pct"] >= 95.0
        facts = [
            c
            for s in built.content["sections"]
            for c in s["claims"]
            if "Not enough information" not in c["text"]
            and "not meaningful" not in c["text"]
            and "No recent Form 8-K" not in c["text"]
            and "not available" not in c["text"]
        ]
        assert facts and all(c["citation_ids"] for c in facts), sym


async def test_t_rag_05_unanswerable_sections_say_so(db_admin: Any, make_user: Any, settings: Any) -> None:
    await seed_golden(db_admin, ["KO"], chunks=False)  # numbers exist, no filing text at all
    try:
        uid = await make_user()
        built = await make_brief(db_admin, settings, uid, "KO", ALEX)
        qual = [s for s in built.content["sections"] if s["key"] in ("moat", "risk", "cat")]
        said = [
            s
            for s in qual
            if any(
                "Not enough information" in c["text"] or "No recent Form 8-K" in c["text"]
                for c in s["claims"]
            )
        ]
        assert len(said) / len(qual) >= 0.9
        assert built.content["stale"] is True and "10-K" in (built.content["stale_reason"] or "")
        assert (
            "Not enough information" in built.content["bear"]["text"]
            or "operating margin" in built.content["bear"]["text"]
        )
    finally:
        await clean_golden(db_admin, ["KO"])


# ------------------------------------------------------------------ T-PERS-07 persona difference
async def test_two_personas_same_ticker_order_differs_ledger_identical(
    golden: Any, make_user: Any, settings: Any
) -> None:
    uid = await make_user()
    a = await make_brief(golden, settings, uid, "NVDA", PRIYA)
    b = await make_brief(golden, settings, uid, "NVDA", ALEX)
    oa, ob = a.content["section_order"], b.content["section_order"]
    assert oa == CONSERVATIVE_ORDER and ob == GROWTH_ORDER
    assert sum(x != y for x, y in zip(oa, ob, strict=True)) / len(oa) >= 0.30
    assert a.content["ledger"] == b.content["ledger"]  # facts identical across users
    assert a.content["ledger"]["m1"]["display"].startswith("$215.9B")
    hints_a = [s["plain_hint"] for s in a.content["sections"]]
    hints_b = [s["plain_hint"] for s in b.content["sections"]]
    assert all(h for h in hints_a if h is not None) and any(hints_a) and not any(hints_b)
    na = sum(len(s["claims"]) for s in a.content["sections"])
    nb = sum(len(s["claims"]) for s in b.content["sections"])
    assert nb > na  # deep > standard
    assert a.content["style_card"] != b.content["style_card"]
    assert (
        StyleCard(**a.content["style_card"]).style_hash() != StyleCard(**b.content["style_card"]).style_hash()
    )


def test_default_style_from_profile() -> None:
    assert default_style_from_profile("Beginner", ["Income"]).order == CONSERVATIVE_ORDER
    s = default_style_from_profile("Beginner", ["Income"])
    assert (s.jargon, s.depth) == (1, "standard")
    g = default_style_from_profile("Intermediate", ["Growth"])
    assert g.order == GROWTH_ORDER and g.jargon == 2 and g.depth == "deep"
    assert default_style_from_profile(None, None).order[0] == "trend"
    assert StyleCard(order=["risk", "nonsense", "risk"]).order[:2] == ["risk", "trend"]


# ------------------------------------------------------------------ verifier rules (a)-(d)
def led() -> Any:
    from app.modules.research.brief.ledger import Ledger, LedgerEntry

    lg = Ledger("X", "X Corp", "Technology", date(2026, 1, 1), "FY2025")
    lg.entries["m1"] = LedgerEntry(
        "m1", "revenue", "Revenue", Decimal(5), "$5.0B", "usd", "us-gaap:Revenues", "xbrl"
    )
    return lg


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("Revenue grew 12% last year {{m1}}.", "(a)"),
        ("Revenue was {{m1}} versus $4.2B before.", "(a)"),
        ("Revenue was {{m9}}.", "(b)"),
        ("Revenue was {{m1}} and it will go up next year.", "(d)"),
        ("Our price target is {{m1}}.", "(d)"),
        ("You should buy this now {{m1}}.", "(d)"),
        ("Revenue was {{ m1 }} {{broken.", "(a)"),
    ],
)
def test_verifier_rejects_bad_claims(text: str, rule: str) -> None:
    c = Claim(text_template=text, citation_ids=["m1"])
    problems = verify_claim(c, led(), {"m1", "s1"}, "t")
    assert any(rule in p for p in problems), (text, problems)


def test_verifier_rule_c_requires_citation_and_notice_exemption_is_narrow() -> None:
    assert any(
        "(c)" in p
        for p in verify_claim(Claim(text_template="The company sells products."), led(), {"m1"}, "t")
    )
    assert any(
        "(c)" in p for p in verify_claim(Claim(text_template="X.", citation_ids=["s9"]), led(), {"m1"}, "t")
    )
    ok = Claim(text_template=templates.NOT_ENOUGH, kind="notice")
    assert verify_claim(ok, led(), set(), "t") == []
    fake = Claim(
        text_template="Revenue will definitely be great.", kind="notice"
    )  # model trying to dodge rule (c)
    assert any("notice" in p for p in verify_claim(fake, led(), set(), "t"))
    sneaky = Claim(text_template=templates.NOT_ENOUGH + " {{m1}}", kind="notice")
    assert verify_claim(sneaky, led(), set(), "t")


def test_allowed_literals_and_unverified_counter() -> None:
    assert literal_numbers("Filed 2026-02-01 for FY2025 under Items 1A and 7, Form 10-K, Q3 in 2025.") == []
    assert literal_numbers("Grew 12 percent in 3 segments") == ["12", "3"]
    lg = led()
    assert count_unverified_numbers("Revenue was $5.0B in 2025.", lg, {"m1"}) == 0
    assert count_unverified_numbers("Revenue was $5.0B and 77 units.", lg, {"m1"}) == 1


def test_verify_brief_covers_bull_and_bear() -> None:
    ok = Claim(text_template="Revenue was {{m1}}.", citation_ids=["m1"])
    bad = Claim(text_template="Up 5 percent.", citation_ids=["m1"])
    rep = verify_brief(
        BriefClaims(sections=[SectionClaims(key="trend", claims=[ok])], bull=ok, bear=bad), led(), {"m1"}
    )
    assert not rep.ok and any(v.startswith("bear") for v in rep.violations)


# ------------------------------------------------------------------ regenerate <= 2, then safe template
def bad_json() -> str:
    c = Claim(text_template="Revenue jumped 45 percent.", citation_ids=["m1"])
    return BriefClaims(sections=[SectionClaims(key="trend", claims=[c])], bull=c, bear=c).model_dump_json()


async def test_regenerates_twice_then_succeeds(golden: Any, make_user: Any, settings: Any) -> None:
    uid = await make_user()
    provider = MockProvider(script=[bad_json(), bad_json(), None])
    built = await make_brief(golden, settings, uid, "AAPL", PRIYA, provider)
    assert built.status == "ok" and built.verification_attempts == 3 and not built.used_safe_template
    assert (
        len(provider.calls) == 3 and provider.calls[1].hints["feedback"]
    )  # verifier feedback is passed back
    assert "rejected by the verifier" in provider.calls[1].messages[-1].content


async def test_three_bad_drafts_fall_back_to_safe_template(
    golden: Any, make_user: Any, settings: Any
) -> None:
    uid = await make_user()
    provider = MockProvider(script=[bad_json(), bad_json(), bad_json()])
    built = await make_brief(golden, settings, uid, "AAPL", PRIYA, provider)
    assert built.status == "degraded" and built.used_safe_template and built.verification_attempts == 3
    assert built.content["citations_summary"]["unverified_numbers"] == 0
    assert "45" not in all_text(built.content)
    trend = next(s for s in built.content["sections"] if s["key"] == "trend")
    assert "$416.2B" in trend["claims"][0]["text"]  # numbers still come from the ledger
    risk = next(s for s in built.content["sections"] if s["key"] == "risk")
    assert "Not enough information" in risk["claims"][0]["text"]  # no model prose in the safe template


async def test_t_chaos_01_llm_outage_degrades_brief_without_crash(
    golden: Any, make_user: Any, settings: Any
) -> None:
    from app.modules.llm.types import ProviderError

    uid = await make_user()
    provider = MockProvider(script=[ProviderError("HTTP 503", status=503, retriable=True)] * 30)
    built = await make_brief(golden, settings, uid, "JNJ", ALEX, provider)
    assert built.status == "degraded" and built.used_safe_template
    assert built.content["ledger"]["m1"]["display"] == "$94.2B"


# ------------------------------------------------------------------ price source
async def test_quote_from_price_source_beats_prices_daily_and_stale_sets_banner(
    golden: Any, make_user: Any, settings: Any
) -> None:
    uid = await make_user()
    ps = StaticPriceSource({"AAPL": Decimal("300.00")}, NOW, source="alpaca")
    built = await make_brief(golden, settings, uid, "AAPL", ALEX, price_source=ps)
    assert (
        built.content["ledger"]["m7"]["display"] == "$300.00"
        and "alpaca" in built.content["ledger"]["m7"]["source"]
    )
    # symbol the source does not know -> falls back to the last close
    built2 = await make_brief(golden, settings, uid, "JNJ", ALEX, price_source=ps)
    assert built2.content["ledger"]["m7"]["display"] == "$171.80"

    class StalePS(StaticPriceSource):
        async def get_last(self, symbol: str) -> Any:
            q = await super().get_last(symbol)
            return type(q)(symbol=q.symbol, price=q.price, ts=q.ts, source=q.source, freshness="stale")

    built3 = await make_brief(
        golden, settings, uid, "AAPL", ALEX, price_source=StalePS({"AAPL": Decimal(300)}, NOW)
    )
    assert built3.stale and "stale" in (built3.stale_reason or "")


# ------------------------------------------------------------------ API: cache, citations, trace, isolation
async def _user_with_style(db: Any, make_user: Any, style: StyleCard | None) -> UUID:
    uid: UUID = await make_user()
    if style is not None:
        await db.execute(
            "insert into user_features (user_id, key, value, confidence) values ($1, 'style_card', $2, 0.9)",
            uid, style.model_dump(),
        )  # fmt: skip
    return uid


async def test_api_brief_cache_regenerate_citations_trace_and_isolation(
    golden: Any, make_user: Any, auth_headers: Callable[..., dict[str, str]], app_factory: Any
) -> None:
    uid = await _user_with_style(golden, make_user, PRIYA)
    other = await _user_with_style(golden, make_user, ALEX)
    async with app_factory(clock=FakeClock(datetime.now(UTC))) as c:
        h = auth_headers(uid)
        r1 = await c.get("/v1/briefs/AAPL", headers=h)
        assert r1.status_code == 200, r1.text
        b1 = r1.json()
        assert set(b1) == {"id", "symbol", "name", "as_of", "generated_at", "status", "stale", "stale_reason", "style_card",
                           "section_order", "sections", "bull", "bear", "ledger", "citations_summary", "disclaimer", "trace_id"}  # fmt: skip
        assert (
            b1["section_order"] == CONSERVATIVE_ORDER
            and [s["key"] for s in b1["sections"]] == CONSERVATIVE_ORDER
        )
        assert b1["disclaimer"].startswith("StockPilot AI is an educational research")
        assert b1["status"] == "ok" and b1["citations_summary"]["unverified_numbers"] == 0
        titles = {s["key"]: (s["title"], s["source_label"]) for s in b1["sections"]}
        assert (
            titles["bal"] == ("Balance sheet & dividend", "XBRL + Item 7")
            and titles["risk"][1] == "Item 1A · Risk Factors"
        )
        assert set(b1["ledger"]["m1"]) == {"label", "value", "display", "unit", "source"}
        val = next(s for s in b1["sections"] if s["key"] == "val")
        assert [row["label"] for row in val["rows"]][:3] == ["Price", "Diluted EPS (last fiscal year)", "P/E"]

        r2 = await c.get("/v1/briefs/aapl", headers=h)  # symbol is case-insensitive; served from cache
        assert r2.json()["id"] == b1["id"]
        r3 = await c.post("/v1/briefs/AAPL/generate", headers=h)
        assert r3.status_code == 200 and r3.json()["id"] != b1["id"]
        assert r3.json()["ledger"] == b1["ledger"]
        # the other user (different style) gets their own brief with the same facts
        bo = (await c.get("/v1/briefs/AAPL", headers=auth_headers(other))).json()
        assert bo["id"] not in (b1["id"], r3.json()["id"]) and bo["ledger"] == b1["ledger"]
        assert bo["section_order"] == GROWTH_ORDER

        # citations resolve and carry provenance
        cited = {cid for s in r3.json()["sections"] for cl in s["claims"] for cid in cl["citation_ids"]}
        assert cited
        kinds = set()
        for cid in cited:
            cr = await c.get(f"/v1/citations/{cid}", headers=h)
            assert cr.status_code == 200, cr.text
            cj = cr.json()
            kinds.add(cj["kind"])
            assert cj["excerpt"] and cj["company"].startswith("Apple")
            if cj["kind"] == "chunk":
                assert (
                    len(cj["sha256"]) == 64
                    and cj["accession_no"]
                    and cj["url"].startswith("https://www.sec.gov/")
                )
            else:
                assert cj["rows"] and cj["title"] == "Verified metrics"
        assert {"chunk", "xbrl"} <= kinds
        assert (
            await c.get(f"/v1/citations/{next(iter(cited))}", headers=auth_headers(other))
        ).status_code == 404  # RLS
        assert (await c.get(f"/v1/citations/{uid}", headers=h)).status_code == 404

        # trace recorded with the pipeline steps
        row = await golden.fetchrow(
            "select id, outcome from traces where user_id = $1 and name = 'brief:AAPL' order by created_at desc limit 1",
            uid,
        )
        assert row is not None and row["outcome"] == "ok"
        steps = [
            s["name"]
            for s in await golden.fetch(
                "select name from trace_steps where trace_ref = $1 order by idx", row["id"]
            )
        ]
        assert (
            steps[:2] == ["ledger", "retrieve"]
            and "llm:brief_claims" in steps
            and "verify" in steps
            and steps[-1] == "render"
        )
        assert (
            await golden.fetchval(
                "select count(*) from llm_calls where user_id = $1 and job = 'brief_claims'", uid
            )
            >= 1
        )
        assert (
            await golden.fetchval(
                "select count(*) from audit_log where user_id = $1 and action = 'brief.generate'", uid
            )
            == 1
        )


async def test_api_errors(
    golden: Any, make_user: Any, auth_headers: Callable[..., dict[str, str]], app_factory: Any
) -> None:
    uid = await _user_with_style(golden, make_user, None)
    async with app_factory() as c:
        assert (await c.get("/v1/briefs/AAPL")).status_code == 401
        r = await c.get("/v1/briefs/ZZZZ", headers=auth_headers(uid))
        assert r.status_code == 422 and r.json()["code"] == "unsupported_symbol"
        assert (await c.get("/v1/briefs/..%2Fetc", headers=auth_headers(uid))).status_code in (404, 422)
        assert (await c.post("/v1/briefs/ZZZZ/generate", headers=auth_headers(uid))).json()[
            "code"
        ] == "unsupported_symbol"
        # no style card and no profile -> default card
        ok = await c.get("/v1/briefs/KO", headers=auth_headers(uid))
        assert ok.status_code == 200 and ok.json()["section_order"][0] == "trend"
        assert isinstance(httpx.Response(200, json=json.loads(ok.text)), httpx.Response)


async def test_api_concurrent_first_requests_do_not_collide(
    golden: Any, make_user: Any, auth_headers: Callable[..., dict[str, str]], app_factory: Any
) -> None:
    """Integration pass 1: the UI asks for the same brief from several widgets at once; the second request used
    to hit the unique key (500). Now it waits for the first and is served from the cache."""
    import asyncio

    uid = await _user_with_style(golden, make_user, PRIYA)
    async with app_factory(clock=FakeClock(datetime.now(UTC))) as c:
        h = auth_headers(uid)
        rs = await asyncio.gather(*(c.get("/v1/briefs/KO", headers=h) for _ in range(4)))
        assert [r.status_code for r in rs] == [200, 200, 200, 200], [r.text[:200] for r in rs]
        assert len({r.json()["id"] for r in rs}) == 1
