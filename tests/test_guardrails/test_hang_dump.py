"""A test stuck holding the GIL past its timeout ends its run with every thread's stack."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

_CONFTEST = """
from tests.support import hang_dump

hang_dump.GRACE_SECONDS = 1.0


def pytest_configure(config):
    config.pluginmanager.register(hang_dump.HangDumpPlugin(), "cruxible-hang-dump")
"""

_STALL = """
import re

import pytest


@pytest.mark.timeout(1)
def test_stalls_holding_the_gil():
    # The regex engine never releases the GIL: pytest-timeout's timer cannot run.
    re.match(r"(a+)+$", "a" * 64 + "b")


def test_finishes():
    pass
"""


def test_a_stall_that_holds_the_gil_dumps_and_exits_after_its_timeout(tmp_path: Path) -> None:
    (tmp_path / "conftest.py").write_text(_CONFTEST)
    (tmp_path / "test_stall.py").write_text(_STALL)
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--timeout",
            "1",
            "--timeout-method=thread",
            "test_stall.py::test_stalls_holding_the_gil",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 0
    assert "Timeout (0:00:02)!" in result.stderr, result.stderr[-2000:]
    assert "test_stalls_holding_the_gil" in result.stderr


@pytest.mark.timeout(5)
def test_the_watchdog_takes_the_mark_over_the_option(request: pytest.FixtureRequest) -> None:
    from tests.support.hang_dump import item_timeout

    assert item_timeout(request.node) == 5.0
