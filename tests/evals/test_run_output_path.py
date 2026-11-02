"""CR-REL-3: a ``--fast`` eval run must never overwrite the committed full-run ``evals/results/latest.json``."""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.framework import LATEST, RESULTS_DIR
from evals.run import FAST_LATEST, parse_args, resolve_out


def test_full_run_writes_latest_json() -> None:
    args = parse_args([])
    assert resolve_out(args) == LATEST == RESULTS_DIR / "latest.json"


def test_fast_run_defaults_to_a_separate_file() -> None:
    args = parse_args(["--fast"])
    out = resolve_out(args)
    assert out == FAST_LATEST == RESULTS_DIR / "latest.fast.json"
    assert out != LATEST


def test_fast_run_can_write_elsewhere_with_explicit_out(tmp_path: Path) -> None:
    args = parse_args(["--fast", "--out", str(tmp_path / "x.json")])
    assert resolve_out(args) == tmp_path / "x.json"


def test_fast_run_refuses_to_target_latest_json() -> None:
    args = parse_args(["--fast", "--out", str(LATEST)])
    with pytest.raises(SystemExit) as exc:
        resolve_out(args)
    assert exc.value.code == 2


def test_full_run_may_target_any_path(tmp_path: Path) -> None:
    assert resolve_out(parse_args(["--out", str(tmp_path / "full.json")])) == tmp_path / "full.json"
