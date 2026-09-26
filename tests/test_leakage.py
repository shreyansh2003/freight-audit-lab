"""The audit must never see the answer key.

Two checks (SPEC Stage 4): (1) no audit-side source file mentions the answer key's folder or file;
(2) running normalize -> rerate -> audit -> baseline on a copy of the data with the answer key
removed produces exactly the same results as running it with the key present.
Only evaluate.py and sweep.py may read `data/ground_truth/`.
"""

import shutil
from pathlib import Path

import pandas as pd

from tests.fixtures import run_pipeline

PACKAGE = Path(__file__).resolve().parent.parent / "freight_audit_lab"
FORBIDDEN = ["ground_truth", "labels.csv"]
# Modules that must stay blind to the answer key. Modules from later stages are checked once they exist.
AUDIT_SIDE = ["normalize.py", "rerate.py", "audit", "exceptions.py", "accruals.py"]


def audit_side_files():
    files = []
    for name in AUDIT_SIDE:
        path = PACKAGE / name
        files += sorted(path.rglob("*.py")) if path.is_dir() else [path] if path.exists() else []
    return files


def test_audit_side_modules_never_mention_the_answer_key():
    files = audit_side_files()
    assert {f.name for f in files} >= {"normalize.py", "rerate.py", "rules.py", "engine.py", "baseline.py"}
    offenders = [(f.name, word) for f in files for word in FORBIDDEN if word in f.read_text()]
    assert not offenders, f"these files mention the answer key: {offenders}"


def test_results_are_identical_with_the_answer_key_removed(full, cfg, audited, tmp_path):
    _, data_dir = full
    assert (data_dir / "ground_truth" / "labels.csv").exists()
    keyless = tmp_path / "data"
    shutil.copytree(data_dir, keyless, ignore=shutil.ignore_patterns("ground_truth"))
    assert not (keyless / "ground_truth").exists()

    with_key, without_key = audited, run_pipeline(keyless, cfg)
    assert with_key.keys() == without_key.keys()
    for name in with_key:
        pd.testing.assert_frame_equal(with_key[name], without_key[name], obj=name)
    assert len(with_key["audit_flags"]) > 0


def test_only_the_generator_evaluate_and_sweep_mention_the_answer_key():
    """CLAUDE.md rule 2: outside generate/ (which writes the key), only evaluate.py and sweep.py may name it,
    and sweep.py reads it through evaluate.load_labels."""
    allowed = {"evaluate.py", "sweep.py"}
    offenders = [str(f.relative_to(PACKAGE)) for f in sorted(PACKAGE.rglob("*.py"))
                 if f.relative_to(PACKAGE).parts[0] != "generate" and f.name not in allowed
                 and any(word in f.read_text() for word in FORBIDDEN)]
    assert not offenders, f"these files mention the answer key: {offenders}"
    assert "read_csv" not in (PACKAGE / "sweep.py").read_text()      # loads labels only via evaluate.load_labels
