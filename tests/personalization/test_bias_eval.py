"""The synthetic bias-detector evaluation is deterministic and stays above regression floors."""

from __future__ import annotations

from typing import Any

from evals.bias_eval import run


def test_bias_detectors_meet_regression_floors_on_synthetic_logs() -> None:
    result: dict[str, Any] = run()
    again: dict[str, Any] = run()
    assert result == again  # seeded and offline
    by = {d["detector"]: d for d in result["detectors"]}
    assert set(by) == {"disposition", "overtrading", "herding", "loss_aversion", "concentration"}
    for name, d in by.items():
        assert d["n"] == 200, name
        assert d["precision"] >= 0.75 and d["recall"] >= 0.65, (name, d)
    assert by["overtrading"]["precision"] == by["overtrading"]["recall"] == 1.0
