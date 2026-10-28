from __future__ import annotations

import runpy
from pathlib import Path

import pytest

pytestmark = pytest.mark.memory

evaluation = runpy.run_path(str(Path(__file__).resolve().parents[1] / "evals" / "memory_eval.py"))


@pytest.mark.parametrize("case", evaluation["CASES"], ids=lambda case: case.name)
def test_synthetic_memory_regression(case, tmp_path):
    assert evaluation["run_case"](case, tmp_path / "synthetic-vault") is None
