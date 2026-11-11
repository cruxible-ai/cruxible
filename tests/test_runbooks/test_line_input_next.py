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
SETTLE_KEY = f"Line:{SETTLE}"
FAIL_KEY = f"Line:{FAIL}"


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


def test_an_enabled_line_outside_any_runbook_is_checked_until_disabled(tmp_path: Path) -> None:
    from cruxible_client.contracts.line_dispatch import LineEnablementPrincipal
    from cruxible_core.service.procedures.line_dispatch import (
        service_disable_line,
        service_enable_line,
    )
    from tests.support.operation_world import NOW, actor

    instance, root = runbook_world(tmp_path)
    _described(instance)
    (root / "data" / "doc.json").unlink()
    assert _rows(instance, root) == []
    service_enable_line(
        instance,
        DESCRIBED_LINE,
        principal=LineEnablementPrincipal.model_validate(
            {"kind": "principal_claim", "label": "owner"}
        ),
        actor=actor(),
        now=NOW,
        daemon_id="needed-inputs",
    )
    assert [row.subject_identity for row in _rows(instance, root)] == [f"Line:{DESCRIBED_LINE}"]
    service_disable_line(instance, DESCRIBED_LINE, actor=actor(), now=NOW)
    assert _rows(instance, root) == []


def _measured_read(instance: Any, root: Path) -> dict[str, int]:
    """One warm read of the needed inputs: SQL statements and VM steps on the index
    and the dispatch projection, and presence calls."""

    from collections import Counter
    from contextlib import contextmanager

    from cruxible_core.consumers.next import line_inputs
    from cruxible_core.documents.workspace_file import WorkspaceFileReader
    from cruxible_core.exhaust.line_dispatch import LineDispatchStore
    from cruxible_core.service.discovery.next import _line_input_items

    count: Counter[str] = Counter()

    def statement(_sql: str) -> None:
        count["sql_statements"] += 1

    def step() -> int:
        count["sql_vm_steps"] += 1
        return 0

    def traced(connection: Any) -> Any:
        connection.set_trace_callback(statement)
        connection.set_progress_handler(step, 1)
        return connection

    def untraced(connection: Any) -> None:
        connection.set_trace_callback(None)
        connection.set_progress_handler(None, 0)

    real_open = line_inputs._STATE.open
    real_locked = LineDispatchStore.locked
    real_presence = WorkspaceFileReader.presence

    @contextmanager
    def state_open(*args: Any, **kwargs: Any) -> Any:
        with real_open(*args, **kwargs) as connection:
            if connection is None:
                yield None
                return
            try:
                yield traced(connection)
            finally:
                untraced(connection)

    @contextmanager
    def locked(self: Any) -> Any:
        with real_locked(self) as connection:
            try:
                yield traced(connection)
            finally:
                untraced(connection)

    def presence(self: Any, *args: Any, **kwargs: Any) -> Any:
        count["presence_calls"] += 1
        return real_presence(self, *args, **kwargs)

    # Warm the fold first: a changed definition is fold work, not read work.
    line_inputs.catch_up(instance)
    reader = readers(instance, root)["workspace_file_reader"]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(line_inputs._STATE, "open", state_open)
        patch.setattr(LineDispatchStore, "locked", locked)
        patch.setattr(WorkspaceFileReader, "presence", presence)
        found = _line_input_items(
            instance, workspace_file_reader=reader, access_profile=_request(instance).access_profile
        )
    assert found == ()
    return dict(count)


def test_lines_neither_enabled_nor_runbook_steps_cost_a_read_nothing(tmp_path: Path) -> None:
    """20 or 2,000 disabled workspace-reading Lines outside any Runbook: the same work."""

    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_client.contracts.procedures.line_specs import (
        line_spec_path,
        parse_line_spec,
        render_line_spec,
    )
    from tests.core_support._identity_support import accept_tree

    instance, root = runbook_world(tmp_path)
    author_runbook(instance, "test.rb", line_steps(SETTLE))
    template_path = line_spec_path(FAIL)
    template = parse_line_spec(
        instance.blob_at(instance.accepted_coordinate().git_oid, template_path),
        path=template_path,
    )
    measured: dict[int, dict[str, int]] = {}
    previous = 0
    for size in (20, 2000):
        updates = {}
        for index in range(previous, size):
            name = f"test.unrelated-{index:04d}"
            clone = template.model_copy(
                update={"identity": ArtifactIdentity(kind="Line", name=name)}
            )
            updates[line_spec_path(name)] = render_line_spec(clone)
        accept_tree(instance, updates, name=f"unrelated-{size}")
        measured[size] = _measured_read(instance, root)
        previous = size
    # One stat for the one Runbook Line, and SQL work flat in the unrelated population.
    assert measured[20]["presence_calls"] == measured[2000]["presence_calls"] == 1
    assert measured[2000]["sql_statements"] == measured[20]["sql_statements"]
    assert measured[2000]["sql_vm_steps"] == measured[20]["sql_vm_steps"]


def test_live_runbooks_sharing_one_line_cost_a_read_the_same(tmp_path: Path) -> None:
    """20 or 2,000 live Runbooks naming one Line whose one input is there: the same work."""

    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_client.contracts.runbooks import parse_runbook, render_runbook, runbook_path
    from tests.core_support._identity_support import accept_tree

    instance, root = runbook_world(tmp_path)
    author_runbook(instance, "test.shared", line_steps(SETTLE))
    template_path = runbook_path("test.shared")
    template = parse_runbook(
        instance.blob_at(instance.accepted_coordinate().git_oid, template_path),
        path=template_path,
    )
    measured: dict[int, dict[str, int]] = {}
    previous = 0
    for size in (20, 2000):
        updates = {}
        for index in range(previous, size):
            name = f"test.shared-{index:04d}"
            clone = template.model_copy(
                update={"identity": ArtifactIdentity(kind="Runbook", name=name)}
            )
            updates[runbook_path(name)] = render_runbook(clone)
        accept_tree(instance, updates, name=f"shared-runbooks-{size}")
        measured[size] = _measured_read(instance, root)
        previous = size
    assert measured[20]["presence_calls"] == measured[2000]["presence_calls"] == 1
    assert measured[2000]["sql_statements"] == measured[20]["sql_statements"]
    assert measured[2000]["sql_vm_steps"] == measured[20]["sql_vm_steps"]


def _named_lines(instance: Any) -> dict[str, int]:
    from cruxible_core.consumers.next import line_inputs

    line_inputs.catch_up(instance)
    with line_inputs._STATE.open(instance, create=False) as connection:
        assert connection is not None
        return {
            str(line): int(count)
            for line, count in connection.execute("SELECT line, runbooks FROM runbook_named_lines")
        }


def test_a_line_leaves_the_named_set_only_when_no_live_runbook_names_it(tmp_path: Path) -> None:
    from tests.test_runbooks.test_runbook_trigger_counters import _retire_runbook

    instance, _root = runbook_world(tmp_path)
    author_runbook(instance, "test.a", line_steps(SETTLE))
    author_runbook(instance, "test.b", line_steps(SETTLE, FAIL))
    assert _named_lines(instance) == {SETTLE_KEY: 2, FAIL_KEY: 1}
    # A revised Runbook releases only its own memberships.
    author_runbook(instance, "test.a", line_steps(FAIL))
    assert _named_lines(instance) == {SETTLE_KEY: 1, FAIL_KEY: 2}
    _retire_runbook(instance, "test.b")
    assert _named_lines(instance) == {FAIL_KEY: 1}
    _retire_runbook(instance, "test.a")
    assert _named_lines(instance) == {}


def test_an_earlier_index_schema_rebuilds_once(tmp_path: Path) -> None:
    """A state an earlier version wrote is replayed once from accepted history, then kept."""

    import sqlite3

    from cruxible_core.consumers import state as disposable
    from cruxible_core.consumers.next import line_inputs

    instance, _root = runbook_world(tmp_path)
    author_runbook(instance, "test.a", line_steps(SETTLE))
    line_inputs.catch_up(instance)
    # The earlier layout: Runbook Lines read through a membership index, no named set.
    with sqlite3.connect(line_inputs._STATE.path(instance)) as connection:
        connection.executescript(
            "DROP TABLE runbook_named_lines;"
            "CREATE INDEX runbook_lines_by_line ON runbook_lines(line);"
        )
    rebuilds: list[str] = []
    real = disposable.DisposableState._rebuild

    def counted(self: Any, connection: Any) -> None:
        rebuilds.append(self.directory)
        real(self, connection)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(disposable.DisposableState, "_rebuild", counted)
        assert _named_lines(instance) == {SETTLE_KEY: 1}
        assert _named_lines(instance) == {SETTLE_KEY: 1}
    assert rebuilds == ["next/line-inputs"]
