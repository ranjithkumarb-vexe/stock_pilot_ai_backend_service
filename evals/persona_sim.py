"""Personalisation simulator (SPEC 10.8 / 12.5): 6 scripted personas x 200 steps, personalised vs non-personalised.

Ranker under test: the production ranking API `app.modules.ranking` (BE-16: `scoring.rank`, `updated_arms`,
`bandit.cold_start_priors`, `metrics`). If that module cannot be imported the simulator falls back to `StubRanker`
below - a minimal re-statement of SPEC 10.6 (0.5 materiality + 0.3 Thompson + 0.2 interest, bounded Beta priors,
pinned criticals) and the output says so (`ranker: "stub"`). The stub is NOT the shipped ranker.

Each step draws a fresh pool of 10 candidates, ranks it twice (personalised = bandit arms learned so far;
baseline = no learning), shows the top 5, and lets the persona react using its hidden preferences:
click liked items, dismiss disliked ones with probability `dismiss_p`, decide on proposals. Only reactions
(events) update the personalised bandit. Metrics are computed on the personalised top 5 and, paired on the same
pool, on the baseline top 5:

* NDCG@5 and MRR against the persona's ground-truth likes (ideal order from the full pool);
* Brier score of the predicted click probability (arm posterior mean) vs the actual click;
* time-to-personalization = events until the 5-step moving NDCG@5 reaches 90% of its plateau (last 50 steps);
* approve-rate of proposals shown in the top 5; fatigue index = share of shown items the persona dismissed.

Deterministic for a given seed list. Run: `uv run python -m evals.persona_sim` (prints JSON).
"""

from __future__ import annotations

import json
import math
import random
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from statistics import mean
from typing import Any, Protocol

T0 = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)
SECTORS = (
    "Technology",
    "Consumer Staples",
    "Healthcare",
    "Financials",
    "Energy",
    "Communication",
    "Consumer Discretionary",
    "Utilities",
)
ITEM_ARMS = ("filing", "move", "earnings", "proposal")
STEPS = 200
SHOW = 5
POOL = 10


# --------------------------------------------------------------------------------------- personas
@dataclass(frozen=True, slots=True)
class Persona:
    id: str
    name: str
    likes: frozenset[str]
    tier: int
    p_click_liked: float = 0.8
    p_click_other: float = 0.05
    dismiss_p: float = 0.5
    p_approve_liked: float = 0.85
    p_approve_other: float = 0.10
    drift_at: int | None = None
    drift_likes: frozenset[str] = frozenset()
    bot: bool = False


PERSONAS: tuple[Persona, ...] = (
    Persona(
        "P1", "Dividend investor (Priya-like)", frozenset({"Consumer Staples", "Utilities", "Healthcare"}), 2
    ),
    Persona("P2", "Growth / AI (Alex-like)", frozenset({"Technology", "Communication"}), 4),
    Persona(
        "P3",
        "Balanced beginner (Sam-like)",
        frozenset({"Financials", "Healthcare", "Consumer Discretionary"}),
        3,
        p_click_liked=0.65,
        dismiss_p=0.3,
    ),
    Persona("P4", "Energy & income", frozenset({"Energy", "Utilities"}), 3),
    Persona(
        "P5",
        "Drifter: dividends to AI",
        frozenset({"Consumer Staples", "Utilities"}),
        3,
        drift_at=20,
        drift_likes=frozenset({"Technology", "Communication"}),
    ),
    Persona(
        "P6",
        "Bot: rejects everything",
        frozenset(),
        3,
        p_click_liked=0.0,
        p_click_other=0.0,
        dismiss_p=1.0,
        p_approve_liked=0.0,
        p_approve_other=0.0,
        bot=True,
    ),
)


# --------------------------------------------------------------------------------------- rankers
@dataclass(frozen=True, slots=True)
class Item:
    id: str
    arm: str
    sector: str
    materiality: float
    critical: bool = False


@dataclass(slots=True)
class Shown:
    item: Item
    p_click: float  # the ranker's predicted click probability


class Ranker(Protocol):
    name: str

    def rank(self, pool: Sequence[Item], arms: Mapping[Any, Any], rng: random.Random) -> list[Shown]: ...

    def update(self, arms: dict[Any, Any], item: Item, reward: float, priors: Any = None) -> None: ...

    def posterior_bounds_ok(self, arms: Mapping[Any, Any]) -> bool: ...

    def top_sector(self, arms: Mapping[Any, Any]) -> str | None: ...


class ProductionRanker:
    """Adapter over `app.modules.ranking` (the code that ships)."""

    name = "app.modules.ranking"

    def __init__(self) -> None:
        from app.modules.ranking import bandit, metrics, models, scoring

        self._m, self._metrics, self._sc = models, metrics, scoring
        self.priors_for = bandit.cold_start_priors

    def _cand(self, it: Item, i: int) -> Any:
        return self._m.Candidate(
            kind="proposal" if it.arm == "proposal" else "alert",
            id=it.id,
            arm=it.arm,
            materiality=it.materiality,
            critical=it.critical,
            title="t",
            created_at=T0 - timedelta(minutes=i),
            item={},
            symbol=f"S{i}",
            sector=it.sector,
        )

    def rank(
        self, pool: Sequence[Item], arms: Mapping[Any, Any], rng: random.Random, priors: Any = None
    ) -> list[Shown]:
        by_id = {it.id: it for it in pool}
        order = self._sc.rank(
            [self._cand(it, i) for i, it in enumerate(pool)], arms, {}, rng, page_size=SHOW, priors=priors
        )
        return [Shown(by_id[s.cand.id], s.arm_mean) for s in order]

    def update(self, arms: dict[Any, Any], item: Item, reward: float, priors: Any = None) -> None:
        arms.update(self._sc.updated_arms(arms, priors or {}, item.arm, item.sector, reward))

    def posterior_bounds_ok(self, arms: Mapping[Any, Any]) -> bool:
        lim = self._sc.ALPHA_BETA_MAX
        return all(
            a.alpha + a.beta <= lim + 1e-6 and a.alpha >= 0.1 - 1e-9 and a.beta >= 0.1 - 1e-9
            for a in arms.values()
        )

    def top_sector(self, arms: Mapping[Any, Any]) -> str | None:
        sec = {k[1].removeprefix("sector:"): a.mean for k, a in arms.items() if k[0] == self._m.SECTOR_ARM}
        return max(sec, key=lambda s: (sec[s], s)) if sec else None


class StubRanker:
    """Fallback ONLY when `app.modules.ranking` is unavailable: SPEC 10.6 in ~30 lines (no MMR/exploration)."""

    name = "stub"
    ALPHA_BETA_MAX = 50.0

    def rank(
        self, pool: Sequence[Item], arms: Mapping[Any, Any], rng: random.Random, priors: Any = None
    ) -> list[Shown]:
        draws: dict[Any, float] = {}
        scored: list[tuple[float, Item, float]] = []
        for it in pool:
            mean_ = 0.0
            theta = 0.0
            for key in ((it.arm, ""), ("sector", f"sector:{it.sector}")):
                a, b = arms.get(key, (2.0, 2.0))
                if key not in draws:
                    draws[key] = rng.betavariate(a, b)
                theta += 0.5 * draws[key]
                mean_ += 0.5 * a / (a + b)
            scored.append((0.5 * it.materiality + 0.3 * max(0.1, theta) + 0.2 * 0.3, it, mean_))
        crit = sorted((s for s in scored if s[1].critical), key=lambda s: -s[1].materiality)
        rest = sorted((s for s in scored if not s[1].critical), key=lambda s: -s[0])
        return [Shown(it, m) for _, it, m in [*crit, *rest]]

    def update(self, arms: dict[Any, Any], item: Item, reward: float, priors: Any = None) -> None:
        for key in ((item.arm, ""), ("sector", f"sector:{item.sector}")):
            a, b = arms.get(key, (2.0, 2.0))
            a, b = a + reward, b + 1.0 - reward
            if a + b > self.ALPHA_BETA_MAX:
                s = self.ALPHA_BETA_MAX / (a + b)
                a, b = max(0.1, a * s), max(0.1, b * s)
            arms[key] = (a, b)

    def posterior_bounds_ok(self, arms: Mapping[Any, Any]) -> bool:
        return all(a + b <= self.ALPHA_BETA_MAX + 1e-6 for a, b in arms.values())

    def top_sector(self, arms: Mapping[Any, Any]) -> str | None:
        sec = {k[1].removeprefix("sector:"): a / (a + b) for k, (a, b) in arms.items() if k[0] == "sector"}
        return max(sec, key=lambda s: (sec[s], s)) if sec else None


def load_ranker(force_stub: bool = False) -> Ranker:
    if not force_stub:
        try:
            return ProductionRanker()
        except (
            Exception
        ) as exc:  # ImportError / API change in the ranking module: fall back, visibly (see `name`)
            print(f"persona_sim: using the stub ranker ({type(exc).__name__}: {exc})", file=sys.stderr)
    return StubRanker()


# --------------------------------------------------------------------------------------- metrics
def _dcg(rels: Sequence[float], k: int) -> float:
    return float(sum((2.0**r - 1.0) / math.log2(i + 2) for i, r in enumerate(rels[:k])))


def ndcg_at_k(ranked: Sequence[float], pool: Sequence[float], k: int = SHOW) -> float:
    best = _dcg(sorted(pool, reverse=True), k)
    return 1.0 if best == 0 else _dcg(ranked, k) / best


def mrr(ranked: Sequence[float]) -> float:
    return next((1.0 / (i + 1) for i, r in enumerate(ranked) if r > 0), 0.0)


# --------------------------------------------------------------------------------------- simulation
@dataclass(slots=True)
class StepLog:
    ndcg: float
    mrr: float
    events: int
    briers: list[float]
    proposals_shown: int
    approvals: int
    dismissals: int
    shown: int
    critical_first: bool | None
    bounds_ok: bool


@dataclass(slots=True)
class RunLog:
    persona: str
    personalised: bool
    steps: list[StepLog] = field(default_factory=list)
    top_sector_after_drift: list[str | None] = field(default_factory=list)  # one per post-drift event count


def make_pool(rng: random.Random, step: int, with_critical: bool) -> list[Item]:
    pool = [
        Item(f"s{step}-{i}", rng.choice(ITEM_ARMS), rng.choice(SECTORS), rng.uniform(0.45, 0.65))
        for i in range(POOL - int(with_critical))
    ]
    if with_critical:
        pool.append(Item(f"s{step}-crit", "breach", rng.choice(SECTORS), 0.95, critical=True))
    return pool


def simulate(
    ranker: Ranker, persona: Persona, seed: int, *, personalised: bool = True, steps: int = STEPS
) -> RunLog:
    rng = random.Random(f"{persona.id}-{seed}")  # noqa: S311 - simulation, not security
    act = random.Random(f"act-{persona.id}-{seed}")  # noqa: S311
    arms: dict[Any, Any] = {}
    likes = set(persona.likes)
    log = RunLog(persona.id, personalised)
    events_total = 0
    priors = getattr(ranker, "priors_for", lambda t: None)(persona.tier)
    for step in range(steps):
        if persona.drift_at is not None and step == persona.drift_at:
            likes = set(persona.drift_likes)
        pool = make_pool(rng, step, with_critical=persona.bot)
        shown = (
            ranker.rank(pool, arms if personalised else {}, rng, priors)
            if isinstance(ranker, (ProductionRanker, StubRanker))
            else []
        )
        top = shown[:SHOW]
        pool_rel = [1.0 if it.sector in likes else 0.0 for it in pool]
        ranked_rel = [1.0 if s.item.sector in likes else 0.0 for s in top]
        briers: list[float] = []
        events = approvals = dismissals = proposals = 0
        for s in top:
            liked = s.item.sector in likes
            if s.item.arm == "proposal":
                proposals += 1
                if act.random() < 0.7:  # the user decides on 70% of the proposals they are shown
                    ok = act.random() < (persona.p_approve_liked if liked else persona.p_approve_other)
                    approvals += int(ok)
                    events += 1
                    if personalised:
                        ranker.update(arms, s.item, 1.0 if ok else 0.0, priors)
                    briers.append((s.p_click - float(ok)) ** 2)
                continue
            clicked = act.random() < (persona.p_click_liked if liked else persona.p_click_other)
            briers.append((s.p_click - float(clicked)) ** 2)
            if clicked:
                events += 1
                if personalised:
                    ranker.update(arms, s.item, 1.0, priors)
            elif not liked and act.random() < persona.dismiss_p:
                events += 1
                dismissals += 1
                if personalised:
                    ranker.update(arms, s.item, 0.0, priors)
        events_total += events
        crit_first = None
        if persona.bot:
            crit_first = bool(shown) and shown[0].item.critical
        log.steps.append(
            StepLog(
                ndcg_at_k(ranked_rel, pool_rel),
                mrr(ranked_rel),
                events_total,
                briers,
                proposals,
                approvals,
                dismissals,
                len(top),
                crit_first,
                ranker.posterior_bounds_ok(arms),
            )
        )
        if personalised and persona.drift_at is not None and step >= persona.drift_at:
            log.top_sector_after_drift.append(ranker.top_sector(arms))
    return log


def moving_average(xs: Sequence[float], w: int = 5) -> list[float]:
    return [mean(xs[max(0, i - w + 1) : i + 1]) for i in range(len(xs))]


def time_to_personalization(runs: Sequence[RunLog]) -> float | None:
    """Mean events (over replications) until the 5-step moving NDCG@5 reaches 90% of its plateau."""
    curve = [mean(r.steps[i].ndcg for r in runs) for i in range(len(runs[0].steps))]
    sm = moving_average(curve)
    plateau = mean(sm[-50:])
    target = 0.9 * plateau
    t = next((i for i, v in enumerate(sm) if v >= target), None)
    return None if t is None else mean(r.steps[t].events for r in runs)


def ndcg_after_events(runs: Sequence[RunLog], n_events: int) -> float:
    """Mean NDCG@5 of the first step at which a run has seen >= n_events events (averaged over replications)."""
    vals = []
    for r in runs:
        step = next((s for s in r.steps if s.events >= n_events), r.steps[-1])
        vals.append(step.ndcg)
    return mean(vals)


def _persona_summary(p: Persona, pers: list[RunLog], base: list[RunLog]) -> dict[str, Any]:
    def agg(runs: list[RunLog], sl: slice) -> dict[str, float]:
        steps = [s for r in runs for s in r.steps[sl]]
        proposals = sum(s.proposals_shown for s in steps)
        shown = sum(s.shown for s in steps)
        briers = [b for s in steps for b in s.briers]
        return {
            "ndcg5": mean(s.ndcg for s in steps),
            "mrr": mean(s.mrr for s in steps),
            "brier": mean(briers) if briers else float("nan"),
            "approve_rate": (sum(s.approvals for s in steps) / proposals) if proposals else float("nan"),
            "fatigue_index": (sum(s.dismissals for s in steps) / shown) if shown else float("nan"),
        }

    last = slice(-100, None)
    a, b = agg(pers, last), agg(base, last)
    ttp = time_to_personalization(pers)
    return {
        "id": p.id,
        "name": p.name,
        "personalised": a,
        "baseline": b,
        "ndcg5_lift": a["ndcg5"] - b["ndcg5"],
        "approve_rate_lift": a["approve_rate"] - b["approve_rate"],
        "time_to_personalization_events": ttp,
        "ndcg5_at_30_events": ndcg_after_events(pers, 30),
    }


def interest_drift_flip(seeds: Sequence[int]) -> dict[str, Any]:
    """T-PERS-03 on the INTERESTS vector (stream C's production maths: `interest_contributions` + `merge_interest`).

    A dividend-first user (questionnaire baseline 0.9 on Consumer Staples / Utilities) starts viewing technology
    and communication names; the top interest must flip to those sectors within 20 view events."""
    from app.modules.personalization import features as f

    sector_of = {"NVDA": "Technology", "GOOGL": "Communication", "KO": "Consumer Staples", "DUK": "Utilities"}
    baseline = {f"sector:{s}": 0.25 for s in set(sector_of.values())}
    baseline.update({"sector:Consumer Staples": 0.9, "sector:Utilities": 0.9})
    new = {"sector:Technology", "sector:Communication"}
    at20: list[bool] = []
    first: list[int] = []
    for seed in seeds:
        rng = random.Random(f"drift-{seed}")  # noqa: S311
        events: list[Any] = []
        flipped_at: int | None = None
        for n in range(1, 41):
            events.append(
                f.EventRow(
                    f"00000000-0000-0000-0000-{n:012d}",
                    T0 - timedelta(seconds=40 - n),
                    "ticker_viewed",
                    {"ticker": rng.choice(["NVDA", "GOOGL"])},
                    {},
                )
            )
            w = f.merge_interest(baseline, f.interest_contributions(events, sector_of, T0))
            top = max(w, key=lambda d: (w[d], d))
            if flipped_at is None and top in new:
                flipped_at = n
            if n == 20:
                at20.append(top in new)
        if flipped_at is not None:
            first.append(flipped_at)
    return {
        "flip_rate_within_20_events": mean(float(x) for x in at20),
        "median_events_to_flip": sorted(first)[len(first) // 2] if first else None,
    }


def run_simulation(
    seeds: Sequence[int] = tuple(range(8)), steps: int = STEPS, force_stub: bool = False
) -> dict[str, Any]:
    ranker = load_ranker(force_stub)
    out: dict[str, Any] = {"ranker": ranker.name, "steps": steps, "seeds": len(seeds), "personas": []}
    per_persona_runs: dict[str, list[RunLog]] = {}
    for p in PERSONAS:
        pers = [simulate(ranker, p, s, personalised=True, steps=steps) for s in seeds]
        base = [simulate(ranker, p, s, personalised=False, steps=steps) for s in seeds]
        per_persona_runs[p.id] = pers
        out["personas"].append(_persona_summary(p, pers, base))
    # T-PERS-03: after the drift, within 20 events the top sector interest flips to the new likes
    drifter = next(p for p in PERSONAS if p.drift_at is not None)
    drift_at = drifter.drift_at or 1
    flips = []
    for r in per_persona_runs[drifter.id]:
        post = r.steps[drift_at:]
        base_events = r.steps[drift_at - 1].events
        hit = next((i for i, s in enumerate(post) if s.events - base_events >= 20), len(post) - 1)
        flips.append(r.top_sector_after_drift[hit] in drifter.drift_likes)
    out["bandit_sector_flip_rate_within_20_events"] = mean(
        float(f) for f in flips
    )  # informational: arms saturate (alpha+beta <= 50)
    out["interest_drift"] = interest_drift_flip(seeds)
    out["drift_flip_rate_within_20_events"] = out["interest_drift"]["flip_rate_within_20_events"]
    bot = next(p for p in PERSONAS if p.bot)
    bot_runs = per_persona_runs[bot.id]
    out["bot"] = {
        "critical_first_rate": mean(
            float(s.critical_first) for r in bot_runs for s in r.steps if s.critical_first is not None
        ),
        "posterior_bounded": all(s.bounds_ok for r in bot_runs for s in r.steps),
    }
    real = [p for p in out["personas"] if not next(q for q in PERSONAS if q.id == p["id"]).bot]
    stable = [
        p for p in real if next(q for q in PERSONAS if q.id == p["id"]).drift_at is None
    ]  # drifter excluded from TTP
    out["aggregate"] = {
        "ndcg5_personalised": mean(p["personalised"]["ndcg5"] for p in real),
        "ndcg5_baseline": mean(p["baseline"]["ndcg5"] for p in real),
        "ndcg5_lift": mean(p["ndcg5_lift"] for p in real),
        "approve_rate_lift": mean(
            p["approve_rate_lift"] for p in real if p["approve_rate_lift"] == p["approve_rate_lift"]
        ),
        "time_to_personalization_events": mean(
            p["time_to_personalization_events"]
            for p in stable
            if p["time_to_personalization_events"] is not None
        ),
        "dividend_persona_ndcg5_at_30_events": next(
            p["ndcg5_at_30_events"] for p in out["personas"] if p["id"] == "P1"
        ),
    }
    return out


if __name__ == "__main__":
    print(json.dumps(run_simulation(), indent=2, default=str))
