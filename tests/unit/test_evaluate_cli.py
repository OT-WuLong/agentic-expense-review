"""The public P14 CLI must protect the frozen test split."""

import subprocess
import sys
from pathlib import Path

from scripts.evaluate import _passes_threshold

ROOT = Path(__file__).resolve().parents[2]


def test_test_split_requires_explicit_final_run(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/evaluate.py"),
            "retrieval",
            "--split",
            "test",
            "--output-dir",
            str(tmp_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "--final-test" in result.stderr
    assert not list(tmp_path.iterdir())


def test_agentic_ablation_cannot_open_frozen_test() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/evaluate_agentic_ablation.py"), "--split", "test"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "invalid choice" in result.stderr


def test_frozen_threshold_rejects_a_regression() -> None:
    assert _passes_threshold(0.25, ">=", 0.30) is False
    assert _passes_threshold(0.0, "==", 0.0) is True
