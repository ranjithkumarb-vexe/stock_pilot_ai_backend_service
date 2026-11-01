"""`make eval`: run every check, write `evals/results/latest.json`, record the run in `eval_runs`.

    uv run python -m evals.run            # everything (pytest-backed checks included, ~1-2 min)
    uv run python -m evals.run --fast     # skip the pytest-backed checks (they are listed as not measured)
    uv run python -m evals.run --strict   # exit 1 when any threshold is missed (CI)

Needs the dev database with ingested filings (`make ingest-xbrl ingest-filings`) and, for the portfolio
questions, the seeded personas (`make seed`). Missing pieces are reported as not measured, never as a pass.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from app.core.db import system_tx
from evals.checks import STATIC_NOT_MEASURED, build_specs, sim
from evals.framework import LATEST, run_all, write_results
from evals.runtime import close_context, open_context


def sim_metrics() -> dict[str, Any]:
    s = sim()
    return {"persona_sim": s}


async def record_in_db(results: dict[str, Any]) -> None:
    try:
        async with system_tx() as conn:
            await conn.execute(
                "insert into eval_runs (finished_at, git_sha, thresholds_met, results) values (now(), $1, $2, $3)",
                results.get("git_sha"),
                results["thresholds_met"],
                results,
            )
    except Exception as exc:  # the JSON file is the source of truth; the table is history
        print(f"eval_runs not updated: {type(exc).__name__}", file=sys.stderr)


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="evals.run")
    ap.add_argument("--fast", action="store_true", help="skip pytest-backed checks")
    ap.add_argument("--strict", action="store_true", help="exit 1 if thresholds are not met")
    ap.add_argument("--out", type=Path, default=LATEST)
    args = ap.parse_args(argv)
    ctx = await open_context(with_pytest=not args.fast)
    try:
        results = await run_all(
            build_specs(),
            ctx,
            extra_not_measured=STATIC_NOT_MEASURED,
            environment={
                "llm_provider": ctx.llm.provider.name,
                "persona_user": str(ctx.user_id) if ctx.user_id else None,
                "ranker": sim()["ranker"],
                "note": "LLM-phrased answers are unmeasured: no Meta API key, MockProvider/degraded path only",
            },
            metrics=sim_metrics(),
        )
        path = write_results(results, args.out)
        await record_in_db(results)
    finally:
        await close_context(ctx)
    for it in results["items"]:
        print(f"{it['status'].upper():4} {it['id']:11} {it['actual']}   [target {it['target']}]")
    for nm in results["not_measured"]:
        print(f"n/m  {nm['id']:11} {nm['reason']}")
    s = results["summary"]
    print(f"\n{s['passed']} pass, {s['failed']} fail, {s['not_measured']} not measured -> {path}")
    return 1 if args.strict and not results["thresholds_met"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
