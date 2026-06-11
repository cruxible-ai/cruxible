"""Regenerate the checked-in HTTP surface snapshot."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tests.support.http_surface import write_http_surface_snapshot  # noqa: E402


def main() -> None:
    snapshot_path = REPO_ROOT / "tests/goldens/http_surface/http_surface_snapshot.json"
    write_http_surface_snapshot(snapshot_path)
    print(f"Wrote {snapshot_path.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
