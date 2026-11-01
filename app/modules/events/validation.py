"""Event validation against the SPEC 10.2 schema. Pure: no I/O, so every rule is unit-testable.

Each event is validated on its own; a bad one is reported as `{index, reason}` and never blocks the others.
Reasons are short machine codes and never echo the offending value (no user content in responses or logs).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

MAX_BATCH = 100
MAX_EVENT_BYTES = 4096
MAX_FUTURE = timedelta(minutes=5)
MAX_AGE = timedelta(days=30)
SCHEMA_VERSION = 1

# SPEC 10.2 P0 types a browser may send.
CLIENT_TYPES = frozenset(
    {
        "ticker_viewed",
        "brief_opened",
        "section_expanded",
        "section_dwell",
        "metric_clicked",
        "glossary_clicked",
        "chat_query",
        "alert_opened",
        "alert_dismissed",
        "alert_snoozed",
        "thumbs_up",
        "thumbs_down",
        "session_started",
    }
)
# Types the server writes itself from authoritative actions (decisions, fills, rule edits). A client may not
# forge them: they drive the bandit, bias detectors and memories.
SERVER_ONLY_TYPES = frozenset(
    {
        "order_placed",
        "order_filled",
        "proposal_approved",
        "proposal_rejected",
        "proposal_modified",
        "rule_edited",
        "rule_override",
    }
)
SECTIONS = frozenset({"trend", "bal", "val", "moat", "risk", "cat", "bb"})

_TOP_KEYS = {"event_id", "ts", "session_id", "type", "entity", "props", "context", "client", "schema_version"}
_ENTITY_KEYS = {"ticker", "section", "alert_id", "proposal_id", "metric"}
_PROPS_KEYS = {"dwell_ms", "action", "reason", "delta", "query_text_hash", "topics"}
_CONTEXT_KEYS = {"market_session", "vix_band", "earnings_season"}
_CLIENT_KEYS = {"device", "tz"}

_TICKER_RE = re.compile(r"^[A-Z0-9.\-]{1,10}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_\-:.]{1,64}$")
_WORD_RE = re.compile(r"^[a-z0-9_\- ]{1,64}$")
_TOPIC_RE = re.compile(r"^[a-z_]{1,24}$")
_HASH_RE = re.compile(r"^[A-Za-z0-9]{8,128}$")
_SESSIONS = {"pre", "regular", "post", "closed"}

REQUIRED_ENTITY: dict[str, tuple[str, ...]] = {
    "ticker_viewed": ("ticker",),
    "section_expanded": ("section",),
    "section_dwell": ("section",),
    "metric_clicked": ("metric",),
    "alert_opened": ("alert_id",),
    "alert_dismissed": ("alert_id",),
    "alert_snoozed": ("alert_id",),
}
REQUIRED_PROPS: dict[str, tuple[str, ...]] = {"section_dwell": ("dwell_ms",)}


@dataclass(frozen=True, slots=True)
class ValidEvent:
    event_id: UUID
    ts: datetime
    session_id: str | None
    type: str
    entity: dict[str, Any]
    props: dict[str, Any]
    context: dict[str, Any]
    client: dict[str, Any]


class Rejected(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_SAFE_KEY_RE = re.compile(r"^[a-z_]{1,24}$")


def _key(name: str) -> str:
    """Echo a client-chosen key only when it looks like a plain identifier."""
    return name if _SAFE_KEY_RE.match(name) else "?"


def _obj(value: Any, name: str, allowed: set[str]) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise Rejected(f"invalid_{name}")
    extra = sorted(set(value) - allowed)
    if extra:
        raise Rejected(f"unknown_field:{name}.{_key(extra[0])}")
    return dict(value)


def _is_num(v: Any) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)


def _check_entity(entity: dict[str, Any]) -> None:
    for key, value in entity.items():
        ok = isinstance(value, str)
        if key == "ticker":
            ok = ok and bool(_TICKER_RE.match(value))
        elif key == "section":
            ok = ok and value in SECTIONS
        elif key == "metric":
            ok = ok and bool(_WORD_RE.match(value))
        else:
            ok = ok and bool(_ID_RE.match(value))
        if not ok:
            raise Rejected(f"invalid_entity.{key}")


def _check_props(props: dict[str, Any]) -> None:
    for key, value in props.items():
        if key == "dwell_ms":
            ok = isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 3_600_000
        elif key == "delta":
            ok = _is_num(value) and abs(value) <= 1e9
        elif key in ("action", "reason"):
            ok = isinstance(value, str) and bool(_WORD_RE.match(value))
        elif key == "query_text_hash":
            ok = isinstance(value, str) and bool(_HASH_RE.match(value))
        else:  # topics
            ok = (
                isinstance(value, list)
                and len(value) <= 8
                and all(isinstance(t, str) and _TOPIC_RE.match(t) for t in value)
            )
        if not ok:
            raise Rejected(f"invalid_props.{key}")


def _check_context(ctx: dict[str, Any]) -> None:
    ms = ctx.get("market_session")
    if ms is not None and ms not in _SESSIONS:
        raise Rejected("invalid_context.market_session")
    vb = ctx.get("vix_band")
    if vb is not None and not (isinstance(vb, str) and re.match(r"^[a-z0-9<>\-]{1,16}$", vb)):
        raise Rejected("invalid_context.vix_band")
    es = ctx.get("earnings_season")
    if es is not None and not isinstance(es, bool):
        raise Rejected("invalid_context.earnings_season")


def _check_client(client: dict[str, Any]) -> None:
    for key, value in client.items():
        if not (isinstance(value, str) and 1 <= len(value) <= (32 if key == "device" else 64)):
            raise Rejected(f"invalid_client.{key}")


def _parse_ts(value: Any, now: datetime) -> datetime:
    if not isinstance(value, str):
        raise Rejected("invalid_ts")
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise Rejected("invalid_ts") from None
    if ts.tzinfo is None:
        raise Rejected("invalid_ts")
    ts = ts.astimezone(UTC)
    if ts > now + MAX_FUTURE:
        raise Rejected("ts_in_future")
    if ts < now - MAX_AGE:
        raise Rejected("ts_too_old")
    return ts


def validate_event(raw: Any, now: datetime) -> ValidEvent:
    """Return the cleaned event or raise `Rejected(reason)`."""
    if not isinstance(raw, dict):
        raise Rejected("not_an_object")
    extra = sorted(set(raw) - _TOP_KEYS)
    if extra:
        raise Rejected(f"unknown_field:{_key(extra[0])}")
    try:
        if len(json.dumps(raw, default=str)) > MAX_EVENT_BYTES:
            raise Rejected("event_too_large")
    except (TypeError, ValueError):
        raise Rejected("not_serialisable") from None

    etype = raw.get("type")
    if not isinstance(etype, str) or not etype:
        raise Rejected("missing_type")
    if etype in SERVER_ONLY_TYPES:
        raise Rejected("server_authoritative_type")
    if etype not in CLIENT_TYPES:
        raise Rejected("unknown_type")

    try:
        event_id = UUID(str(raw.get("event_id")))
    except ValueError:
        raise Rejected("invalid_event_id") from None
    ts = _parse_ts(raw.get("ts"), now)

    session_id = raw.get("session_id")
    if session_id is not None and not (isinstance(session_id, str) and 1 <= len(session_id) <= 64):
        raise Rejected("invalid_session_id")
    version = raw.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION or isinstance(version, bool):
        raise Rejected("unsupported_schema_version")

    entity = _obj(raw.get("entity"), "entity", _ENTITY_KEYS)
    props = _obj(raw.get("props"), "props", _PROPS_KEYS)
    context = _obj(raw.get("context"), "context", _CONTEXT_KEYS)
    client = _obj(raw.get("client"), "client", _CLIENT_KEYS)
    _check_entity(entity)
    _check_props(props)
    _check_context(context)
    _check_client(client)
    for key in REQUIRED_ENTITY.get(etype, ()):
        if key not in entity:
            raise Rejected(f"missing_entity.{key}")
    for key in REQUIRED_PROPS.get(etype, ()):
        if key not in props:
            raise Rejected(f"missing_props.{key}")
    return ValidEvent(event_id, ts, session_id, etype, entity, props, context, client)
