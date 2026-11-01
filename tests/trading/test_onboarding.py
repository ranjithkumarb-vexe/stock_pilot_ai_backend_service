"""BE-05: onboarding, profile, rules, watchlist, universe, export/reset/delete (FR-01, FR-02, FR-09, T-PERS-06)."""

from __future__ import annotations

import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from app.core.personas import PERSONAS
from app.modules.onboarding.content import QUESTIONS, TIER_LABELS, score_answers
from app.modules.risk import default_rules_for_tier
from scripts.seed_data import QUESTIONS as SEED_QUESTIONS
from scripts.seed_data import TIER_LABELS as SEED_LABELS
from scripts.seed_data import TIER_RULES

pytestmark = pytest.mark.db


async def login(
    client: httpx.AsyncClient, persona: str, email: str | None = None
) -> tuple[dict[str, str], UUID]:
    body: dict[str, Any] = {"persona": persona}
    if email:
        body["email"] = email
    r = await client.post("/v1/auth/dev-login", json=body)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, UUID(r.json()["user"]["user_id"])


def onboarding_body(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "answers": [4, 3, 3, 3, 3, 3],
        "horizon": "3-10 years",
        "goals": ["Growth"],
        "experience": "Intermediate",
        "likes": ["Technology"],
        "excludes": ["Energy"],
        "esg_exclusions": ["Tobacco"],
        "watchlist": ["AAPL", "MSFT", "JNJ"],
        "region": "US",
        "currency": "USD",
        "starting_cash": 100000,
        "disclaimer_ack": True,
    }
    body.update(over)
    return body


# ---------------------------------------------------------------------------- content + scoring


def test_content_matches_design_data() -> None:
    assert QUESTIONS == SEED_QUESTIONS
    assert TIER_LABELS == SEED_LABELS
    for t in range(1, 6):
        assert default_rules_for_tier(t).to_dict() == TIER_RULES[t]


@pytest.mark.parametrize(
    ("answers", "tier"),
    [
        ([1, 1, 1, 1, 1, 2], 1),  # mean 1.17
        ([2, 2, 1, 2, 1, 2], 2),  # mean 1.67 -> 2
        ([4, 3, 3, 3, 3, 3], 3),  # mean 3.17
        ([4, 2, 3, 3, 3, 3], 3),  # mean 3.0
        ([5, 5, 4, 5, 5, 5], 5),  # mean 4.83
        ([5, 2, 3, 5, 3, 2], 3),  # mean 3.33
        ([5, 3, 4, 5, 5, 3], 4),  # mean 4.17
        ([4, 5, 3, 5, 5, 3], 4),  # mean 4.17
        ([5, 5, 5, 5, 5, 5], 5),
        ([1, 2, 1, 2, 2, 3], 2),  # mean 1.83
    ],
)
def test_scoring_maps_mean_to_tier(answers: list[int], tier: int) -> None:
    assert score_answers(answers).tier == tier


@pytest.mark.parametrize(
    ("answers", "tier"), [([2, 2, 3, 2, 3, 3], 3), ([2, 3, 2, 3, 2, 3], 3), ([1, 2, 2, 3, 2, 2], 2)]
)
def test_scoring_rounds_half_up_like_the_design(answers: list[int], tier: int) -> None:
    # mean 2.5 -> JS Math.round gives 3 (Python's round() would give 2)
    assert score_answers(answers).tier == tier


def test_scoring_rejects_wrong_length() -> None:
    with pytest.raises(ValueError, match="6 answers"):
        score_answers([1, 2])


async def test_questions_endpoint_is_public_and_complete(tclient: httpx.AsyncClient) -> None:
    r = await tclient.get("/v1/onboarding/questions")
    assert r.status_code == 200
    j = r.json()
    assert len(j["questions"]) == 6
    assert j["questions"][0]["options"][0] == {"label": "Sell everything", "score": 1}
    assert [t["label"] for t in j["tiers"]] == list(TIER_LABELS[1:])
    assert j["tier_rules"]["3"] == {"pos": 15, "sec": 35, "dd": 15, "trades": 5, "cash": 5}
    assert j["horizons"] == ["Under 1 year", "1-3 years", "3-10 years", "10+ years"]
    assert "Technology" in j["sectors"] and j["disclaimer"].startswith("StockPilot AI is an educational")


async def test_universe_is_public_and_has_17_large_caps_in_7_sectors(tclient: httpx.AsyncClient) -> None:
    r = await tclient.get("/v1/universe")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) >= 17 and len({i["sector"] for i in items}) >= 6
    assert {"symbol", "name", "sector", "industry", "exchange"} <= set(items[0])
    assert "SPY" not in {i["symbol"] for i in items}  # benchmark is not tradeable


# ---------------------------------------------------------------------------- POST /onboarding


async def test_new_user_completes_onboarding(tclient: httpx.AsyncClient, db_admin: Any) -> None:
    h, uid = await login(tclient, "new")
    assert (await tclient.get("/v1/me", headers=h)).json()["onboarded"] is False
    r = await tclient.post("/v1/onboarding", headers=h, json=onboarding_body())
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["risk_tier"] == 3 and j["tier_label"] == "Balanced"
    assert j["rules"] == {"pos": 15, "sec": 35, "dd": 15, "trades": 5, "cash": 5}
    assert "tier 3 (Balanced)" in j["explanation"] and "maximum position 15%" in j["explanation"]
    assert [w["symbol"] for w in j["watchlist"]] == ["AAPL", "MSFT", "JNJ"]
    assert j["watchlist"][0]["quote"]["price"] > 0
    assert j["profile"]["likes"] == ["Technology"] and j["profile"]["excludes"] == ["Energy"]
    assert (await tclient.get("/v1/me", headers=h)).json()["onboarded"] is True

    # persisted: profile (ack time), rules, portfolio + ledger, learned state
    prof = await db_admin.fetchrow("select * from profiles where user_id = $1", uid)
    assert prof["risk_tier_stated"] == 3 and prof["disclaimer_ack_at"] is not None
    assert await db_admin.fetchval("select count(*) from risk_rules where user_id = $1", uid) == 5
    assert await db_admin.fetchval("select sum(amount) from cash_ledger where user_id = $1", uid) == Decimal(
        100000
    )
    assert await db_admin.fetchval("select count(*) from equity_snapshots where user_id = $1", uid) == 1
    assert await db_admin.fetchval("select count(*) from bandit_arms where user_id = $1", uid) == 7
    assert await db_admin.fetchval("select count(*) from memories where user_id = $1", uid) >= 3  # incl. ESG
    dims = {
        r["dim"]: r["weight"]
        for r in await db_admin.fetch("select dim, weight from user_interest_vector where user_id = $1", uid)
    }
    assert dims == {"sector:Technology": Decimal("0.6"), "factor:growth": Decimal("0.6")}
    # disclaimer ack + completion logged
    actions = {
        r["action"] for r in await db_admin.fetch("select action from audit_log where user_id = $1", uid)
    }
    assert {"disclaimer.ack", "onboarding.complete"} <= actions
    assert (
        await db_admin.fetchval(
            "select count(*) from events where user_id = $1 and type = 'onboarding_complete'", uid
        )
        == 1
    )


async def test_second_onboarding_is_409(tclient: httpx.AsyncClient) -> None:
    h, _ = await login(tclient, "new")
    assert (await tclient.post("/v1/onboarding", headers=h, json=onboarding_body())).status_code == 200
    r = await tclient.post("/v1/onboarding", headers=h, json=onboarding_body())
    assert r.status_code == 409 and r.json()["code"] == "conflict"


async def test_seeded_persona_cannot_onboard_again(tclient: httpx.AsyncClient) -> None:
    h, _ = await login(tclient, "priya")
    r = await tclient.post("/v1/onboarding", headers=h, json=onboarding_body())
    assert r.status_code == 409


async def test_unsupported_ticker_is_422_unsupported_symbol(tclient: httpx.AsyncClient) -> None:
    h, _uid = await login(tclient, "new")
    r = await tclient.post("/v1/onboarding", headers=h, json=onboarding_body(watchlist=["AAPL", "ZZZZ"]))
    assert r.status_code == 422 and r.json()["code"] == "unsupported_symbol"
    assert r.json()["details"]["symbol"] == "ZZZZ"
    assert (await tclient.get("/v1/me", headers=h)).json()["onboarded"] is False  # nothing half-created


@pytest.mark.parametrize(
    "over",
    [
        {"disclaimer_ack": False},
        {"answers": [1, 1, 1, 1, 1]},
        {"answers": [3, 1, 1, 1, 1, 1]},  # 3 is not an option of question 1
        {"goals": []},
        {"horizon": "tomorrow"},
        {"region": "IN"},
        {"currency": "INR"},
        {"likes": ["Technology"], "excludes": ["Technology"]},
        {"likes": ["Crypto"]},
        {"esg_exclusions": ["Nope"]},
        {"starting_cash": 5},
        {"watchlist": ["AAPL", "AAPL"]},
        {"extra": 1},
    ],
)
async def test_invalid_onboarding_is_rejected(tclient: httpx.AsyncClient, over: dict[str, Any]) -> None:
    h, _ = await login(tclient, "new")
    r = await tclient.post("/v1/onboarding", headers=h, json=onboarding_body(**over))
    assert r.status_code == 422 and r.json()["code"] in {"validation_error", "unsupported_symbol"}


async def test_onboarding_requires_auth(tclient: httpx.AsyncClient) -> None:
    assert (await tclient.post("/v1/onboarding", json=onboarding_body())).status_code == 401


async def test_income_goal_gets_dividend_defaults(tclient: httpx.AsyncClient) -> None:
    h, _ = await login(tclient, "new")
    await tclient.post(
        "/v1/onboarding", headers=h, json=onboarding_body(goals=["Income"], experience="Beginner")
    )
    p = (await tclient.get("/v1/profile", headers=h)).json()
    assert p["style_card"]["metric"] == "Dividend yield" and p["style_card"]["jargon"] == 1
    assert p["style_card"]["section_order"][0] == "bal"
    assert any(i["dim"] == "factor:dividend" for i in p["interests"])


# ---------------------------------------------------------------------------- GET/PATCH /profile


async def test_priya_profile_matches_the_design(
    tclient: httpx.AsyncClient, reseed: Callable[..., Any]
) -> None:
    await reseed("priya")
    h, _ = await login(tclient, "priya")
    p = (await tclient.get("/v1/profile", headers=h)).json()
    assert (
        p["stated_tier"] == 2 and 1.0 <= p["revealed_tier"] <= 5.0 and p["gap_days"] >= 0
    )  # revealed tier is computed by stream C
    assert p["profile"]["tier_label"] == "Conservative" and p["profile"]["goals"] == [
        "Income",
        "Preservation",
    ]
    assert p["profile"]["esg_exclusions"] == ["Tobacco"]
    assert p["rules"] == {"pos": 10, "sec": 25, "dd": 10, "trades": 3, "cash": 10}
    texts = {m["text"] for m in p["memories"]}
    assert len(p["memories"]) >= 5  # the 5 seeded ones (+ any auto-derived by stream C on recompute)
    assert "Prefers dividend-paying large caps and wants income to be stable." in texts
    assert {"id", "kind", "text", "confidence", "source"} == set(p["memories"][0])
    interests = {i["label"]: i["weight"] for i in p["interests"]}
    assert interests["Dividend"] == 0.9 and interests["Consumer Staples"] == 0.82
    assert [b["name"] for b in p["bias"]] == [
        "Overtrading",
        "Disposition effect",
        "Concentration",
        "Herding",
        "Loss aversion",
    ]
    conc = p["bias"][2]
    assert conc["state"] == "ok" and conc["value"].endswith("(flag at 0.25)")  # wording from stream C
    assert p["style_card"]["tone"] == "Plain language" and p["style_card"]["jargon"] == 1
    assert len(p["recent_events"]) == 10 and {"id", "ts", "type", "text"} == set(p["recent_events"][0])


async def test_alex_concentration_row_is_computed_live_and_flagged(
    tclient: httpx.AsyncClient, reseed: Callable[..., Any]
) -> None:
    await reseed("alex")
    h, _ = await login(tclient, "alex")
    p = (await tclient.get("/v1/profile", headers=h)).json()
    conc = next(b for b in p["bias"] if b["name"] == "Concentration")
    assert conc["state"] == "flag" and float(conc["value"].split()[0]) > 0.25
    states = {b["name"]: b["state"] for b in p["bias"]}
    assert set(states.values()) <= {"ok", "watch", "flag"} and states["Concentration"] == "flag"
    assert p["stated_tier"] == 4 and 1.0 <= p["revealed_tier"] <= 5.0 and p["gap_days"] >= 0


async def test_profile_requires_onboarding_and_auth(tclient: httpx.AsyncClient) -> None:
    assert (await tclient.get("/v1/profile")).status_code == 401
    h, _ = await login(tclient, "new")
    assert (await tclient.get("/v1/profile", headers=h)).status_code == 404


async def test_patch_profile_updates_fields_and_logs(tclient: httpx.AsyncClient, db_admin: Any) -> None:
    h, uid = await login(tclient, "new")
    await tclient.post("/v1/onboarding", headers=h, json=onboarding_body())
    r = await tclient.patch(
        "/v1/profile",
        headers=h,
        json={
            "horizon": "10+ years",
            "goals": ["Income"],
            "likes": ["Healthcare"],
            "tone": "Direct",
            "jargon": 3,
        },
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["profile"]["horizon"] == "10+ years" and j["profile"]["likes"] == ["Healthcare"]
    assert j["profile"]["excludes"] == ["Energy"]  # untouched
    assert j["style_card"]["tone"] == "Direct" and j["style_card"]["jargon"] == 3
    assert j["profile"]["risk_tier_stated"] == 3  # the stated tier is never edited here
    assert (
        await db_admin.fetchval(
            "select count(*) from audit_log where user_id = $1 and action = 'profile.update'", uid
        )
        == 1
    )
    assert (
        await db_admin.fetchval(
            "select count(*) from events where user_id = $1 and type = 'profile_edited'", uid
        )
        == 1
    )


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"likes": ["Crypto"]},
        {"likes": ["Technology"], "excludes": ["Technology"]},
        {"esg_exclusions": ["x"]},
        {"jargon": 9},
        {"risk_tier_stated": 1},
    ],
)
async def test_patch_profile_validation(tclient: httpx.AsyncClient, body: dict[str, Any]) -> None:
    h, _ = await login(tclient, "new")
    await tclient.post("/v1/onboarding", headers=h, json=onboarding_body())
    assert (await tclient.patch("/v1/profile", headers=h, json=body)).status_code == 422


# ---------------------------------------------------------------------------- rules


async def test_rule_edit_is_audited_and_emits_event(tclient: httpx.AsyncClient, db_admin: Any) -> None:
    h, uid = await login(tclient, "new")
    await tclient.post("/v1/onboarding", headers=h, json=onboarding_body())  # tier 3 defaults
    r = await tclient.put(
        "/v1/profile/rules", headers=h, json={"pos": 12, "sec": 35, "dd": 15, "trades": 5, "cash": 5}
    )
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["rules"]["pos"] == 12 and j["changed"] == [
        {"rule": "pos", "old": 15.0, "new": 12.0, "direction": "tighten"}
    ]
    assert j["warnings"] == []
    audit = await db_admin.fetch(
        "select details from audit_log where user_id = $1 and action = 'rule.edit'", uid
    )
    assert len(audit) == 1 and audit[0]["details"]["old"] == 15.0 and audit[0]["details"]["new"] == 12.0
    ev = await db_admin.fetch(
        "select entity, props from events where user_id = $1 and type = 'rule_edited'", uid
    )
    assert len(ev) == 1 and ev[0]["entity"] == {"rule": "pos"} and ev[0]["props"]["direction"] == "tighten"
    src = await db_admin.fetchval(
        "select source from risk_rules where user_id = $1 and rule_key = 'pos'", uid
    )
    assert src == "user"
    # restoring the tier default marks it as default again; unchanged rules write nothing
    r2 = await tclient.put(
        "/v1/profile/rules", headers=h, json={"pos": 15, "sec": 35, "dd": 15, "trades": 5, "cash": 5}
    )
    assert r2.json()["changed"][0]["direction"] == "loosen" and r2.json()["warnings"]
    assert (
        await db_admin.fetchval("select source from risk_rules where user_id = $1 and rule_key = 'pos'", uid)
        == "default"
    )
    r3 = await tclient.put(
        "/v1/profile/rules", headers=h, json={"pos": 15, "sec": 35, "dd": 15, "trades": 5, "cash": 5}
    )
    assert r3.json()["changed"] == []
    assert (await tclient.get("/v1/profile", headers=h)).json()["rules"]["pos"] == 15


async def test_loosening_the_cash_buffer_is_flagged_and_contradiction_warned(
    tclient: httpx.AsyncClient,
) -> None:
    h, _ = await login(tclient, "new")
    await tclient.post("/v1/onboarding", headers=h, json=onboarding_body())
    j = (
        await tclient.put(
            "/v1/profile/rules", headers=h, json={"pos": 30, "sec": 20, "dd": 15, "trades": 5, "cash": 0}
        )
    ).json()
    kinds = {c["rule"]: c["direction"] for c in j["changed"]}
    assert kinds == {"pos": "loosen", "sec": "tighten", "cash": "loosen"}
    assert len(j["warnings"]) == 3  # two loosened + sector < position contradiction


@pytest.mark.parametrize(
    "bad", [{"pos": 0}, {"pos": 101}, {"trades": 0}, {"cash": -1}, {"dd": 0}, {"sec": "x"}]
)
async def test_rule_validation(tclient: httpx.AsyncClient, bad: dict[str, Any]) -> None:
    h, _ = await login(tclient, "new")
    await tclient.post("/v1/onboarding", headers=h, json=onboarding_body())
    body = {"pos": 15, "sec": 35, "dd": 15, "trades": 5, "cash": 5, **bad}
    assert (await tclient.put("/v1/profile/rules", headers=h, json=body)).status_code == 422
    assert (await tclient.put("/v1/profile/rules", headers=h, json={"pos": 10})).status_code == 422


# ---------------------------------------------------------------------------- memories / reset / export / delete


async def test_forget_memory_and_tenant_isolation(
    tclient: httpx.AsyncClient, reseed: Callable[..., Any]
) -> None:
    await reseed("priya", "alex")
    ph, _ = await login(tclient, "priya")
    ah, _ = await login(tclient, "alex")
    before = (await tclient.get("/v1/profile", headers=ph)).json()["memories"]
    mem = next(m["id"] for m in before if m["source"] == "Onboarding")
    assert (await tclient.delete(f"/v1/profile/memories/{mem}", headers=ah)).status_code == 404  # not Alex's
    assert (await tclient.delete(f"/v1/profile/memories/{mem}", headers=ph)).status_code == 204
    assert (await tclient.delete(f"/v1/profile/memories/{mem}", headers=ph)).status_code == 404
    after = (await tclient.get("/v1/profile", headers=ph)).json()["memories"]
    assert mem not in {m["id"] for m in after}  # (stream C may add auto-derived memories on recompute)
    assert (await tclient.delete(f"/v1/profile/memories/{uuid4()}", headers=ph)).status_code == 404
    assert (await tclient.delete("/v1/profile/memories/not-a-uuid", headers=ph)).status_code == 422


async def test_reset_forgets_learning_but_keeps_profile_rules_portfolio(
    tclient: httpx.AsyncClient, reseed: Callable[..., Any], db_admin: Any
) -> None:
    await reseed("alex")
    h, uid = await login(tclient, "alex")
    before_cash = await db_admin.fetchval("select sum(amount) from cash_ledger where user_id = $1", uid)
    r = await tclient.post("/v1/profile/reset", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "reset"
    assert r.json()["cleared"]["memories"] == 5 and r.json()["cleared"]["user_features"] > 0
    n_events = await db_admin.fetchval("select count(*) from events where user_id = $1", uid)
    assert n_events > 5  # the event history is kept (only what was LEARNED from it is forgotten)
    p = (await tclient.get("/v1/profile", headers=h)).json()
    assert p["rules"]["pos"] == 25 and p["profile"]["risk_tier_stated"] == 4
    texts = " | ".join(m["text"] for m in p["memories"])
    assert (
        "Stated risk tier 4" in texts and "Opens Catalysts first" not in texts
    )  # onboarding memories back, learned ones gone
    assert any(m["source"] == "Onboarding" for m in p["memories"])
    assert (
        await db_admin.fetchval(
            "select count(*) from bandit_arms where user_id = $1 and alpha = 1 and beta = 1", uid
        )
        == 7
    )
    assert (
        await db_admin.fetchval("select sum(amount) from cash_ledger where user_id = $1", uid) == before_cash
    )
    assert await db_admin.fetchval("select count(*) from positions where user_id = $1", uid) == 6
    assert (
        await db_admin.fetchval(
            "select count(*) from audit_log where user_id = $1 and action = 'profile.reset'", uid
        )
        == 1
    )


async def test_export_is_valid_json_with_every_table_and_no_embeddings(
    tclient: httpx.AsyncClient, reseed: Callable[..., Any], db_admin: Any
) -> None:
    await reseed("priya")
    h, uid = await login(tclient, "priya")
    r = await tclient.get("/v1/profile/export", headers=h)
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    data = json.loads(r.content)
    assert data["format"] == "stockpilot-export/1" and data["user"]["id"] == str(uid)
    tables = data["tables"]
    expected = {
        r_["table_name"]
        for r_ in await db_admin.fetch(
            "select distinct table_name from information_schema.columns where table_schema = 'public' and column_name = 'user_id'"
        )
    } - {"stream_tokens"}
    assert set(tables) == expected
    assert len(tables["positions"]) == 9 and len(tables["fills"]) == 15 and len(tables["memories"]) == 5
    assert all("embedding" not in m for m in tables["memories"])
    assert all(row["user_id"] == str(uid) for t in ("positions", "fills", "orders") for row in tables[t])
    assert (await tclient.get("/v1/profile/export")).status_code == 401


async def test_t_pers_06_delete_account_removes_every_row(
    tclient: httpx.AsyncClient, reseed: Callable[..., Any], db_admin: Any
) -> None:
    """Export is valid BEFORE deletion; afterwards no table holds a row for the user (SQL-verified)."""
    await reseed("sam")
    h, uid = await login(tclient, "sam")
    # add learned state with an embedding so the cascade is proven for vectors too
    await db_admin.execute(
        "update memories set embedding = $2::vector where user_id = $1 and id = (select id from memories where user_id = $1 limit 1)",
        uid,
        "[" + ",".join(["0.01"] * 1024) + "]",
    )
    exported = json.loads((await tclient.get("/v1/profile/export", headers=h)).content)
    assert exported["tables"]["profiles"] and exported["tables"]["watchlist_items"]
    tables = [
        r_["table_name"]
        for r_ in await db_admin.fetch(
            "select distinct table_name from information_schema.columns where table_schema = 'public' and column_name = 'user_id'"
        )
    ]
    assert (
        await db_admin.fetchval(
            "select count(*) from memories where user_id = $1 and embedding is not null", uid
        )
        == 1
    )
    r = await tclient.request("DELETE", "/v1/account", headers=h, json={"confirm": "DELETE"})
    assert r.status_code == 204
    for t in tables:
        n = await db_admin.fetchval(f'select count(*) from "{t}" where user_id = $1', uid)
        assert n == 0, f"{t} still has {n} rows"
    assert await db_admin.fetchval("select count(*) from users where id = $1", uid) == 0
    await reseed("sam")  # restore for later tests


# ---------------------------------------------------------------------------- watchlist


async def test_watchlist_crud_with_quotes(tclient: httpx.AsyncClient, reseed: Callable[..., Any]) -> None:
    await reseed("sam")
    h, _ = await login(tclient, "sam")
    items = (await tclient.get("/v1/watchlist", headers=h)).json()["items"]
    assert [i["symbol"] for i in items] == ["AAPL", "MSFT", "GOOGL", "AMZN", "JNJ", "KO", "JPM", "XOM"]
    aapl = items[0]
    assert aapl["quote"]["price"] == 268.7 and aapl["quote"]["change_pct"] == pytest.approx(0.41, abs=0.01)
    assert {"symbol", "price", "prev_close", "change_pct", "ts", "source", "session", "freshness"} <= set(
        aapl["quote"]
    )
    put = await tclient.put("/v1/watchlist/nvda", headers=h)  # case-insensitive, idempotent
    assert put.status_code == 200 and put.json()["symbol"] == "NVDA"
    assert (await tclient.put("/v1/watchlist/NVDA", headers=h)).status_code == 200
    assert len((await tclient.get("/v1/watchlist", headers=h)).json()["items"]) == 9
    assert (await tclient.delete("/v1/watchlist/NVDA", headers=h)).status_code == 204
    assert (await tclient.delete("/v1/watchlist/NVDA", headers=h)).status_code == 204
    assert len((await tclient.get("/v1/watchlist", headers=h)).json()["items"]) == 8


async def test_watchlist_rejects_unsupported_and_malformed_symbols(tclient: httpx.AsyncClient) -> None:
    h, _ = await login(tclient, "sam")
    r = await tclient.put("/v1/watchlist/ZZZZ", headers=h)
    assert r.status_code == 422 and r.json()["code"] == "unsupported_symbol"
    assert (await tclient.put("/v1/watchlist/SPY", headers=h)).json()[
        "code"
    ] == "unsupported_symbol"  # benchmark
    assert (await tclient.put("/v1/watchlist/bad%20sym", headers=h)).status_code == 422
    assert (await tclient.get("/v1/watchlist")).status_code == 401


async def test_watchlist_cap(tclient: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.watchlist import service

    h, _ = await login(tclient, "sam")
    monkeypatch.setattr(service, "MAX_WATCHLIST", 8)
    r = await tclient.put("/v1/watchlist/NVDA", headers=h)
    assert r.status_code == 422 and r.json()["code"] == "validation_error"


async def test_watchlists_are_per_user(tclient: httpx.AsyncClient) -> None:
    sh, _ = await login(tclient, "sam")
    ph, _ = await login(tclient, "priya")
    s = {i["symbol"] for i in (await tclient.get("/v1/watchlist", headers=sh)).json()["items"]}
    p = {i["symbol"] for i in (await tclient.get("/v1/watchlist", headers=ph)).json()["items"]}
    assert s != p and "KO" in s and "GOOGL" in p


# ---------------------------------------------------------------------------- supabase admin helper


async def test_supabase_admin_delete_uses_service_key_and_tolerates_404() -> None:
    import respx

    from app.core.config import Settings
    from app.modules.profile.supabase_admin import configured, delete_auth_user

    uid = PERSONAS["priya"].user_id
    unconfigured = Settings(supabase_url="", supabase_service_role_key="")
    assert not configured(unconfigured) and await delete_auth_user(unconfigured, uid) is False
    s = Settings(supabase_url="https://proj.supabase.co", supabase_service_role_key="svc-key")
    with respx.mock(assert_all_called=True) as mock:
        route = mock.delete(f"https://proj.supabase.co/auth/v1/admin/users/{uid}").respond(204)
        assert await delete_auth_user(s, uid) is True
        assert route.calls[0].request.headers["authorization"] == "Bearer svc-key"
    with respx.mock() as mock:
        mock.delete(f"https://proj.supabase.co/auth/v1/admin/users/{uid}").respond(404)
        assert await delete_auth_user(s, uid) is True
    with respx.mock() as mock:
        mock.delete(f"https://proj.supabase.co/auth/v1/admin/users/{uid}").respond(500)
        with pytest.raises(Exception, match="500"):
            await delete_auth_user(s, uid)
