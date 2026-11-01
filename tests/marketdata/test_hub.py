"""StreamHub: ids, coalescing, per-user isolation, replay."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.modules.marketdata.hub import StreamHub

A, B = uuid4(), uuid4()


def test_quotes_only_reach_subscribers_of_that_symbol_and_coalesce() -> None:
    hub = StreamHub()
    sa, sb = hub.subscribe(A, {"AAPL"}), hub.subscribe(B, {"MSFT"})
    hub.publish_quote("AAPL", {"price": 1})
    hub.publish_quote("AAPL", {"price": 2})
    hub.publish_quote("MSFT", {"price": 9})
    a, b = sa.drain(), sb.drain()
    assert [e.data["price"] for e in a] == [2] and [e.data["price"] for e in b] == [9]  # latest per symbol
    assert sa.drain() == []


def test_user_events_are_private_and_ordered_by_id() -> None:
    hub = StreamHub()
    sa, sb = hub.subscribe(A, set()), hub.subscribe(B, set())
    e1 = hub.publish_user_event(A, "alert", {"id": "x"})
    e2 = hub.publish_user_event(A, "proposal", {"id": "y"})
    assert [e.id for e in sa.drain()] == [e1.id, e2.id] and sb.drain() == []
    with pytest.raises(ValueError, match="unsupported"):
        hub.publish_user_event(A, "quote", {})


def test_overflow_drops_oldest_user_event_only() -> None:
    hub = StreamHub(subscriber_queue=3)
    sub = hub.subscribe(A, set())
    for i in range(5):
        hub.publish_user_event(A, "alert", {"n": i})
    assert [e.data["n"] for e in sub.drain()] == [2, 3, 4] and sub.dropped == 2


def test_replay_returns_latest_quote_per_symbol_and_own_events_after_last_id() -> None:
    hub = StreamHub()
    hub.publish_quote("AAPL", {"p": 1})
    mark = hub.publish_quote("AAPL", {"p": 2}).id
    hub.publish_quote("AAPL", {"p": 3})
    hub.publish_quote("MSFT", {"p": 7})
    hub.publish_quote("NVDA", {"p": 5})  # not this user's symbol
    hub.publish_user_event(A, "alert", {"a": 1})
    hub.publish_user_event(B, "alert", {"b": 1})
    out = hub.replay(A, {"AAPL", "MSFT"}, mark)
    assert [(e.type, e.data) for e in out] == [("quote", {"p": 3}), ("quote", {"p": 7}), ("alert", {"a": 1})]
    assert [e.id for e in out] == sorted(e.id for e in out)
    assert hub.replay(A, {"AAPL"}, 10**9) == []


def test_set_symbols_drops_pending_quotes_and_counts_connections() -> None:
    hub = StreamHub()
    sub = hub.subscribe(A, {"AAPL", "MSFT"})
    hub.publish_quote("AAPL", {})
    hub.publish_quote("MSFT", {})
    sub.set_symbols({"AAPL"})
    assert [e.symbol for e in sub.drain()] == ["AAPL"]
    assert hub.connections_for(A) == 1 and hub.subscriber_count == 1
    hub.unsubscribe(sub)
    assert hub.connections_for(A) == 0


def test_event_encoding_is_sse() -> None:
    hub = StreamHub()
    ev = hub.publish_quote("AAPL", {"price": 1.5})
    assert ev.encode() == f'id: {ev.id}\nevent: quote\ndata: {{"price":1.5}}\n\n'.encode()
    assert hub.heartbeat({"ts": "x"}).id > ev.id
