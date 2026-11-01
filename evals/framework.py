"""Eval runner core (BE-20): thresholds are data, results are computed, nothing is hard-coded to pass.

A check returns a `Measurement` (named numbers + a display string). The runner compares each number with the
`Threshold`s declared for that eval id (SPEC 12.3 / 12.4) and sets `status` itself: a check cannot declare its
own pass. A check that raises is reported as `fail` (never skipped); one that cannot run here returns
`NotMeasured(reason)` and is listed under `not_measured`, not counted as a pass.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import traceback
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

RESULTS_DIR = Path(__file__).resolve().parent / "results"
LATEST = RESULTS_DIR / "latest.json"


@dataclass(frozen=True, slots=True)
class Threshold:
    op: Literal["ge", "le", "eq", "gt", "lt"]
    value: float
    fmt: str = "{:g}"  # how the bound is shown in `target`

    def met(self, x: float) -> bool:
        if x != x:  # NaN never passes
            return False
        return {
            "ge": x >= self.value - 1e-12,
            "le": x <= self.value + 1e-12,
            "eq": abs(x - self.value) <= 1e-9,
            "gt": x > self.value,
            "lt": x < self.value,
        }[self.op]

    def text(self) -> str:
        sym = {"ge": ">=", "le": "<=", "eq": "=", "gt": ">", "lt": "<"}[self.op]
        return f"{sym} {self.fmt.format(self.value)}"


@dataclass(slots=True)
class Measurement:
    values: dict[str, float]
    display: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class NotMeasured:
    reason: str
    owner: str = ""


Check = Callable[[Any], Awaitable[Measurement | NotMeasured]]


@dataclass(slots=True)
class EvalSpec:
    id: str
    description: str
    thresholds: dict[str, Threshold]
    run: Check
    target: str | None = None  # override of the generated target text

    def target_text(self) -> str:
        return self.target or ", ".join(t.text() for t in self.thresholds.values())


@dataclass(slots=True)
class EvalOutcome:
    id: str
    description: str
    target: str
    actual: str
    status: Literal["pass", "fail"]
    seconds: float
    detail: dict[str, Any]

    def as_item(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "target": self.target,
            "actual": self.actual,
            "status": self.status,
            "seconds": round(self.seconds, 2),
            "detail": self.detail,
        }


def judge(spec: EvalSpec, m: Measurement) -> bool:
    if not spec.thresholds:
        return False
    return all(k in m.values and t.met(m.values[k]) for k, t in spec.thresholds.items())


async def run_spec(spec: EvalSpec, ctx: Any) -> EvalOutcome | tuple[EvalSpec, NotMeasured]:
    t0 = time.perf_counter()
    try:
        res = await spec.run(ctx)
    except Exception as exc:  # a crashing check is a failing check
        tb = traceback.format_exception_only(type(exc), exc)[-1].strip()
        return EvalOutcome(
            spec.id,
            spec.description,
            spec.target_text(),
            f"error: {tb[:160]}",
            "fail",
            time.perf_counter() - t0,
            {"error": tb[:400]},
        )
    if isinstance(res, NotMeasured):
        return spec, res
    ok = judge(spec, res)
    return EvalOutcome(
        spec.id,
        spec.description,
        spec.target_text(),
        res.display,
        "pass" if ok else "fail",
        time.perf_counter() - t0,
        {**res.detail, "values": res.values},
    )


def git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=5,
            cwd=Path(__file__).resolve().parent.parent,
            check=False,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


async def run_all(
    specs: Sequence[EvalSpec],
    ctx: Any,
    *,
    extra_not_measured: Sequence[dict[str, Any]] = (),
    environment: dict[str, Any] | None = None,
    metrics: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    t0 = time.perf_counter()
    items: list[EvalOutcome] = []
    nm: list[dict[str, Any]] = [dict(x) for x in extra_not_measured]
    for spec in specs:
        r = await run_spec(spec, ctx)
        if isinstance(r, EvalOutcome):
            items.append(r)
        else:
            spec_, n = r
            nm.append(
                {
                    "id": spec_.id,
                    "description": spec_.description,
                    "target": spec_.target_text(),
                    "reason": n.reason,
                    "owner": n.owner,
                }
            )
    passed = sum(1 for i in items if i.status == "pass")
    failed = len(items) - passed
    return {
        "generated_at": (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "thresholds_met": bool(items) and failed == 0,
        "git_sha": git_sha(),
        "duration_s": round(time.perf_counter() - t0, 1),
        "items": [i.as_item() for i in items],
        "not_measured": nm,
        "summary": {"passed": passed, "failed": failed, "not_measured": len(nm), "measured": len(items)},
        "environment": environment or {},
        "metrics": metrics or {},
    }


def write_results(results: dict[str, Any], path: Path = LATEST) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(results, indent=2, sort_keys=False, default=str) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path
