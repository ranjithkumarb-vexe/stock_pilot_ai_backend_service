"""Request / response models of the alerts API."""

from __future__ import annotations

from typing import Any, Literal

from app.core.schemas import ApiModel, ResponseModel

AlertType = Literal["breach", "filing", "move", "earnings", "mirror", "concentration"]
AlertStateName = Literal["new", "opened", "dismissed", "snoozed"]


class AlertOut(ResponseModel):
    id: str
    type: AlertType
    symbol: str | None
    critical: bool
    materiality: float
    title: str
    body: str
    source_label: str
    state: AlertStateName
    created_at: str
    snoozed_until: str | None = None
    why: dict[str, Any] | None = None


class AlertsPage(ResponseModel):
    items: list[AlertOut]
    next_cursor: str | None = None
    move_threshold: float
    fatigue_index: float
    min_materiality: float


class AlertActionIn(ApiModel):
    action: Literal["open", "dismiss", "snooze", "restore"]


class RefreshOut(ResponseModel):
    created: int
    resolved: int
    items: list[AlertOut]
    move_threshold: float
