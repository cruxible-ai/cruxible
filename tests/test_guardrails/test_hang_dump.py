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


def test_the_job_deadline_caps_a_late_test_s_own(tmp_path: Path) -> None:
    """A hang due after the CI job ends is dumped at the job's deadline instead."""

    import time

    (tmp_path / "conftest.py").write_text(_CONFTEST)
    (tmp_path / "test_stall.py").write_text(_STALL.replace("timeout(1)", "timeout(600)"))
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO),
        "CRUXIBLE_TEST_DEADLINE": str(time.time() + 3),
    }
    started = time.monotonic()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--timeout",
            "600",
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
    assert re.search(r"Timeout \(0:00:0[0-9.]*\)!", result.stderr), result.stderr[-2000:]
    assert "test_stalls_holding_the_gil" in result.stderr
    assert time.monotonic() - started < 60


@pytest.mark.timeout(5)
def test_the_watchdog_takes_the_mark_over_the_option(request: pytest.FixtureRequest) -> None:
    from tests.support.hang_dump import item_timeout

    assert item_timeout(request.node) == 5.0
