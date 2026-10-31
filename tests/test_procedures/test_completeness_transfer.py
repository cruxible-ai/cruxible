"""The completeness law's transfer through calls, extract rows, invoke and repeat.

Every value a sink receives is complete only if everything it was derived
from was. These run served Lines (the real admission, executor and journal)
with a probe sink attached to the executor:

- an extract's ``rows`` view drops invalid fields; a read through it that
  reaches one is incomplete, so ``extract -> rows -> filter -> count ->
  derive`` cannot launder an unreadable status into a complete negative;
- an acquiring call over an incomplete request is incomplete;
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


# --- invoke and repeat ------------------------------------------------------------


def _child(instance: Any) -> None:
    """A child whose return reads an extract that is complete only if its count holds."""

    author_procedure(
        instance,
        ProcedureSpec.model_validate(
            {
                "procedure": "test.child",
                "input": {},
                "output": {"status": {"type": "string", "optional": True}},
                "slots": {"read": _SLOT},
                "steps": [
                    {
                        "call": "doc",
                        "captures": {"capture": "test.orders-v1"},
                        "slot": "read",
                        "input": {**_READ, "relative_path": "data/doc.json"},
                    },
                    _entries("$doc.capture", complete_when={"count_at": "/count"}),
                    {
                        "project": "result",
                        "fields": {"status": "$steps.entries.rows.0.status"},
                        "output": {"status": {"type": "string", "optional": True}},
                    },
                ],
                "returns": "result",
                "budget": {"provider_calls": 1, "capture_bytes": 65536},
            }
        ),
        name="child",
    )


@pytest.mark.parametrize(("stated", "complete"), [(1, True), (5, False)])
def test_an_invoke_carries_its_childs_incomplete_return_past_complete_parent_inputs(
    tmp_path: Path, sinks: list[tuple[str, bool]], stated: int, complete: bool
) -> None:
    """F5: the child's completion records its return's completeness; the parent reads it."""

    document = {"count": stated, "entries": [{"id": "a", "status": "open"}]}
    instance, root = _workspace_world(tmp_path, {"doc.json": document})
    _child(instance)
    author_procedure(
        instance,
        ProcedureSpec.model_validate(
            {
                "procedure": "test.parent",
                "input": {},
                "output": {"child": "json"},
                "steps": [
                    {"invoke": "test.child", "id": "child", "input": {}},
                    # The invoke's value is {succeeded, status, receipt, value}; the
                    # authored typing reads the child's output, so the path is explicit.
                    {
                        "explicit": {
                            "kind": "project",
                            "node_id": "finished",
                            "fields": {"child": "$steps.child.value"},
                            "contract_out": {
                                "target": {"kind": "Contract", "name": "test.parent-output"}
                            },
                            "as": "finished",
                        }
                    },
                ],
                "returns": "finished",
                "budget": {"provider_calls": 1, "capture_bytes": 65536},
            }
        ),
        name="parent",
    )
    author_line(
        instance,
        name="test.parent-line",
        procedure="test.parent",
        parameters={},
        label="parent",
    )

    state = _run(instance, "test.parent-line", root=root)

    assert state.status == "succeeded", state.terminal
    assert state.result == {"child": {"status": "open"}}
    # The child's own sink, then the parent's: the parent's inputs are complete.
    assert sinks == [("result", complete), ("finished", complete)]


def _record(status: object, *, invalid: bool) -> dict[str, Any]:
    field = (
        {"tag": "invalid", "problem": {"code": "not_in_enum", "lexeme": str(status)}}
        if invalid
        else {"tag": "present", "value": status, "at": "/status"}
    )
    return {"record": "/0", "key": {}, "fields": {"status": field}}


@pytest.mark.parametrize("invalid", [False, True])
def test_a_repeat_is_the_and_over_the_attempts_it_used(
    tmp_path: Path, sinks: list[tuple[str, bool]], invalid: bool
) -> None:
    """A body transform that evaluates an unknown predicate leaves the repeat incomplete."""

    instance = world(tmp_path, operation_build())
    author_procedure(
        instance,
        ProcedureSpec.model_validate(
            {
                "procedure": "test.repeated",
                "input": {},
                "output": {"kept": "integer"},
                "steps": [
                    {
                        "repeat": "retry",
                        "max_attempts": 1,
                        "body": [
                            {
                                "node_id": "attempt",
                                "operation": "transform",
                                "transform_kind": "filter",
                                "spec": {
                                    "tag": "cruxible-transform-filter-spec-v1",
                                    "input": [_record("open", invalid=invalid)],
                                    "where": "present($item.fields.status)",
                                },
                                "as": "attempt",
                            }
                        ],
                        "until": "attempt.count >= 0",
                    },
                    {
                        "project": "finished",
                        "fields": {"kept": "$steps.retry.final.attempt.count"},
                    },
                ],
                "returns": "finished",
                "budget": {"provider_calls": 0},
            }
        ),
    )
    author_line(instance, name="test.repeat-line", procedure="test.repeated", parameters={})

    state = _run(instance, "test.repeat-line")

    assert state.status == "succeeded", state
    assert state.result == {"kept": 0 if invalid else 1}
    assert sinks == [("finished", not invalid)]


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


def _filter_count_derive(source: str, *, prefix: str) -> list[dict[str, Any]]:
    """filter present(status) over ``source`` -> count -> derive fixed/affected, as transforms."""

    return [
        {
            "node_id": f"{prefix}kept",
            "transform_kind": "filter",
            "spec": {
                "tag": "cruxible-transform-filter-spec-v1",
                "input": source,
                "where": "present($item.fields.status)",
            },
            "as": f"{prefix}kept",
        },
        {
            "node_id": f"{prefix}count",
            "transform_kind": "aggregate",
            "spec": {
                "tag": "cruxible-transform-aggregate-spec-v1",
                "input": f"$steps.{prefix}kept",
                "op": "count",
            },
            "as": f"{prefix}count",
        },
        {
            "node_id": f"{prefix}verdict",
            "transform_kind": "derive",
            "spec": {
                "tag": "cruxible-transform-derive-spec-v1",
                "input": {"open": f"$steps.{prefix}count"},
                "cases": [{"when": "eq($item.fields.open, 0)", "value": "fixed"}],
                "else": "affected",
            },
            "as": f"{prefix}verdict",
        },
    ]


_SHAPED = {
    "node_id": "shaped",
    "transform_kind": "shape",
    "as": "shaped",
}


def _shaped(invalid: bool) -> dict[str, Any]:
    return {
        **_SHAPED,
        "spec": {
            "tag": "cruxible-transform-shape-spec-v1",
            "input": [_record("open", invalid=invalid)],
            "fields": {"status": {"from": "$item.fields.status"}},
        },
    }


def _repeat_world(tmp_path: Path, body: list[dict[str, Any]], after: list[dict[str, Any]]) -> Any:
    instance = world(tmp_path, operation_build())
    explicit_after = []
    for index, step in enumerate(after):
        following = after[index + 1]["node_id"] if index + 1 < len(after) else "finished"
        explicit_after.append({"explicit": {"kind": "transform", **step, "next": following}})
    author_procedure(
        instance,
        ProcedureSpec.model_validate(
            {
                "procedure": "test.repeat-rows",
                "input": {},
                "output": {"verdict": "string"},
                "steps": [
                    {
                        "repeat": "retry",
                        "max_attempts": 1,
                        "body": [{**item, "operation": "transform"} for item in body],
                        "until": "shaped.count >= 0",
                        **({"next": after[0]["node_id"]} if after else {}),
                    },
                    *explicit_after,
                    {
                        "project": "finished",
                        "fields": {
                            "verdict": (
                                "$steps.verdict.result"
                                if after
                                else "$steps.retry.final.verdict.result"
                            )
                        },
                        "output": {"verdict": "string"},
                    },
                ],
                "returns": "finished",
                "budget": {"provider_calls": 0},
            }
        ),
    )
    author_line(instance, name="test.repeat-rows-line", procedure="test.repeat-rows", parameters={})
    return instance


@pytest.mark.parametrize("invalid", [False, True])
def test_a_lossy_rows_read_inside_a_repeat_body_taints_the_derived_negative(
    tmp_path: Path, sinks: list[tuple[str, bool]], invalid: bool
) -> None:
    """Re-review F3/F5: shape keeps the invalid status; a body filter over its rows view
    must not read it as absence and reach a complete "fixed"."""

    body = [_shaped(invalid), *_filter_count_derive("$steps.shaped.rows", prefix="")]
    instance = _repeat_world(tmp_path, body, [])

    state = _run(instance, "test.repeat-rows-line")

    assert state.status == "succeeded", state
    assert state.result == {"verdict": "fixed" if invalid else "affected"}
    assert sinks == [("finished", not invalid)]


@pytest.mark.parametrize(
    "view", ["$steps.retry.final.shaped.rows", "$steps.retry.attempts.0.outputs.shaped.rows"]
)
@pytest.mark.parametrize("invalid", [False, True])
def test_a_repeats_final_and_attempt_values_keep_their_bodies_dropped_fields(
    tmp_path: Path, sinks: list[tuple[str, bool]], invalid: bool, view: str
) -> None:
    instance = _repeat_world(tmp_path, [_shaped(invalid)], _filter_count_derive(view, prefix=""))

    state = _run(instance, "test.repeat-rows-line")

    assert state.status == "succeeded", state
    assert state.result == {"verdict": "fixed" if invalid else "affected"}
    assert sinks == [("finished", not invalid)]
