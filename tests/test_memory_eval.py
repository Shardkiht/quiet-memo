from __future__ import annotations

import runpy
from pathlib import Path

import pytest

pytestmark = pytest.mark.memory

evaluation = runpy.run_path(str(Path(__file__).resolve().parents[1] / "evals" / "memory_eval.py"))


@pytest.mark.parametrize("case", evaluation["CASES"], ids=lambda case: case.name)
def test_synthetic_memory_regression(case, tmp_path):
    assert evaluation["run_case"](case, tmp_path / "synthetic-vault") is None


def test_contextual_recall_synthetic_comparison(tmp_path):
    result = evaluation["run_contextual_eval"](tmp_path / "contextual-vault")
    assert result["contextual"] == {"Recall@1": 1.0, "MRR@5": 1.0}
    assert result["baseline"]["Recall@1"] < result["contextual"]["Recall@1"]
    assert result["baseline"]["MRR@5"] < result["contextual"]["MRR@5"]
