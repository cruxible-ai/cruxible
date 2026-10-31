"""Closed transforms, three-valued predicates and the completeness law, through the executor.

A record whose ``status`` is invalid is dropped by ``filter present(status)``,
but its absence taints everything derived from what was kept: ``count`` is 0
and ``derive fixed`` is reached, yet the probe sink sees an incomplete value
(R03). A guard over an incomplete value carries that into its branch. The
probe sink is this slice's harness; the real sinks belong to the Claims
terminal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.procedures.contract_schema import PropertySchema
from cruxible_client.contracts.procedures.contracts import OwnedProcedureContractValidator
from cruxible_client.contracts.procedures.models import (
    GuardNode,
    GuardPredicate,
    HaltNode,
    PredicateOperand,
    ProcedureDefinition,
    ProjectNode,
    TransformNode,
)
from cruxible_client.contracts.procedures.nodes import TransformDeriveSpec, TransformFilterSpec
from cruxible_core.procedures.execution import ProcedureExecutor
from tests.support.procedures import named
from tests.test_procedures.test_procedure_execution import (
    _Authority,
    _budget,
    _fixture,
    _hard_caps,
    _owned_accepted,
    _owned_contract,
    _owned_pin,
    _prepare,
    _StateReader,
)


def _status(value: object, *, invalid: bool = False) -> dict[str, Any]:
    if invalid:
        field = {"tag": "invalid", "problem": {"code": "not_in_enum", "lexeme": str(value)}}
    else:
        field = {"tag": "present", "value": value, "at": "/status"}
    return {"record": f"/{value}", "key": {}, "fields": {"status": field}}


def _rows(*records: dict[str, Any]) -> dict[str, Any]:
    return {"records": list(records), "complete": True}


def _transform(node_id: str, kind: str, spec: dict[str, Any], **extra: Any) -> TransformNode:
    return TransformNode.model_validate(
        {
            "kind": "transform",
            "node_id": node_id,
            "transform_kind": kind,
            "spec": {"tag": f"cruxible-transform-{kind}-spec-v1", **spec},
            "as": node_id,
            **extra,
        }
    )


def _run(nodes: tuple[Any, ...], tmp_path: Path, *, returns: str = "result") -> tuple[Any, list]:
    empty = _owned_contract("empty", {})
    opaque = _owned_contract("opaque", {"rows": PropertySchema(type="json")})
    out = _owned_contract("result", {"value": PropertySchema(type="json")})
    entry, opaque_pin, out_pin = (
        _owned_pin("contract-in", empty),
        _owned_pin("contract-out", opaque),
        _owned_pin("contract-out", out),
    )
    rebound = []
    for node in nodes:
        if isinstance(node, ProjectNode):
            target = opaque_pin if node.node_id == "feed" else out_pin
            node = node.model_copy(update={"contract_out": named(target)})
        rebound.append(node)
    definition = ProcedureDefinition(
        name="completeness",
        contract_in=named(entry),
        contract_out=named(out_pin),
        nodes=tuple(rebound),
        returns=returns,
        budget=_budget(),
        hard_caps=_hard_caps(),
        terminal_capability=1,
    )
    accepted = _owned_accepted(
        definition, contracts=(empty, opaque, out), pins=(entry, opaque_pin, out_pin)
    )
    fixture = _fixture(tmp_path)
    sinks: list[tuple[str, bool]] = []
    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=OwnedProcedureContractValidator(accepted),
        completeness_probe=lambda node_id, complete: sinks.append((node_id, complete)),
    ).execute(_prepare(accepted, fixture, _StateReader(), invocation_input={}), accepted)
    return result, sinks


_PLACEHOLDER = named(_owned_pin("contract-out", _owned_contract("result", {})))


def _feed(*records: dict[str, Any]) -> ProjectNode:
    return ProjectNode(
        node_id="feed", literal={"rows": _rows(*records)}, contract_out=_PLACEHOLDER, as_="feed"
    )


def _returned(alias: str) -> ProjectNode:
    return ProjectNode(
        node_id="result",
        fields={"value": f"$steps.{alias}"},
        contract_out=_PLACEHOLDER,
        as_="result",
    )


def _count_fixed(records: tuple[dict[str, Any], ...]) -> tuple[Any, ...]:
    return (
        _feed(*records),
        _transform(
            "kept",
            "filter",
            {"input": "$steps.feed.rows", "where": "present($item.fields.status)"},
        ),
        _transform("count", "aggregate", {"input": "$steps.kept", "op": "count"}),
        _transform(
            "verdict",
            "derive",
            {
                "input": {"open": "$steps.count"},
                "cases": [{"when": "eq($item.fields.open, 0)", "value": "fixed"}],
                "else": "affected",
            },
        ),
        ProjectNode(
            node_id="result",
            fields={"value": "$steps.verdict.result"},
            contract_out=_PLACEHOLDER,
            as_="result",
        ),
    )


def test_an_invalid_status_dropped_by_filter_taints_the_count_and_the_derived_negative(
    tmp_path: Path,
) -> None:
    result, sinks = _run(_count_fixed((_status("open", invalid=True),)), tmp_path)

    assert result.status == "succeeded", result
    # The positive reading: nothing kept, count 0, derive reached "fixed"...
    assert result.output == {"value": "fixed"}
    # ...but the sink sees an incomplete value: the row was unreadable, not absent.
    assert sinks == [("result", False)]


def test_readable_rows_reach_the_sink_complete(tmp_path: Path) -> None:
    result, sinks = _run(_count_fixed((_status("open"), _status("closed"))), tmp_path)

    assert result.output == {"value": "affected"}
    assert sinks == [("result", True)]


def test_a_filter_keeps_only_true_rows_and_unknown_marks_it_incomplete(tmp_path: Path) -> None:
    nodes = (
        _feed(_status("open"), _status("closed"), _status("x", invalid=True)),
        _transform(
            "kept",
            "filter",
            {"input": "$steps.feed.rows", "where": 'eq($item.fields.status, "open")'},
        ),
        _returned("kept"),
    )
    result, sinks = _run(nodes, tmp_path)

    assert result.status == "succeeded", result
    kept = result.output["value"]
    assert [row["status"] for row in kept["rows"]] == ["open"]
    assert kept["complete"] is False
    assert sinks == [("result", False)]


def test_a_guard_over_an_incomplete_value_carries_it_into_its_branch(tmp_path: Path) -> None:
    nodes = (
        *_count_fixed((_status("open", invalid=True),))[:3],
        GuardNode(
            node_id="gate",
            predicate=GuardPredicate(
                left=PredicateOperand(kind="step", alias="count", path=("result",)),
                operator="eq",
                right=PredicateOperand(kind="literal", value=0),
            ),
            on_true="result",
            on_false="stop",
            refusal_code="gate",
            message="Open items remain.",
        ),
        # The branch's value reads nothing incomplete; its control lineage does.
        ProjectNode(
            node_id="result",
            literal={"value": "closed"},
            contract_out=_PLACEHOLDER,
            as_="result",
            leaf=True,
        ),
        HaltNode(node_id="stop", reason="open"),
    )
    result, sinks = _run(nodes, tmp_path)

    assert result.status == "succeeded", result
    assert result.output == {"value": "closed"}
    assert sinks == [("result", False)]


def test_join_dedupe_union_and_shape_carry_records_and_completeness(tmp_path: Path) -> None:
    nodes = (
        _feed(_status("open"), _status("closed"), _status("open")),
        _transform("unique", "dedupe", {"input": "$steps.feed.rows", "keys": ["status"]}),
        _transform("both", "union", {"inputs": ["$steps.unique", "$steps.unique"]}),
        _transform(
            "labelled",
            "shape",
            {
                "input": "$steps.both",
                "fields": {"label": {"from": "$item.fields.status"}, "kind": {"value": "cve"}},
            },
        ),
        _transform(
            "joined",
            "join",
            {
                "left": "$steps.unique",
                "right": "$steps.labelled",
                "left_field": "status",
                "right_field": "label",
                "right_fields": {"kind": "kind"},
            },
        ),
        _returned("joined"),
    )
    result, sinks = _run(nodes, tmp_path)

    assert result.status == "succeeded", result
    joined = result.output["value"]
    assert [(row["status"], row["kind"]) for row in joined["rows"]] == [
        ("open", "cve"),
        ("open", "cve"),
        ("closed", "cve"),
        ("closed", "cve"),
    ]
    assert joined["complete"] is True
    assert sinks == [("result", True)]


def test_a_derive_without_else_is_refused() -> None:
    with pytest.raises(ValidationError, match="derive_else_required"):
        TransformDeriveSpec.model_validate(
            {
                "input": {"open": "$steps.count"},
                "cases": [{"when": "eq($item.fields.open, 0)", "value": "fixed"}],
            }
        )


@pytest.mark.parametrize(
    "where",
    [
        "matches($item.fields.status, 'o.*')",
        'eq(concat($item.fields.status, "x"), "openx")',
        {"op": "regex", "field": "status"},
    ],
    ids=["unknown-call", "string-building", "unknown-op"],
)
def test_a_predicate_outside_the_closed_grammar_is_refused(where: object) -> None:
    with pytest.raises(ValidationError):
        TransformFilterSpec.model_validate({"input": "$steps.feed.rows", "where": where})


def test_a_predicate_outside_the_closed_grammar_is_refused_by_the_graph(tmp_path: Path) -> None:
    payload = _transform(
        "kept", "filter", {"input": "$steps.feed.rows", "where": "present($item.fields.status)"}
    ).model_dump(mode="json", by_alias=True)
    payload["spec"]["where"] = "loop($item.fields.status)"
    with pytest.raises(ValidationError, match="predicate_outside_closed_grammar"):
        TransformNode.model_validate(payload)
