"""The ``pilot`` fixture: a transcript and cost rows per scenario, under a scratch HOME.

Mark a scenario ``@pytest.mark.pilot("name", owner="...", title="...")`` and take
the ``pilot`` fixture. When the test ends, its transcript goes to
``$PILOT_OUT/<name>.txt`` and its rows to ``$PILOT_OUT/results.tsv`` with the
outcome pytest reports (see ``harness.py``).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.pilot_contract_batch.harness import Scenario, isolate_home
from tests.pilot_contract_batch.surfaces import SurfaceMissing


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "pilot(name, owner, title): a contract-batch pilot scenario")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> Iterator[None]:
    outcome = yield
    report = outcome.get_result()  # type: ignore[attr-defined]
    if report.when == "call" or (report.when == "setup" and not report.passed):
        item._pilot_report = report  # type: ignore[attr-defined]
        item._pilot_excinfo = call.excinfo  # type: ignore[attr-defined]


def _status(report: Any) -> str:
    if report is None:
        return "error"
    if getattr(report, "wasxfail", None) is not None and report.skipped:
        return "xfailed"
    if report.passed:
        return "passed"
    if report.skipped:
        return "skipped"
    return "failed"


def _stopped(excinfo: Any) -> str | None:
    if excinfo is None:
        return None
    value = excinfo.value
    if isinstance(value, SurfaceMissing):
        return str(value)
    first = (str(value).strip().splitlines() or [""])[0]
    return f"{type(value).__name__}: {first[:300]}"


@pytest.fixture
def pilot(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[Scenario]:
    marker = request.node.get_closest_marker("pilot")
    if marker is None:
        raise RuntimeError("a pilot scenario needs @pytest.mark.pilot(name, owner=, title=)")
    scenario = Scenario(
        name=marker.args[0],
        owner=marker.kwargs["owner"],
        title=marker.kwargs["title"],
        expected=request.module.__doc__ or "",
    )
    isolate_home(monkeypatch, tmp_path)
    yield scenario
    report = getattr(request.node, "_pilot_report", None)
    scenario.write(_status(report), _stopped(getattr(request.node, "_pilot_excinfo", None)))
