"""A Line's needed workspace file, named when its run refuses to find it.

The run's refusal of a file that is not there names the path, the Line
parameter it comes from and the author's description, and a repair that says
where to place the file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from cruxible_client import contracts
from cruxible_client.contracts.repairs import HandEditRepair
from tests.support.runbooks import (
    FAIL,
    readers,
    runbook_world,
)
from tests.test_runbooks.test_runbook_preview import DESCRIBED_LINE, DOC_HELP, _described


def _run(
    instance: Any, root: Path, line: str, *, reader: bool = True
) -> contracts.ProcedureRunState:
    from datetime import timedelta

    from cruxible_core.service.procedures.procedure_runs import (
        LineRunRequest,
        service_run_playbill_line,
    )
    from tests.support.operation_world import NOW, actor
    from tests.test_procedures.test_completeness_transfer import _Clock

    served = readers(instance, root)
    result = service_run_playbill_line(
        instance,
        path_identity_digest=line,
        request=LineRunRequest(line=line),
        actor_context=actor(),
        caller_rung=3,
        daemon_clock=_Clock(NOW + timedelta(minutes=1)),
        provider_runtime_operator=served["provider_runtime_operator"],
        workspace_file_reader=served["workspace_file_reader"] if reader else None,
    )
    return contracts.ProcedureRunState.model_validate(result.model_dump(mode="json"))


def test_a_missing_file_refusal_names_the_path_its_parameter_and_where_to_put_it(
    tmp_path: Path,
) -> None:
    from click.testing import CliRunner

    from cruxible_core.cli.commands.playbill import _echo_run_outcome

    instance, root = runbook_world(tmp_path)
    _described(instance)
    (root / "data" / "doc.json").unlink()
    state = _run(instance, root, DESCRIBED_LINE)
    assert state.status == "node_refused"
    terminal = state.terminal
    assert terminal.code == "workspace_file_read_refused"
    assert terminal.message == "workspace source is unavailable: data/doc.json"
    details = terminal.details
    assert (details["path_class"], details["relative_path"], details["parameter"]) == (
        "missing",
        "data/doc.json",
        "path",
    )
    assert details["description"] == DOC_HELP
    placement = (
        "Place the file this Line reads at data/doc.json in the attached workspace "
        f"(parameter path: {DOC_HELP}), then run it again."
    )
    assert details["repair_commands"][0] == placement
    assert isinstance(terminal.repair, HandEditRepair)
    assert terminal.repair.hand_edit.required_change == placement

    import click

    @click.command()
    def show() -> None:
        _echo_run_outcome(state, "Run")

    output = CliRunner().invoke(show).output
    assert (
        "Refused: workspace_file_read_refused: workspace source is unavailable: data/doc.json"
        in output
    )
    assert f"Repair: hand edit workspace file data/doc.json: {placement}" in output


def test_a_refusal_with_no_workspace_names_the_path_and_an_undescribed_parameter(
    tmp_path: Path,
) -> None:
    instance, root = runbook_world(tmp_path)
    state = _run(instance, root, FAIL, reader=False)
    details = state.terminal.details
    assert (details["path_class"], details["relative_path"], details["parameter"]) == (
        "binding",
        "data/missing.json",
        "path",
    )
    # The author wrote no description: none is invented.
    assert "description" not in details
    assert details["repair_commands"][0].startswith(
        "Place the file this Line reads at data/missing.json in a workspace attached"
    )
    assert "(parameter path)" in details["repair_commands"][0]


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="root reads a mode-000 file, so no read access is denied",
)
def test_preview_and_a_real_run_agree_on_a_file_without_read_access(tmp_path: Path) -> None:
    from cruxible_core.service.runbook_preview import service_preview_runbook
    from tests.support.runbooks import author_runbook, line_steps

    instance, root = runbook_world(tmp_path)
    _described(instance)
    author_runbook(instance, "test.rb", line_steps(DESCRIBED_LINE))
    source = root / "data" / "doc.json"
    source.chmod(0)
    try:
        reader = readers(instance, root)["workspace_file_reader"]
        assert reader.presence("data/doc.json") == ("refused", "unreadable")
        preview = service_preview_runbook(instance, name="test.rb", workspace_file_reader=reader)
        (item,) = preview.steps[0].inputs
        assert (item.state, item.refused) == ("refused", "unreadable")
        state = _run(instance, root, DESCRIBED_LINE)
        assert state.status == "node_refused"
        assert state.terminal.details["path_class"] == "unreadable"
    finally:
        source.chmod(0o600)
