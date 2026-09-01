"""The checked-in Playbill wire updater must execute and be byte-idempotent."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_playbill_wire_updater_executes_byte_idempotently(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "tests/goldens/playbill").mkdir(parents=True)
    (checkout / "benchmarks/playbill_taubench").mkdir(parents=True)
    shutil.copy2(
        ROOT / "scripts/update_playbill_wire_goldens.py",
        checkout / "scripts/update_playbill_wire_goldens.py",
    )
    for name in (
        "claim-type-v1.json",
        "candidate-v1.json",
        "candidate-v2.json",
        "depgraph-v3.json",
        "merkle-manifest-v1.json",
        "p2-b0-artifact-codec-v1.json",
        "query-definition-v1.json",
        "changeset-v3.json",
        "subject-v1.json",
        "semantic-genesis-v1.json",
        "source-reference-v1.json",
    ):
        shutil.copy2(
            ROOT / "tests/goldens/playbill" / name,
            checkout / "tests/goldens/playbill" / name,
        )
    shutil.copytree(
        ROOT / "benchmarks/playbill_taubench/seed-example",
        checkout / "benchmarks/playbill_taubench/seed-example",
    )
    before = _snapshot(checkout)

    completed = subprocess.run(
        [sys.executable, str(checkout / "scripts/update_playbill_wire_goldens.py")],
        cwd=checkout,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert _snapshot(checkout) == before
