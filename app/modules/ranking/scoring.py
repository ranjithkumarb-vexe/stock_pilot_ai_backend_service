"""Pure ranking maths (SPEC 10.6): score, Thompson draw, epsilon floor, MMR, pinned criticals.

    score = 0.5 * materiality + 0.3 * Thompson_sample(user, item_type, context) + 0.2 * interest_similarity

* Materiality is deterministic (computed by code when the item is created).
* One Thompson draw per (arm, context) per ranking round, so items of the same arm share the draw and are ordered
  by their own materiality / interest.
* Guardrails: Beta parameters are bounded (each <= 50, total mass <= 50, so history decays and cannot lock in),
  the draw has an epsilon floor, and with probability `EXPLORE_P` one hidden item of a suppressed arm is shown.
* Critical items are pinned first and the bandit can neither demote nor hide them.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence

from app.modules.ranking.models import SECTOR_ARM, Arm, Candidate, Scored

W_MATERIALITY = 0.5
W_THOMPSON = 0.3
W_INTEREST = 0.2
MMR_LAMBDA = 0.7
ALPHA_BETA_MAX = 50.0  # per-parameter ceiling AND total-mass ceiling (alpha + beta <= 50)
PARAM_MIN = 0.1
EPS_FLOOR = 0.10  # a suppressed arm still draws at least this
EXPLORE_P = 0.10  # chance that one suppressed item is promoted into the last visible slot
EXPLORE_ARM_MEAN = 0.25  # an arm is "suppressed" below this posterior mean
NEUTRAL_INTEREST = 0.3  # design: unknown interest = 0.3
DEFAULT_PRIOR = Arm(2.0, 2.0)  # weak Beta(2,2) prior
SECTOR_SHARE = 0.65  # weight of the shared sector-preference draw inside the learned (Thompson) term
SECTOR_PRIOR = Arm(
    1.0, 1.0
)  # the shared sector-preference arm starts uninformative: it is learned only from behaviour
SECTOR_EVIDENCE = (
    3.0  # pseudo-counts one reward adds to the sector arm (a sector taste is shared by all item types)
)
FORGET = 0.95  # per-update discount of evidence beyond the prior (non-stationary taste, no lock-in)


def clamp_arm(alpha: float, beta: float) -> Arm:
    """Bound a posterior: each parameter in [PARAM_MIN, 50] and alpha + beta <= 50 (older evidence decays)."""
    a, b = max(PARAM_MIN, alpha), max(PARAM_MIN, beta)
    total = a + b
    if total > ALPHA_BETA_MAX:
        scale = ALPHA_BETA_MAX / total
        a, b = max(PARAM_MIN, a * scale), max(PARAM_MIN, b * scale)
        over = a + b - ALPHA_BETA_MAX
        if over > 0:  # the floor on the small parameter can push the sum over: take it from the large one
            if a >= b:
                a -= over
            else:
                b -= over
    return Arm(min(a, ALPHA_BETA_MAX), min(b, ALPHA_BETA_MAX))


def update_arm(
    arm: Arm, reward: float, prior: Arm = DEFAULT_PRIOR, forget: float = FORGET, weight: float = 1.0
) -> Arm:
    """Discounted Bernoulli update with a fractional reward in [0, 1] (approve 1, modify 0.5, reject 0 ...).

    Evidence beyond the prior decays by `forget` per update (so the posterior tracks a drifting taste and its
    mass settles near `prior + 1 / (1 - forget)`), then the reward is added and the result is bounded."""
    r = max(0.0, min(1.0, reward))
    a = prior.alpha + forget * (arm.alpha - prior.alpha) + weight * r
    b = prior.beta + forget * (arm.beta - prior.beta) + weight * (1.0 - r)
    return clamp_arm(a, b)


def updated_arms(
    arms: Mapping[tuple[str, str], Arm],
    priors: Mapping[str, Arm],
    arm: str,
    sector: str | None,
    reward: float,
) -> dict[tuple[str, str], Arm]:
    """The posterior entries one reward changes: the type arm `(arm, '')` and, with a sector, the shared
    sector-preference arm `('sector', 'sector:S')` (so what the user likes in one item type carries to the others)."""
    base = arms.get((arm, "")) or priors.get(arm, DEFAULT_PRIOR)
    prior = priors.get(arm, DEFAULT_PRIOR)
    out = {(arm, ""): update_arm(clamp_arm(base.alpha, base.beta), reward, prior)}
    if sector:
        key = (SECTOR_ARM, f"sector:{sector}")
        out[key] = update_arm(arms.get(key) or SECTOR_PRIOR, reward, SECTOR_PRIOR, weight=SECTOR_EVIDENCE)
    return out


def draw(arm: Arm, rng: random.Random) -> float:
    return rng.betavariate(arm.alpha, arm.beta)


def interest_similarity(c: Candidate, interests: Mapping[str, float]) -> float:
    dim = c.interest_dim
    if dim is None:
        return NEUTRAL_INTEREST
    w = interests.get(dim)
    return NEUTRAL_INTEREST if w is None else max(0.0, min(1.0, float(w)))


def item_similarity(a: Candidate, b: Candidate) -> float:
    """Redundancy of two items for MMR (1 = the same story)."""
    if a.symbol and a.symbol == b.symbol:
        return 1.0 if a.arm == b.arm else 0.8
    same_arm, same_sector = a.arm == b.arm, bool(a.sector) and a.sector == b.sector
    if same_arm and same_sector:
        return 0.5
    if same_arm:
        return 0.35
    return 0.25 if same_sector else 0.0


def score_candidates(
    cands: Sequence[Candidate],
    arms: Mapping[tuple[str, str], Arm],
    interests: Mapping[str, float],
    rng: random.Random,
    *,
    priors: Mapping[str, Arm] | None = None,
) -> list[Scored]:
    """Score every candidate (no ordering yet). `arms` is keyed `(arm, context)`; '' is the type-level arm."""
    priors = priors or {}
    draws: dict[tuple[str, str], tuple[float, Arm]] = {}

    def arm_of(name: str, ctx: str) -> Arm:
        found = arms.get((name, ctx))
        if found is not None:
            return found
        if name == SECTOR_ARM:
            return SECTOR_PRIOR
        return priors.get(name) or DEFAULT_PRIOR

    def theta(name: str, ctx: str) -> tuple[float, Arm]:
        key = (name, ctx)
        if key not in draws:
            a = arm_of(name, ctx)
            draws[key] = (draw(a, rng), a)
        return draws[key]

    out: list[Scored] = []
    for c in cands:
        t_type, a_type = theta(c.arm, "")
        if c.context:
            t_ctx, a_ctx = theta(SECTOR_ARM, c.context)
            t = (1 - SECTOR_SHARE) * t_type + SECTOR_SHARE * t_ctx
            mean = (1 - SECTOR_SHARE) * a_type.mean + SECTOR_SHARE * a_ctx.mean
        else:
            t, mean = t_type, a_type.mean
        t_eff = max(EPS_FLOOR, t)
        sim = interest_similarity(c, interests)
        mat = max(0.0, min(1.0, c.materiality))
        score = W_MATERIALITY * mat + W_THOMPSON * t_eff + W_INTEREST * sim
        out.append(
            Scored(
                cand=c,
                score=round(score, 6),
                materiality=mat,
                thompson=round(t_eff, 6),
                interest=round(sim, 6),
                pinned=c.critical,
                arm_mean=round(mean, 6),
                detail={
                    "arm": c.arm,
                    "context": c.context,
                    "alpha": round(a_type.alpha, 4),
                    "beta": round(a_type.beta, 4),
                    "raw_draw": round(t, 6),
                },
            )
        )
    return out


def mmr_order(items: Sequence[Scored], lam: float = MMR_LAMBDA) -> list[Scored]:
    """Greedy maximal-marginal-relevance order of the NON-pinned items (lambda = 0.7)."""
    remaining = sorted(items, key=lambda s: (-s.score, s.cand.id))
    chosen: list[Scored] = []
    while remaining:
        best_i, best_v = 0, float("-inf")
        for i, s in enumerate(remaining):
            penalty = max((item_similarity(s.cand, c.cand) for c in chosen), default=0.0)
            v = lam * s.score - (1.0 - lam) * penalty
            if v > best_v + 1e-12:
                best_i, best_v = i, v
        chosen.append(remaining.pop(best_i))
    return chosen


def rank(
    cands: Sequence[Candidate],
    arms: Mapping[tuple[str, str], Arm],
    interests: Mapping[str, float],
    rng: random.Random,
    *,
    page_size: int | None = None,
    priors: Mapping[str, Arm] | None = None,
) -> list[Scored]:
    """Full order: pinned criticals (materiality desc, newest first), then MMR over the rest, then the
    epsilon-exploration swap inside the visible page."""
    scored = score_candidates(cands, arms, interests, rng, priors=priors)
    pinned = sorted(
        (s for s in scored if s.pinned),
        key=lambda s: (-s.materiality, -s.cand.created_at.timestamp(), s.cand.id),
    )
    rest = mmr_order([s for s in scored if not s.pinned])
    ordered = [*pinned, *rest]
    if page_size is not None and len(ordered) > page_size > len(pinned):
        _explore(ordered, page_size, rng)
    return ordered


def _explore(ordered: list[Scored], page_size: int, rng: random.Random) -> None:
    """Epsilon-floor: occasionally show one hidden item of a suppressed arm so the arm can recover."""
    roll = rng.random()
    if roll >= EXPLORE_P:
        return
    hidden = [s for s in ordered[page_size:] if not s.pinned and s.arm_mean < EXPLORE_ARM_MEAN]
    if not hidden:
        return
    pick = max(hidden, key=lambda s: (s.materiality, s.cand.id))
    ordered.remove(pick)
    pick.explored = True
    ordered.insert(page_size - 1, pick)
