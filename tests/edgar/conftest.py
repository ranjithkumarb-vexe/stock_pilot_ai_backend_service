"""Fixtures for EDGAR tests: recorded (trimmed) real `companyfacts` for AAPL, JNJ, NVDA."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from app.modules.edgar.xbrl import FactSet, parse_companyfacts

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "edgar"


def load_companyfacts_json(symbol: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"companyfacts_{symbol}.json").read_text())
    return data


@pytest.fixture(scope="session")
def factset() -> Callable[[str], FactSet]:
    cache: dict[str, FactSet] = {}

    def _get(symbol: str) -> FactSet:
        if symbol not in cache:
            cache[symbol] = FactSet(parse_companyfacts(load_companyfacts_json(symbol)))
        return cache[symbol]

    return _get
