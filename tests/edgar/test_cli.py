"""CLI helpers (no network, no database)."""

from __future__ import annotations

from app.modules.edgar import cli as edgar_cli
from app.modules.research import cli as research_cli
from tests.edgar.conftest import load_companyfacts_json


def test_trim_companyfacts_keeps_allowlisted_10k_entries_only() -> None:
    full = load_companyfacts_json("AAPL")
    payload = {
        "cik": 1,
        "entityName": "X",
        "facts": {
            "us-gaap": {
                "Revenues": {"units": {"USD": [
                    {"end": "2020-12-31", "val": 1, "form": "10-K"},
                    {"end": "2024-12-31", "val": 2, "form": "10-K"},
                    {"end": "2024-09-30", "val": 3, "form": "10-Q"},
                ]}},
                "NotAllowed": {"units": {"USD": [{"end": "2024-12-31", "val": 4, "form": "10-K"}]}},
            }
        },
    }  # fmt: skip
    out = edgar_cli.trim_companyfacts(payload)
    entries = out["facts"]["us-gaap"]["Revenues"]["units"]["USD"]
    assert [e["val"] for e in entries] == [2] and "NotAllowed" not in out["facts"]["us-gaap"]
    assert edgar_cli.trim_companyfacts(full)["facts"]  # idempotent on already-trimmed fixtures


def test_split_helpers() -> None:
    assert edgar_cli._split("aapl, jnj ,") == ["AAPL", "JNJ"]
    assert edgar_cli._split(None) is None
    assert research_cli._split("kO") == ["KO"]
