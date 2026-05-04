"""Repository vocabulary guardrails for the 0.2 kit cleanup."""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _tracked_text_files() -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    paths: list[Path] = []
    for raw in completed.stdout.splitlines():
        path = REPO_ROOT / raw
        if not path.exists() or not path.is_file():
            continue
        if raw.startswith("docs/dev/"):
            continue
        if raw == "CHANGELOG.md" or raw.startswith("docs/migrations/"):
            continue
        if raw == "tests/test_guardrails/test_vocabulary.py":
            continue
        if raw.endswith("/data/nvd_kev_cves.json"):
            continue
        if path.suffix.lower() in {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".pyc"}:
            continue
        paths.append(path)
    return paths


def test_no_packaged_cruxible_kits_refs() -> None:
    offenders: list[str] = []
    for path in _tracked_text_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "cruxible_kits." in text or "src/cruxible_kits" in text:
            offenders.append(path.relative_to(REPO_ROOT).as_posix())
    assert offenders == []


def test_no_old_fork_vocabulary() -> None:
    banned = [
        "WorldFork",
        "world_fork",
        "service_fork_world",
        "cruxible_world_fork",
        "ForkSnapshot",
        "fork_snapshot",
        "snapshot_fork",
        "fork --snapshot",
        "world fork",
    ]
    offenders: list[str] = []
    for path in _tracked_text_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        text = path.read_text(encoding="utf-8", errors="ignore")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if any(term in line for term in banned):
                offenders.append(f"{rel}:{line_number}:{line.strip()}")
    assert offenders == []
