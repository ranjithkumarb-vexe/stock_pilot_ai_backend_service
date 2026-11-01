"""`GET /v1/evals/latest`: the last real `make eval` run (nothing here is hard-coded; no file => 404)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import APIRouter
from pydantic import ValidationError

from app.core.deps import CurrentUser
from app.core.errors import AppError, ErrorCode, not_found
from app.modules.evals.schemas import EvalsLatest

router = APIRouter(tags=["evals"])

DEFAULT_RESULTS = Path(__file__).resolve().parents[3] / "evals" / "results" / "latest.json"


def results_path() -> Path:
    return Path(os.environ.get("EVALS_RESULTS_PATH") or DEFAULT_RESULTS)


def load_latest(path: Path | None = None) -> EvalsLatest:
    p = path or results_path()
    if not p.is_file():
        raise not_found("No eval run has been recorded yet. Run `make eval`.")
    try:
        return EvalsLatest.model_validate(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, ValueError, ValidationError) as exc:
        raise AppError(ErrorCode.UPSTREAM_UNAVAILABLE, "Eval results file is unreadable") from exc


@router.get("/evals/latest", response_model=EvalsLatest, summary="Scoreboard from the latest `make eval` run")
async def evals_latest(user: CurrentUser) -> EvalsLatest:
    return load_latest()
