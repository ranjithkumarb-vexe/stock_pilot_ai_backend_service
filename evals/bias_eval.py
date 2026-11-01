"""Bias-detector precision / recall on synthetic trade logs with injected biases (SPEC 10.8, BE-15 acceptance).

Run: `uv run python -m evals.bias_eval` (writes `evals/results/bias_detectors.json`). Deterministic (seeded) and
offline. For every bias, 100 users with the bias injected and 100 without; the production detector functions
(`app.modules.personalization.bias`) are applied unchanged and `flag` is treated as a positive prediction.
Synthetic data only: these numbers show the detectors separate *injected* bias from noise, not real investors.
"""

from __future__ import annotations

import json
import random
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from app.modules.personalization import bias as b

SEED = 20261101
N_PER_CLASS = 100
START = date(2026, 1, 2)
DAYS = 240
RESULTS = Path(__file__).parent / "results" / "bias_detectors.json"


@dataclass
class Score:
    detector: str
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 0.0

    def add(self, truth: bool, predicted: bool) -> None:
        if truth and predicted:
            self.tp += 1
        elif truth:
            self.fn += 1
        elif predicted:
            self.fp += 1
        else:
            self.tn += 1

    def as_dict(self) -> dict[str, object]:
        d = asdict(self)
        d.update(
            precision=round(self.precision, 3),
            recall=round(self.recall, 3),
            n=self.tp + self.fp + self.fn + self.tn,
        )
        return d


def _prices(
    rng: random.Random, n_symbols: int = 8, spike_days: set[int] | None = None
) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for s in range(n_symbols):
        px, path = 100.0, []
        for d in range(DAYS):
            r = rng.gauss(0.0003, 0.012)
            if spike_days and d in spike_days and s == 0:
                r += 0.09
            px *= 1 + r
            path.append(px)
        out[f"S{s}"] = path
    return out


def _ts(day: int) -> datetime:
    return datetime.combine(START + timedelta(days=day), datetime.min.time(), tzinfo=UTC) + timedelta(
        hours=15
    )


def _disposition_user(rng: random.Random, biased: bool) -> tuple[list[b.Trade], dict[str, list[float]]]:
    prices = _prices(rng)
    held: dict[str, tuple[float, float]] = {}  # symbol -> (qty, avg cost)
    trades: list[b.Trade] = []
    for day in range(10, DAYS):
        if rng.random() < 0.10 and len(held) < 6:
            sym = rng.choice([s for s in prices if s not in held])
            px = prices[sym][day]
            held[sym] = (10.0, px)
            trades.append(b.Trade(_ts(day), sym, "buy", 10.0, px))
        elif rng.random() < 0.18 and held:
            winners = [s for s, (_, c) in held.items() if prices[s][day] > c]
            losers = [s for s in held if s not in winners]
            pool = winners * 5 + losers if biased else list(held)  # biased: winners sold 5x as eagerly
            if not pool:
                continue
            sym = rng.choice(pool)
            px = prices[sym][day]
            trades.append(b.Trade(_ts(day), sym, "sell", held[sym][0], px))
            del held[sym]
    return trades, prices


def eval_disposition(rng: random.Random) -> Score:
    score = Score("disposition")
    for truth in [True] * N_PER_CLASS + [False] * N_PER_CLASS:
        trades, prices = _disposition_user(rng, truth)

        def close_on(sym: str, d: date, prices: dict[str, list[float]] = prices) -> float | None:
            idx = (d - START).days
            return prices[sym][idx] if 0 <= idx < DAYS and sym in prices else None

        res = b.detect_disposition(trades, close_on)
        score.add(truth, res.state == "flag")
    return score


def eval_overtrading(rng: random.Random) -> Score:
    score = Score("overtrading")
    now = _ts(DAYS)
    for truth in [True] * N_PER_CLASS + [False] * N_PER_CLASS:
        n = rng.randint(11, 20) if truth else rng.randint(0, 7)
        trades = [
            b.Trade(now - timedelta(hours=rng.uniform(1, 160)), "S0", "buy", 1, 100.0) for _ in range(n)
        ]
        res = b.detect_overtrading(trades, now=now, rule_limit=10, portfolio_value=50_000)
        score.add(truth, res.state == "flag")
    return score


def eval_herding(rng: random.Random) -> Score:
    score = Score("herding")
    for truth in [True] * N_PER_CLASS + [False] * N_PER_CLASS:
        spike_days = set(rng.sample(range(40, DAYS - 3), 6))
        prices = _prices(rng, 1, spike_days)
        bars = [(START + timedelta(days=d), p, 1000.0 + rng.gauss(0, 50)) for d, p in enumerate(prices["S0"])]
        spikes = {"S0": b.spike_dates(bars)}
        trades: list[b.Trade] = []
        for _ in range(12):
            if truth and rng.random() < 0.75 and spike_days:
                day = rng.choice(sorted(spike_days)) + rng.randint(0, 2)
            else:
                day = rng.randint(30, DAYS - 1)
            trades.append(b.Trade(_ts(day), "S0", "buy", 1, prices["S0"][day]))
        res = b.detect_herding(trades, spikes)
        score.add(truth, res.state == "flag")
    return score


def eval_loss_aversion(rng: random.Random) -> Score:
    score = Score("loss_aversion")
    for truth in [True] * N_PER_CLASS + [False] * N_PER_CLASS:
        p_loser, p_winner = (0.85, 0.30) if truth else (0.5, 0.5)
        decisions = [
            b.ReduceDecision(rng.random() < (p_loser if lo else p_winner), lo)
            for lo in [True] * 8 + [False] * 8
        ]
        score.add(truth, b.detect_loss_aversion(decisions).state == "flag")
    return score


def eval_concentration(rng: random.Random) -> Score:
    score = Score("concentration")
    for truth in [True] * N_PER_CLASS + [False] * N_PER_CLASS:
        n = rng.randint(5, 12)
        raw = [rng.random() ** (6 if truth else 1) for _ in range(n)]
        values = {f"S{i}": v * 1000 for i, v in enumerate(raw)}
        # ground truth is the HHI definition itself: the detector's threshold on the injected weights
        true_hhi = sum((v / sum(raw)) ** 2 for v in raw)
        score.add(true_hhi > b.HHI_FLAG, b.detect_concentration(values).state == "flag")
    return score


def run() -> dict[str, object]:
    rng = random.Random(SEED)  # noqa: S311 - deterministic simulation, not security
    scores = [
        eval_disposition(rng),
        eval_overtrading(rng),
        eval_herding(rng),
        eval_loss_aversion(rng),
        eval_concentration(rng),
    ]
    return {
        "seed": SEED,
        "n_per_class": N_PER_CLASS,
        "note": "synthetic logs with injected biases; positive prediction = state 'flag'",
        "detectors": [s.as_dict() for s in scores],
    }


def main() -> int:
    result = run()
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    for d in result["detectors"]:  # type: ignore[attr-defined]
        print(
            f"{d['detector']:<14} precision={d['precision']:.2f} recall={d['recall']:.2f} (tp={d['tp']} fp={d['fp']} fn={d['fn']} tn={d['tn']})"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
