"""BE-20: the eval runner computes pass/fail itself (a failing fixture is a fail) and `GET /v1/evals/latest` serves it."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from evals import persona_sim
from evals.checks import PYTEST_CHECKS, build_specs
from evals.framework import (
    EvalSpec,
    Measurement,
    NotMeasured,
    Threshold,
    judge,
    run_all,
    write_results,
)


async def _m(values: dict[str, float], display: str = "x") -> Measurement:
    return Measurement(values, display)


def spec(eid: str, thresholds: dict[str, Threshold], values: dict[str, float]) -> EvalSpec:
    async def run(_: Any) -> Measurement:
        return await _m(values, f"actual {values}")

    return EvalSpec(eid, f"fixture {eid}", thresholds, run)


async def test_failing_fixture_is_reported_as_fail_and_thresholds_not_met() -> None:
    res = await run_all(
        [
            spec("OK-1", {"r": Threshold("ge", 0.9)}, {"r": 0.95}),
            spec("BAD-1", {"r": Threshold("ge", 0.9)}, {"r": 0.50}),
            spec("BAD-2", {"n": Threshold("eq", 0)}, {"n": 3}),
        ],
        None,
    )
    st = {i["id"]: i["status"] for i in res["items"]}
    assert st == {"OK-1": "pass", "BAD-1": "fail", "BAD-2": "fail"}
    assert res["thresholds_met"] is False and res["summary"] == {
        "passed": 1, "failed": 2, "not_measured": 0, "measured": 3,
    }  # fmt: skip
    bad = next(i for i in res["items"] if i["id"] == "BAD-1")
    assert bad["target"] == ">= 0.9" and bad["actual"].startswith("actual")


async def test_all_pass_means_thresholds_met_and_nothing_passes_by_default() -> None:
    res = await run_all([spec("A", {"r": Threshold("le", 2)}, {"r": 1})], None)
    assert res["thresholds_met"] is True
    assert (await run_all([], None))["thresholds_met"] is False  # no evidence is not a pass
    no_thr = EvalSpec("NT", "no thresholds", {}, lambda _: _m({"x": 1}))
    assert (await run_all([no_thr], None))["items"][0]["status"] == "fail"
    missing = spec("MISS", {"r": Threshold("ge", 1)}, {"other": 5})
    assert (await run_all([missing], None))["items"][0]["status"] == "fail"


async def test_crashing_check_is_a_fail_and_not_measured_is_listed_separately() -> None:
    async def boom(_: Any) -> Measurement:
        raise RuntimeError("database exploded")

    async def na(_: Any) -> NotMeasured:
        return NotMeasured("needs a live model", "owner")

    res = await run_all(
        [
            EvalSpec("CRASH", "c", {"r": Threshold("eq", 1)}, boom),
            EvalSpec("NA", "n", {"r": Threshold("eq", 1)}, na),
        ],
        None,
        extra_not_measured=[{"id": "X", "reason": "later"}],
    )
    assert res["items"][0]["status"] == "fail" and "database exploded" in res["items"][0]["actual"]
    assert [n["id"] for n in res["not_measured"]] == ["X", "NA"]
    assert res["summary"]["measured"] == 1 and res["thresholds_met"] is False


@pytest.mark.parametrize(
    ("op", "bound", "x", "ok"),
    [("ge", 0.9, 0.9, True), ("ge", 0.9, 0.89, False), ("le", 2, 2, True), ("eq", 0, 1, False), ("gt", 0, 0, False), ("lt", 3, 2, True), ("ge", 1, float("nan"), False)],
)  # fmt: skip
def test_threshold_semantics(op: Any, bound: float, x: float, ok: bool) -> None:
    assert Threshold(op, bound).met(x) is ok


def test_judge_needs_every_named_value() -> None:
    s = spec("J", {"a": Threshold("ge", 1), "b": Threshold("le", 1)}, {})
    assert not judge(s, Measurement({"a": 1}, ""))
    assert judge(s, Measurement({"a": 1, "b": 0}, ""))


def test_write_results_is_atomic_and_round_trips(tmp_path: Path) -> None:
    p = write_results({"generated_at": "2026-11-01T00:00:00Z", "items": []}, tmp_path / "sub" / "latest.json")
    assert json.loads(p.read_text())["items"] == [] and not list(p.parent.glob("*.tmp"))


def test_spec_catalogue_is_complete_and_unique() -> None:
    specs = build_specs()
    ids = [s.id for s in specs]
    assert len(ids) == len(set(ids)) and all(s.thresholds for s in specs)
    for must in ("T-RAG-01", "T-RAG-02", "T-RAG-03", "T-RAG-04", "T-RAG-05", "T-RAG-06", "T-SEC-01", "T-SEC-02", "T-PERS-02", "T-PERS-03", "T-PERS-07"):  # fmt: skip
        assert must in ids
    assert set(PYTEST_CHECKS) <= set(ids)


# ------------------------------------------------------------------------------------------ persona simulator
def test_persona_sim_is_deterministic_and_personalisation_beats_baseline() -> None:
    a = persona_sim.run_simulation(seeds=range(3), steps=60)
    b = persona_sim.run_simulation(seeds=range(3), steps=60)
    assert a == b
    assert len(a["personas"]) == 6 and a["steps"] == 60
    assert a["aggregate"]["ndcg5_lift"] > 0.1 and a["bot"] == {
        "critical_first_rate": 1.0,
        "posterior_bounded": True,
    }
    for p in a["personas"]:
        assert 0 <= p["personalised"]["ndcg5"] <= 1 and 0 <= p["personalised"]["mrr"] <= 1


def test_persona_sim_runs_on_the_documented_stub_ranker_too() -> None:
    out = persona_sim.run_simulation(seeds=range(2), steps=60, force_stub=True)
    assert out["ranker"] == "stub" and out["aggregate"]["ndcg5_lift"] > 0
    assert out["bot"]["critical_first_rate"] == 1.0 and out["bot"]["posterior_bounded"]


def test_ranking_metrics() -> None:
    assert persona_sim.ndcg_at_k([1, 1, 0, 0, 0], [1, 1, 0, 0, 0, 0]) == pytest.approx(1.0)
    assert persona_sim.ndcg_at_k([0, 0, 0, 0, 0], [1, 0, 0, 0, 0]) == 0.0
    assert persona_sim.ndcg_at_k([0, 0], [0, 0]) == 1.0  # nothing relevant to find
    assert persona_sim.mrr([0, 0, 1]) == pytest.approx(1 / 3) and persona_sim.mrr([0, 0]) == 0.0


# ------------------------------------------------------------------------------------------ GET /v1/evals/latest
def results_file(tmp: Path, ok: bool = True) -> Path:
    p = tmp / "latest.json"
    p.write_text(
        json.dumps(
            {
                "generated_at": "2026-11-01T12:00:00Z",
                "thresholds_met": ok,
                "items": [
                    {
                        "id": "T-RAG-01",
                        "description": "d",
                        "target": "100%",
                        "actual": "30 / 30",
                        "status": "pass",
                    },
                    {
                        "id": "T-X",
                        "description": "d2",
                        "target": ">= 1",
                        "actual": "0",
                        "status": "fail",
                        "detail": {"k": 1},
                    },
                ],
                "not_measured": [{"id": "T-PERF-01", "reason": "later"}],
            }
        )
    )
    return p


async def test_endpoint_404_without_a_results_file(
    app_client: httpx.AsyncClient, auth_headers: Callable[..., dict[str, str]], make_user: Any,
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:  # fmt: skip
    monkeypatch.setenv("EVALS_RESULTS_PATH", str(tmp_path / "missing.json"))
    h = auth_headers(await make_user())
    r = await app_client.get("/v1/evals/latest", headers=h)
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    assert (await app_client.get("/v1/evals/latest")).status_code == 401


async def test_endpoint_serves_the_file_verbatim_including_failures(
    app_client: httpx.AsyncClient, auth_headers: Callable[..., dict[str, str]], make_user: Any,
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:  # fmt: skip
    monkeypatch.setenv("EVALS_RESULTS_PATH", str(results_file(tmp_path, ok=False)))
    r = await app_client.get("/v1/evals/latest", headers=auth_headers(await make_user()))
    body = r.json()
    assert (
        r.status_code == 200
        and body["thresholds_met"] is False
        and body["generated_at"] == "2026-11-01T12:00:00Z"
    )
    assert [(i["id"], i["status"]) for i in body["items"]] == [("T-RAG-01", "pass"), ("T-X", "fail")]
    assert set(body["items"][0]) >= {"id", "description", "target", "actual", "status"}
    assert body["not_measured"][0]["id"] == "T-PERF-01"


async def test_endpoint_rejects_a_corrupt_or_invalid_file(
    app_client: httpx.AsyncClient, auth_headers: Callable[..., dict[str, str]], make_user: Any,
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:  # fmt: skip
    h = auth_headers(await make_user())
    bad = tmp_path / "bad.json"
    monkeypatch.setenv("EVALS_RESULTS_PATH", str(bad))
    bad.write_text("{not json")
    assert (await app_client.get("/v1/evals/latest", headers=h)).status_code == 503
    bad.write_text(json.dumps({"generated_at": "x", "thresholds_met": True, "items": [{"id": "a", "description": "b", "target": 1, "actual": 1, "status": "passed"}]}))  # fmt: skip
    assert (await app_client.get("/v1/evals/latest", headers=h)).status_code == 503


def test_chat_qa_dataset_shape() -> None:
    from evals.runtime import load_jsonl

    rows = load_jsonl("chat_qa.jsonl")
    assert len({r["id"] for r in rows}) == len(rows) >= 40
    assert {r["expect"] for r in rows} == {"grounded", "not_found"}
    assert sum(r["expect"] == "not_found" for r in rows) >= 20
