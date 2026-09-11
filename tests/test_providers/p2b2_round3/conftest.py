"""Round-3 probe support: short state roots (AF_UNIX 103-byte ceiling)."""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from cruxible_core.server.registry import reset_registry

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def _remove_root(root: Path) -> None:
    shutil.rmtree(root, ignore_errors=True)
    if os.path.lexists(root):
        time.sleep(0.05)
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture()
def short_root(request: pytest.FixtureRequest) -> Path:
    root = Path(tempfile.mkdtemp(prefix=".b2-r3-", dir=REPOSITORY_ROOT))
    request.addfinalizer(lambda: _remove_root(root))
    request.addfinalizer(reset_registry)
    return root
