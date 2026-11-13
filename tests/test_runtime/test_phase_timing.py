"""Each write-side operation logs one line naming where its time went."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
from cruxible_core.ledger import git
from cruxible_core.runtime import phase_timing
from cruxible_core.runtime.phase_timing import PHASES, operation_timing, phase
from tests.core_support._write_support import KIND, seed_write_surface
from tests.test_floor.test_floor_index import LEAD, _set, _write, accept_edit

WI1 = f"{KIND}/wi-1"


def _lines(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [entry for entry in logs if entry["event"] == "write_phases"]


def test_a_write_logs_one_line_with_every_phase(tmp_path: Path) -> None:
    instance, _owner = seed_write_surface(tmp_path)
    with capture_logs() as logs:
        _write(instance, _set(WI1, "status", "ready"))
    (line,) = _lines(logs)
    assert (line["operation"], line["status"]) == ("write", "ok")
    # In-process Git plumbing can leave the subprocess phase at zero; every
    # phase must still be present, including phases that round down to zero.
    for name in (*PHASES, "other", "total"):
        assert line[f"{name}_ms"] >= 0, name  # type: ignore[operator]
    claimed = sum(float(line[f"{name}_ms"]) for name in PHASES) + float(line["other_ms"])  # type: ignore[arg-type]
    assert abs(claimed - float(line["total_ms"])) < 0.05  # type: ignore[arg-type]


def test_a_git_command_is_still_measured(monkeypatch: pytest.MonkeyPatch) -> None:
    instants = iter((0, 1_000_000, 3_000_000, 4_000_000))
    monkeypatch.setattr(phase_timing.time, "perf_counter_ns", lambda: next(instants))
    monkeypatch.setattr(
        git.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, stdout=b"", stderr=b""),
    )
    with capture_logs() as logs, operation_timing("write"):
        git._command(["git", "--version"])
    (line,) = _lines(logs)
    assert line["git_ms"] == 2.0
    assert line["other_ms"] == 2.0
    assert line["total_ms"] == 4.0
    assert all(line[f"{name}_ms"] == 0.0 for name in PHASES if name != "git")


def test_a_proposal_and_its_activation_each_log_their_own_line(tmp_path: Path) -> None:
    instance, _owner = seed_write_surface(tmp_path)
    with capture_logs() as logs:
        accept_edit(
            instance, "lead-field", {claim_type_path(LEAD.predicate): render_claim_type(LEAD)}
        )
    assert [line["operation"] for line in _lines(logs)] == ["propose", "activate"]


def test_phases_nest_exclusively_and_a_failure_still_logs() -> None:
    with capture_logs() as logs:
        try:
            with operation_timing("write"), phase("admission"), phase("evaluate"):
                raise ValueError("refused")
        except ValueError:
            pass
        with phase("plan"):
            pass  # outside any operation: nothing is timed or logged
    (line,) = _lines(logs)
    assert line["status"] == "ValueError"
    assert line["admission_ms"] >= 0 and line["evaluate_ms"] >= 0  # type: ignore[operator]
