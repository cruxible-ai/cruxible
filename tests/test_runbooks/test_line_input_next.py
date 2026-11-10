"""``next`` names a needed workspace file that is not there, from state.

Every enabled or Runbook-named Line's workspace-file inputs come from an index
kept as definitions are accepted, plus one stat per indexed input: setup
before the Line ever ran, repair once a run read the file and it vanished.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.captures import CanonicalDuration
from cruxible_core.coverage.contracts import CoverageAccessProfile
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.service.discovery.next import NextRequest, service_playbill_next
from tests.support.runbooks import (
    FAIL,
    SETTLE,
    author_runbook,
    line_steps,
    readers,
    runbook_world,
)
from tests.test_runbooks.test_line_input_refusal import _run
from tests.test_runbooks.test_runbook_preview import DESCRIBED_LINE, DOC_HELP, _described

EVALUATION_TIME = datetime(2026, 10, 30, 12, tzinfo=UTC)


# -- next ---------------------------------------------------------------------------


def _request(instance: Any) -> NextRequest:
    return NextRequest(
        at=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        evaluation_time=EVALUATION_TIME,
        access_profile=CoverageAccessProfile(
            profile_id="needed-inputs", permitted_access_classes=("instance", "public")
        ),
        expiring_within=CanonicalDuration(microseconds=604_800_000_000),
    )


def _rows(instance: Any, root: Path) -> list[Any]:
    result = service_playbill_next(
        instance,
        request=_request(instance),
        workspace_file_reader=readers(instance, root)["workspace_file_reader"],
    )
    return [item for item in result.items if item.reason == "line_input_missing"]


def test_next_names_a_missing_input_of_a_runbook_line_until_the_file_is_placed(
    tmp_path: Path,
) -> None:
    instance, root = runbook_world(tmp_path)
    _described(instance)
    (root / "data" / "doc.json").unlink()
    # Neither Line is enabled: nothing to run them, so nothing is owed yet.
    assert _rows(instance, root) == []
    author_runbook(instance, "test.rb", line_steps(DESCRIBED_LINE, FAIL))
    rows = {row.subject_identity: row for row in _rows(instance, root)}
    assert set(rows) == {f"Line:{DESCRIBED_LINE}", f"Line:{FAIL}"}
    row = rows[f"Line:{DESCRIBED_LINE}"]
    # Never ran: the file was never supplied, a setup step.
    assert row.severity == "setup"
    assert (row.detail["relative_path"], row.detail["parameter"], row.detail["description"]) == (
        "data/doc.json",
        "path",
        DOC_HELP,
    )
    assert rows[f"Line:{FAIL}"].detail["description"] is None
    assert row.repair.operation == "hand_edit"
    assert row.repair.required_change.startswith(
        "Place the file this Line reads at data/doc.json in the attached workspace"
    )
    # Placing the file closes the row; removing it opens it again.
    (root / "data" / "doc.json").write_text('{"entries": [], "count": 0}')
    assert {row.subject_identity for row in _rows(instance, root)} == {f"Line:{FAIL}"}
    (root / "data" / "doc.json").unlink()
    assert len(_rows(instance, root)) == 2
    # Without a daemon reader no row is checked.
    result = service_playbill_next(instance, request=_request(instance))
    assert not [item for item in result.items if item.reason == "line_input_missing"]


def test_a_file_gone_after_a_run_is_repair_work(tmp_path: Path) -> None:
    instance, root = runbook_world(tmp_path)
    author_runbook(instance, "test.rb", line_steps(SETTLE))
    assert _rows(instance, root) == []
    assert _run(instance, root, SETTLE).status == "succeeded"
    (root / "data" / "doc.json").unlink()
    (row,) = _rows(instance, root)
    assert (row.subject_identity, row.severity) == (f"Line:{SETTLE}", "repair")
    assert "gone since it last ran" in row.detail["detail"]


def test_next_stats_each_indexed_input_once_and_lists_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    from cruxible_core.documents.workspace_file import WorkspaceFileReader

    instance, root = runbook_world(tmp_path)
    author_runbook(instance, "test.rb", line_steps(SETTLE, FAIL))
    calls: list[str] = []
    real = WorkspaceFileReader.presence

    def counted(self: Any, relative_path: str, **kwargs: Any) -> Any:
        calls.append(relative_path)
        return real(self, relative_path, **kwargs)

    def no_listing(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("next listed a workspace directory")

    monkeypatch.setattr(WorkspaceFileReader, "presence", counted)
    reader = readers(instance, root)["workspace_file_reader"]
    monkeypatch.setattr(os, "scandir", no_listing)
    service_playbill_next(instance, request=_request(instance), workspace_file_reader=reader)
    # One stat per indexed input of a Runbook-named Line: never a scan.
    assert sorted(calls) == ["data/doc.json", "data/missing.json"]


def test_the_index_folds_only_changed_lines(tmp_path: Path) -> None:
    from cruxible_core.consumers.next import line_inputs

    instance, root = runbook_world(tmp_path)
    line_inputs.catch_up(instance)
    blobs: list[str] = []
    real = type(instance).blob_at

    def counted(self: Any, git_oid: str, path: str) -> Any:
        blobs.append(path)
        return real(self, git_oid, path)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(type(instance), "blob_at", counted)
        line_inputs.catch_up(instance)
        assert blobs == []
    author_runbook(instance, "test.rb", line_steps(SETTLE))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(type(instance), "blob_at", counted)
        line_inputs.catch_up(instance)
    # The new Runbook only: no Line, Procedure or Contract is read again.
    assert blobs == ["runbooks/test.rb.json"]
