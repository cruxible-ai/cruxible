"""Round-3 probe support: short state roots (AF_UNIX 103-byte ceiling)."""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

WORKTREE = "/Users/robertmalone/Git/p2-worktrees/p2b2"
if WORKTREE not in sys.path:
    sys.path.insert(0, WORKTREE)


@pytest.fixture()
def short_root(request: pytest.FixtureRequest) -> Path:
    root = Path(tempfile.mkdtemp(prefix=".r3-", dir=Path(WORKTREE)))
    request.addfinalizer(lambda: shutil.rmtree(root, ignore_errors=True))
    return root
