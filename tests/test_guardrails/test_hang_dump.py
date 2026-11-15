"""A test stuck holding the GIL past its timeout ends its run with every thread's stack."""

from __future__ import annotations

import os
import re
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


@pytest.fixture
def stalls_in_teardown():
    yield
    re.match(r"(a+)+$", "a" * 64 + "b")


@pytest.mark.timeout(1)
def test_fails_then_stalls_in_teardown(stalls_in_teardown):
    # pytest cancels faulthandler's timer on a failure; the teardown must stay covered.
    assert False
"""


@pytest.mark.parametrize(
    ("test", "frame"),
    [
        ("test_stalls_holding_the_gil", "test_stalls_holding_the_gil"),
        ("test_fails_then_stalls_in_teardown", "stalls_in_teardown"),
    ],
)
def test_a_stall_that_holds_the_gil_dumps_and_exits_after_its_timeout(
    tmp_path: Path, test: str, frame: str
) -> None:
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
            f"test_stall.py::{test}",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 0
    # The watchdog's own header, about 2 s in (the timeout plus a grace of 1 s).
    assert re.search(r"Timeout \(0:00:0[12][.0-9]*\)!", result.stderr), result.stderr[-2000:]
    assert frame in result.stderr


@pytest.mark.timeout(5)
def test_the_watchdog_takes_the_mark_over_the_option(request: pytest.FixtureRequest) -> None:
    from tests.support.hang_dump import item_timeout

    assert item_timeout(request.node) == 5.0
