"""Round-4 probe support: short state roots (AF_UNIX 103-byte ceiling)."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture()
def short_root(request: pytest.FixtureRequest) -> Path:
    root = Path(tempfile.mkdtemp(prefix=".b2-r4-", dir=REPOSITORY_ROOT))
    request.addfinalizer(lambda: shutil.rmtree(root, ignore_errors=True))
    return root
