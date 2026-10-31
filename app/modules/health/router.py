"""Ops endpoints (unauthenticated): liveness and readiness."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.core.db import Database
from app.core.errors import upstream_unavailable

PREFIX = ""  # mounted at /healthz and /readyz (not under /v1)

router = APIRouter(tags=["ops"])


class HealthStatus(BaseModel):
    status: Literal["ok"]


@router.get("/healthz", response_model=HealthStatus, summary="Liveness probe")
async def healthz() -> HealthStatus:
    return HealthStatus(status="ok")


@router.get("/readyz", response_model=HealthStatus, summary="Readiness probe (checks the database)")
async def readyz(request: Request) -> HealthStatus:
    db: Database = request.app.state.db
    if not await db.ping():
        raise upstream_unavailable("Database unavailable")
    return HealthStatus(status="ok")
