"""The candidate-card vector updater executes and is byte-idempotent."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_candidate_card_updater_executes_byte_idempotently(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "tests/goldens/playbill").mkdir(parents=True)
    shutil.copy2(
        ROOT / "scripts/update_playbill_card_vectors.py",
        checkout / "scripts/update_playbill_card_vectors.py",
    )
    shutil.copy2(
        ROOT / "tests/goldens/playbill/card-renderer-v1.json",
        checkout / "tests/goldens/playbill/card-renderer-v1.json",
    )
    before = (checkout / "tests/goldens/playbill/card-renderer-v1.json").read_bytes()

    completed = subprocess.run(
        [sys.executable, str(checkout / "scripts/update_playbill_card_vectors.py")],
        cwd=checkout,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert (checkout / "tests/goldens/playbill/card-renderer-v1.json").read_bytes() == before
