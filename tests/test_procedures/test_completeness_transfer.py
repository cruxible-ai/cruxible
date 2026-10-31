"""The completeness law's transfer through calls, extract rows, invoke and repeat.

Every value a sink receives is complete only if everything it was derived
from was. These run served Lines (the real admission, executor and journal)
with a probe sink attached to the executor:

- an extract's ``rows`` view drops invalid fields; a read through it that
  reaches one is incomplete, so ``extract -> rows -> filter -> count ->
  derive`` cannot launder an unreadable status into a complete negative;
- an acquiring call over an incomplete request is incomplete, and so is any
  call bound to an operation that declares ``may_truncate``;
- an invoke carries its child's return completeness, recorded in the child's
  completion; a repeat is the AND over the attempts it used.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.procedures.authored import ProcedureSpec
from cruxible_client.contracts.shapes import Contract, contract_path
from cruxible_client.contracts.shapes import render_contract as render
from cruxible_core.procedures.execution import ProcedureExecutor
from cruxible_core.service.procedures.procedure_runs import (
    LineRunRequest,
    service_run_playbill_line,
)
from tests.support.operation_world import (
    NOW,
    Invoker,
    Operator,
    actor,
    author_line,
    author_procedure,
    operation,
    operation_build,
    world,
)

ENTRY = Contract.model_validate(
    {
        "name": "test.entry",
        "version": 1,
        "fields": {"status": {"type": "enum[open, closed]"}, "note": {"type": "int?"}},
    }
)


@dataclass(frozen=True)
class _Clock:
    at: datetime

    def now(self) -> datetime:
        return self.at

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()


@pytest.fixture
def sinks(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bool]]:
    """Attach a probe sink to every executor the served lane builds."""

    seen: list[tuple[str, bool]] = []
    original = ProcedureExecutor.__init__

    def attached(self: ProcedureExecutor, *args: Any, **kwargs: Any) -> None:
        kwargs["completeness_probe"] = lambda node_id, complete: seen.append((node_id, complete))
        original(self, *args, **kwargs)

    monkeypatch.setattr(ProcedureExecutor, "__init__", attached)
    return seen


def _run(instance: Any, line: str, *, invoker: Invoker | None = None, root: Path | None = None):  # type: ignore[no-untyped-def]
    from cruxible_core.documents.workspace_file import WorkspaceFileReader
    from cruxible_core.runtime.provider_runtime import ProviderRuntimeOperator

    operator: Any = (
        Operator(invoker or Invoker())
        if root is None
        else ProviderRuntimeOperator(root.parent / "state")
    )
    return service_run_playbill_line(
        instance,
        path_identity_digest=line,
        request=LineRunRequest(line=line),
        actor_context=actor(),
        caller_rung=2,
        daemon_clock=_Clock(NOW),
        provider_runtime_operator=operator,
        **(
            {}
            if root is None
            else {
                "workspace_file_reader": WorkspaceFileReader(
                    instance_id=instance.descriptor.instance_id,
                    operating_profile="local",
                    attached_roots=(root,),
                    managed_roots=(instance.root,),
                )
            }
        ),
    )


# --- extract rows -------------------------------------------------------------

_READ = {
    "logical_source": "commerce.production.orders",
    "coordinate_type": "database-snapshot-v1",
    "coordinate": {"lsn": "workspace"},
    "selector_type": "relation-primary-key-v1",
    "selector": {"id": 7, "relation": "orders"},
}
_SLOT = {"in": "workspace.file.request@1", "out": "workspace.file.result@1", "effect": "pure"}


def _transform(node_id: str, kind: str, spec: dict[str, Any], next_node: str) -> dict[str, Any]:
    return {
        "explicit": {
            "kind": "transform",
            "node_id": node_id,
            "transform_kind": kind,
            "spec": {"tag": f"cruxible-transform-{kind}-spec-v1", **spec},
            "as": node_id,
            "next": next_node,
        }
    }


def _workspace_world(tmp_path: Path, documents: dict[str, object]) -> tuple[Any, Path]:
    from cruxible_client.contracts.captures import capture_contract_path, render_capture_contract
    from tests.core_support._identity_support import accept_tree
    from tests.core_support._light_capture_support import record_contract

    instance = world(tmp_path, operation_build())
    root = tmp_path / "workspace"
    (root / "data").mkdir(parents=True, exist_ok=True)
    for name, document in documents.items():
        (root / "data" / name).write_text(json.dumps(document))
    capture = record_contract()
    accept_tree(
        instance,
        {
            capture_contract_path(capture.identity.name): render_capture_contract(capture),
            contract_path(ENTRY.name, ENTRY.version): render(ENTRY),
        },
        name="capture-and-row",
    )
    return instance, root


def _entries(capture: str, *, node_id: str = "entries", **extra: Any) -> dict[str, Any]:
    return {
        "extract": node_id,
        "capture": capture,
        "records": "/entries/*",
        "row": f"{ENTRY.name}@{ENTRY.version}",
        "key": {"id": {"at": "id"}},
        "fields": {"status": {"at": "status"}, "note": {"at": "note"}},
        **extra,
    }


def _count_fixed(instance: Any, *, kept_from: str) -> None:
    """call -> extract -> filter present(status) over ``kept_from`` -> count -> derive -> leaf."""

    spec = ProcedureSpec.model_validate(
        {
            "procedure": "test.rows",
            "input": {},
            "output": {"verdict": "string"},
            "slots": {"read": _SLOT},
            "steps": [
                {
                    "call": "doc",
                    "captures": {"capture": "test.orders-v1"},
                    "slot": "read",
                    "input": {**_READ, "relative_path": "data/doc.json"},
                },
                _entries("$doc.capture", next="kept"),
                _transform(
                    "kept",
                    "filter",
                    {"input": kept_from, "where": "present($item.fields.status)"},
                    "count",
                ),
                _transform(
                    "count", "aggregate", {"input": "$steps.kept", "op": "count"}, "verdict"
                ),
                _transform(
                    "verdict",
                    "derive",
                    {
                        "input": {"open": "$steps.count"},
                        "cases": [{"when": "eq($item.fields.open, 0)", "value": "fixed"}],
                        "else": "affected",
                    },
                    "out",
                ),
                {"project": "out", "fields": {"verdict": "$steps.verdict.result"}},
            ],
            "returns": "out",
            "budget": {"provider_calls": 1, "capture_bytes": 65536},
        }
    )
    author_procedure(instance, spec)
    author_line(instance, name="test.rows-line", procedure="test.rows", parameters={})


@pytest.mark.parametrize("kept_from", ["$steps.entries.rows", "$steps.entries"])
def test_an_unreadable_status_never_reaches_the_sink_as_a_complete_negative(
    tmp_path: Path, sinks: list[tuple[str, bool]], kept_from: str
) -> None:
    """F3: through ``rows`` (fields dropped) or ``records`` (fields kept), both taint."""

    document = {"entries": [{"id": "a", "status": "Maybe"}]}
    instance, root = _workspace_world(tmp_path, {"doc.json": document})
    _count_fixed(instance, kept_from=kept_from)

    state = _run(instance, "test.rows-line", root=root)

    assert state.status == "succeeded", state.terminal
    # Nothing kept, count 0, derive reached "fixed"...
    assert state.result == {"verdict": "fixed"}
    # ...but the sink sees an incomplete value: the status was unreadable.
    assert sinks == [("out", False)]


def test_readable_rows_reach_the_sink_complete(
    tmp_path: Path, sinks: list[tuple[str, bool]]
) -> None:
    document = {"entries": [{"id": "a", "status": "closed"}, {"id": "b", "status": "open"}]}
    instance, root = _workspace_world(tmp_path, {"doc.json": document})
    _count_fixed(instance, kept_from="$steps.entries.rows")

    state = _run(instance, "test.rows-line", root=root)

    assert state.status == "succeeded", state.terminal
    assert state.result == {"verdict": "affected"}
    assert sinks == [("out", True)]


def test_an_invalid_field_elsewhere_taints_rows_but_not_the_records_it_never_reads(
    tmp_path: Path, sinks: list[tuple[str, bool]]
) -> None:
    """Records keep each field's result, so an invalid ``note`` the filter never reads
    leaves the records path complete; the rows view cannot tell, so it taints."""

    document = {"entries": [{"id": "a", "status": "open", "note": "seven"}]}
    for world_dir, kept_from, complete in (
        ("records", "$steps.entries", True),
        ("rows", "$steps.entries.rows", False),
    ):
        sinks.clear()
        (tmp_path / world_dir).mkdir()
        instance, root = _workspace_world(tmp_path / world_dir, {"doc.json": document})
        _count_fixed(instance, kept_from=kept_from)
        state = _run(instance, "test.rows-line", root=root)
        assert state.status == "succeeded", state.terminal
        assert state.result == {"verdict": "affected"}
        assert sinks == [("out", complete)], kept_from


def _row_read(tmp_path: Path, field: str) -> Any:
    document = {"entries": [{"id": "a", "status": "Maybe"}, {"id": "b", "status": "open"}]}
    instance, root = _workspace_world(tmp_path, {"doc.json": document})
    spec = ProcedureSpec.model_validate(
        {
            "procedure": "test.row-read",
            "input": {},
            "output": {"row": "json"},
            "slots": {"read": _SLOT},
            "steps": [
                {
                    "call": "doc",
                    "captures": {"capture": "test.orders-v1"},
                    "slot": "read",
                    "input": {**_READ, "relative_path": "data/doc.json"},
                },
                _entries("$doc.capture"),
                {
                    "project": "out",
                    "fields": {"row": f"$steps.entries.rows.{field}"},
                    "output": {"row": "json"},
                },
            ],
            "returns": "out",
            "budget": {"provider_calls": 1, "capture_bytes": 65536},
        }
    )
    author_procedure(instance, spec)
    author_line(instance, name="test.row-line", procedure="test.row-read", parameters={})
    return _run(instance, "test.row-line", root=root)


@pytest.mark.parametrize(("row", "complete"), [("0", False), ("1", True)])
def test_a_row_read_is_incomplete_exactly_when_it_reaches_a_dropped_field(
    tmp_path: Path, sinks: list[tuple[str, bool]], row: str, complete: bool
) -> None:
    state = _row_read(tmp_path, row)

    assert state.status == "succeeded", state.terminal
    assert sinks == [("out", complete)]


def test_a_dropped_field_read_by_name_refuses_rather_than_reading_absence(
    tmp_path: Path, sinks: list[tuple[str, bool]]
) -> None:
    state = _row_read(tmp_path, "0.status")

    assert state.status == "node_refused"
    assert state.terminal.code == "runtime_reference_unresolved"
    assert sinks == []


# --- acquiring calls ----------------------------------------------------------


@pytest.mark.parametrize(("stated", "complete"), [(1, True), (5, False)])
def test_an_acquiring_call_over_an_incomplete_request_is_incomplete(
    tmp_path: Path, sinks: list[tuple[str, bool]], stated: int, complete: bool
) -> None:
    """F4: the second read's path comes from an extract whose stated count may not hold."""

    index = {"count": stated, "next": "data/doc.json", "entries": [{"id": "a", "status": "open"}]}
    document = {"entries": [{"id": "b", "status": "open"}]}
    instance, root = _workspace_world(tmp_path, {"index.json": index, "doc.json": document})
    spec = ProcedureSpec.model_validate(
        {
            "procedure": "test.chained",
            "input": {},
            "output": {"status": {"type": "string", "optional": True}},
            "slots": {"first": _SLOT, "second": _SLOT},
            "steps": [
                {
                    "call": "index",
                    "captures": {"capture": "test.orders-v1"},
                    "slot": "first",
                    "input": {**_READ, "relative_path": "data/index.json"},
                },
                _entries(
                    "$index.capture",
                    node_id="listing",
                    complete_when={"count_at": "/count"},
                    header={"next": {"at": "/next", "presence": "required"}},
                ),
                {
                    "call": "doc",
                    "captures": {"capture": "test.orders-v1"},
                    "slot": "second",
                    "input": {**_READ, "relative_path": "$steps.listing.header.next.value"},
                },
                _entries("$doc.capture"),
                {
                    "project": "out",
                    "fields": {"status": "$steps.entries.rows.0.status"},
                    "output": {"status": {"type": "string", "optional": True}},
                },
            ],
            "returns": "out",
            "budget": {"provider_calls": 2, "capture_bytes": 65536},
        }
    )
    author_procedure(instance, spec)
    author_line(instance, name="test.chained-line", procedure="test.chained", parameters={})

    state = _run(instance, "test.chained-line", root=root)

    assert state.status == "succeeded", state.terminal
    assert state.result == {"status": "open"}
    assert sinks == [("out", complete)]


# --- truncating operations, invoke and repeat ---------------------------------


_COMPUTE = {"in": "test.value@1", "out": "test.value@1", "effect": "pure"}


def _value_world(tmp_path: Path, *, may_truncate: bool) -> Any:
    served = operation().model_copy(update={"may_truncate": may_truncate})
    return world(tmp_path, operation_build(operations=(served,)))


def _child(instance: Any) -> None:
    author_procedure(
        instance,
        ProcedureSpec.model_validate(
            {
                "procedure": "test.child",
                "input": {"value": "integer"},
                "output": {"value": "integer"},
                "slots": {
                    "compute": {"in": "test.value@1", "out": "test.value@1", "effect": "pure"}
                },
                "steps": [
                    {"call": "result", "slot": "compute", "input": {"value": "$input.value"}}
                ],
                "returns": "result",
                "budget": {"provider_calls": 1},
            }
        ),
        name="child",
    )


@pytest.mark.parametrize("may_truncate", [False, True])
def test_a_call_bound_to_a_truncating_operation_is_incomplete(
    tmp_path: Path, sinks: list[tuple[str, bool]], may_truncate: bool
) -> None:
    instance = _value_world(tmp_path, may_truncate=may_truncate)
    _child(instance)
    author_line(instance, name="test.child-line", procedure="test.child")

    state = _run(instance, "test.child-line")

    assert state.status == "succeeded", state
    assert sinks == [("result", not may_truncate)]


@pytest.mark.parametrize("may_truncate", [False, True])
def test_an_invoke_carries_its_childs_incomplete_return_past_complete_parent_inputs(
    tmp_path: Path, sinks: list[tuple[str, bool]], may_truncate: bool
) -> None:
    """F5: the child's completion records its return's completeness; the parent reads it."""

    instance = _value_world(tmp_path, may_truncate=may_truncate)
    _child(instance)
    author_procedure(
        instance,
        ProcedureSpec.model_validate(
            {
                "procedure": "test.parent",
                "input": {"value": "integer"},
                "output": {"child": "json"},
                "steps": [
                    {"invoke": "test.child", "id": "child", "input": {"value": "$input.value"}},
                    {
                        "project": "finished",
                        "fields": {"child": "$steps.child.value"},
                        "output": {"child": "json"},
                    },
                ],
                "returns": "finished",
                "budget": {"provider_calls": 1},
            }
        ),
        name="parent",
    )
    author_line(
        instance, name="test.parent-line", procedure="test.parent", bindings={}, label="parent"
    )

    state = _run(instance, "test.parent-line")

    assert state.status == "succeeded", state
    assert state.result == {"child": {"value": 2}}
    # The child's own sink, then the parent's: the parent inputs are complete.
    assert sinks == [("result", not may_truncate), ("finished", not may_truncate)]


@pytest.mark.parametrize("may_truncate", [False, True])
def test_a_repeat_is_the_and_over_the_attempts_it_used(
    tmp_path: Path, sinks: list[tuple[str, bool]], may_truncate: bool
) -> None:
    instance = _value_world(tmp_path, may_truncate=may_truncate)
    author_procedure(
        instance,
        ProcedureSpec.model_validate(
            {
                "procedure": "test.repeated",
                "input": {"value": "integer"},
                "output": {"value": "integer"},
                "slots": {
                    "compute": {"in": "test.value@1", "out": "test.value@1", "effect": "pure"}
                },
                "steps": [
                    {
                        "repeat": "retry",
                        "max_attempts": 1,
                        "body": [
                            {
                                "node_id": "attempt",
                                "operation": "call",
                                "slot": "compute",
                                "spec": {"value": "$input.value"},
                                "as": "attempt",
                            }
                        ],
                        "until": "attempt.value == 2",
                    },
                    {
                        "project": "finished",
                        "fields": {"value": "$steps.retry.final.attempt.value"},
                    },
                ],
                "returns": "finished",
                "budget": {"provider_calls": 1},
                "caps": {"provider_calls": 1, "repeat_attempts": 1},
            }
        ),
    )
    author_line(instance, name="test.repeat-line", procedure="test.repeated")

    state = _run(instance, "test.repeat-line")

    assert state.status == "succeeded", state
    assert sinks == [("finished", not may_truncate)]


@pytest.mark.parametrize(("view", "complete"), [("rows", False), ("records", True)])
def test_a_closed_transforms_rows_view_also_taints_a_read_reaching_a_dropped_field(
    tmp_path: Path, sinks: list[tuple[str, bool]], view: str, complete: bool
) -> None:
    """Records flowing through a filter keep the invalid note; its rows view drops it."""

    document = {"entries": [{"id": "a", "status": "open", "note": "seven"}]}
    instance, root = _workspace_world(tmp_path, {"doc.json": document})
    spec = ProcedureSpec.model_validate(
        {
            "procedure": "test.kept-rows",
            "input": {},
            "output": {"kept": "json"},
            "slots": {"read": _SLOT},
            "steps": [
                {
                    "call": "doc",
                    "captures": {"capture": "test.orders-v1"},
                    "slot": "read",
                    "input": {**_READ, "relative_path": "data/doc.json"},
                },
                _entries("$doc.capture", next="kept"),
                _transform(
                    "kept",
                    "filter",
                    {"input": "$steps.entries", "where": "present($item.fields.status)"},
                    "out",
                ),
                {
                    "project": "out",
                    "fields": {"kept": f"$steps.kept.{view}"},
                    "output": {"kept": "json"},
                },
            ],
            "returns": "out",
            "budget": {"provider_calls": 1, "capture_bytes": 65536},
        }
    )
    author_procedure(instance, spec)
    author_line(instance, name="test.kept-line", procedure="test.kept-rows", parameters={})

    state = _run(instance, "test.kept-line", root=root)

    assert state.status == "succeeded", state.terminal
    assert sinks == [("out", complete)]


@pytest.mark.parametrize(("status", "complete"), [("Maybe", False), ("open", True)])
def test_an_inbox_item_reading_an_invalid_field_reaches_its_sink_incomplete(
    tmp_path: Path, sinks: list[tuple[str, bool]], status: str, complete: bool
) -> None:
    """F13 with F3: a typed inbox item never carries an unreadable value as absence."""

    status_item = Contract.model_validate(
        {
            "name": "test.status-item",
            "version": 1,
            "fields": {"id": {"type": "string"}, "status": {"type": "string?"}},
        }
    )
    document = {"entries": [{"id": "a", "status": status}]}
    instance, root = _workspace_world(tmp_path, {"doc.json": document})
    from tests.core_support._identity_support import accept_tree
    from tests.support.inbox import contract_members

    accept_tree(instance, contract_members(status_item), name="status-item")
    spec = ProcedureSpec.model_validate(
        {
            "procedure": "test.inbox-statuses",
            "input": {},
            "output": {"posted": "integer"},
            "slots": {"read": _SLOT},
            "steps": [
                {
                    "call": "doc",
                    "captures": {"capture": "test.orders-v1"},
                    "slot": "read",
                    "input": {**_READ, "relative_path": "data/doc.json"},
                },
                _entries("$doc.capture"),
                {"project": "out", "fields": {"posted": 1}},
                {
                    "inbox": "test.status-item@1",
                    "for_each": "$steps.entries.records",
                    "fields": {"id": "$item.key.id", "status": "$item.fields.status"},
                },
            ],
            "returns": "out",
            "budget": {"provider_calls": 1, "capture_bytes": 65536},
        }
    )
    author_procedure(instance, spec)
    author_line(
        instance,
        name="test.inbox-line",
        procedure="test.inbox-statuses",
        parameters={},
        max_authority="observe",
    )

    state = _run(instance, "test.inbox-line", root=root)

    assert state.status == "succeeded", state.terminal
    inbox_sinks = [complete_ for node_id, complete_ in sinks if node_id != "out"]
    assert inbox_sinks == [complete]
