"""SPEC 10.2 event validation (pure)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from app.modules.events.validation import CLIENT_TYPES, SERVER_ONLY_TYPES, Rejected, validate_event

NOW = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)


def ev(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "event_id": str(uuid4()),
        "ts": "2026-10-30T14:59:00Z",
        "session_id": "s-1",
        "type": "ticker_viewed",
        "entity": {"ticker": "NVDA"},
        "props": {},
        "context": {"market_session": "regular", "vix_band": "15-25", "earnings_season": True},
        "client": {"device": "desktop", "tz": "America/New_York"},
        "schema_version": 1,
    }
    base.update(over)
    return base


def reason(raw: Any) -> str:
    with pytest.raises(Rejected) as exc:
        validate_event(raw, NOW)
    return exc.value.reason


def test_valid_event_roundtrip() -> None:
    v = validate_event(ev(), NOW)
    assert (
        v.type == "ticker_viewed"
        and v.entity == {"ticker": "NVDA"}
        and v.ts == datetime(2026, 10, 30, 14, 59, tzinfo=UTC)
    )


@pytest.mark.parametrize("etype", sorted(CLIENT_TYPES))
def test_every_p0_client_type_is_accepted_with_its_required_fields(etype: str) -> None:
    entity = {"ticker": "AAPL", "section": "bal", "alert_id": "al-1", "metric": "pe"}
    props = {"dwell_ms": 9000}
    assert validate_event(ev(type=etype, entity=entity, props=props), NOW).type == etype


def test_unknown_and_server_authoritative_types_are_rejected() -> None:
    assert reason(ev(type="launch_missiles")) == "unknown_type"
    assert reason(ev(type="")) == "missing_type"
    assert reason(ev(type=None)) == "missing_type"
    for t in SERVER_ONLY_TYPES:
        assert reason(ev(type=t)) == "server_authoritative_type"


@pytest.mark.parametrize(
    ("over", "expected"),
    [
        ({"event_id": "nope"}, "invalid_event_id"),
        ({"event_id": None}, "invalid_event_id"),
        ({"ts": "yesterday"}, "invalid_ts"),
        ({"ts": "2026-10-30T14:59:00"}, "invalid_ts"),  # naive
        ({"ts": 12345}, "invalid_ts"),
        ({"ts": "2026-10-30T15:06:00Z"}, "ts_in_future"),
        ({"ts": "2026-09-01T00:00:00Z"}, "ts_too_old"),
        ({"schema_version": 2}, "unsupported_schema_version"),
        ({"schema_version": True}, "unsupported_schema_version"),
        ({"session_id": ""}, "invalid_session_id"),
        ({"session_id": "x" * 65}, "invalid_session_id"),
        ({"entity": []}, "invalid_entity"),
        ({"entity": {"ticker": "nvda!"}}, "invalid_entity.ticker"),
        ({"entity": {"ticker": "NVDA", "colour": "red"}}, "unknown_field:entity.colour"),
        ({"surprise": 1}, "unknown_field:surprise"),
        (
            {"props": {"dwell_ms": -1}, "type": "section_dwell", "entity": {"section": "bal"}},
            "invalid_props.dwell_ms",
        ),
        (
            {"props": {"dwell_ms": 3_600_001}, "type": "section_dwell", "entity": {"section": "bal"}},
            "invalid_props.dwell_ms",
        ),
        (
            {"props": {"dwell_ms": True}, "type": "section_dwell", "entity": {"section": "bal"}},
            "invalid_props.dwell_ms",
        ),
        ({"props": {"delta": float("nan")}}, "invalid_props.delta"),
        ({"props": {"action": "<script>"}}, "invalid_props.action"),
        ({"props": {"topics": ["ok"] * 9}}, "invalid_props.topics"),
        ({"props": {"topics": ["Bad Topic"]}}, "invalid_props.topics"),
        ({"props": {"free_text": "my SSN is..."}}, "unknown_field:props.free_text"),
        ({"context": {"market_session": "lunch"}}, "invalid_context.market_session"),
        ({"context": {"earnings_season": "yes"}}, "invalid_context.earnings_season"),
        ({"client": {"device": ""}}, "invalid_client.device"),
        (
            {"type": "section_dwell", "entity": {"section": "nope"}, "props": {"dwell_ms": 5}},
            "invalid_entity.section",
        ),
    ],
)
def test_invalid_events_give_a_specific_reason(over: dict[str, Any], expected: str) -> None:
    assert reason(ev(**over)) == expected


def test_missing_required_fields_per_type() -> None:
    assert reason(ev(entity={})) == "missing_entity.ticker"
    assert reason(ev(type="section_dwell", entity={"section": "bal"}, props={})) == "missing_props.dwell_ms"
    assert reason(ev(type="alert_opened", entity={})) == "missing_entity.alert_id"
    assert reason(ev(type="metric_clicked", entity={"ticker": "AAPL"})) == "missing_entity.metric"


def test_not_an_object_and_size_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    assert reason("hello") == "not_an_object"
    assert reason(None) == "not_an_object"
    longest = ev(
        props={
            "topics": ["a" * 24] * 8,
            "query_text_hash": "a" * 128,
            "action": "a" * 64,
            "reason": "b" * 64,
        },
        client={"device": "d" * 32, "tz": "z" * 64},
        session_id="s" * 64,
    )
    assert (
        validate_event(longest, NOW).session_id == "s" * 64
    )  # the largest legal event is well under the cap
    monkeypatch.setattr("app.modules.events.validation.MAX_EVENT_BYTES", 100)
    assert reason(longest) == "event_too_large"


def test_reasons_never_echo_input() -> None:
    secret = "SUPERSECRETVALUE"
    for over in ({"type": secret}, {"event_id": secret}, {"ts": secret}, {"entity": {"ticker": secret}}):
        assert "SUPERSECRET" not in reason(ev(**over))
    assert reason(ev(props={secret: 1})) == "unknown_field:props.?"  # odd key names are not echoed back
    assert reason(ev(props={"free_text": 1})) == "unknown_field:props.free_text"
