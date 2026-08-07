"""T6 and T8 -- what a 0.3 core does when it meets 0.4 artifacts.

A gate can only bind a reader that executes it, and 0.3.2 is already shipped.
So the story is not "hold old readers back" -- it is fail loud on every artifact
class a 0.3 reader can actually reach, and tell the user to upgrade.

The 0.3 definition reader is modelled here by rebuilding its model from THIS
tree's field set minus `graph_format`, which is exactly what 0.3.2 declares.
That keeps the test honest about the mechanism it is asserting (strictness plus
an unknown key) rather than pretending to run an old release.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError, create_model

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ConfigError
from cruxible_core.procedure.types import ProcedureDefinition, ProcedureRecord
from cruxible_core.receipt.types import OperationType, Receipt
from cruxible_core.runtime.instance import _load_snapshot_procedures
from cruxible_core.service import service_run_procedure
from tests.test_procedures.conftest import actor, provider_definition
from tests.test_procedures.test_execution import _accept, _receipt, _stub_provider

_BUDGET = {"wall_clock_s": 30, "max_provider_calls": 2}


def _v2_dump() -> dict[str, Any]:
    definition = ProcedureDefinition.model_validate(
        {
            "name": "v2_probe",
            "steps": [
                {"id": "read", "provider": "scorer", "input": {}, "as": "rows"},
                {
                    "id": "gate",
                    "guard": {"left": "count(rows, items)", "op": "gt", "right": 0},
                    "message": "no rows",
                },
            ],
            "returns": "rows",
            "precondition": {},
            "budget": _BUDGET,
            "graph_format": 2,
        }
    )
    return definition.model_dump(mode="json", by_alias=True, exclude_none=True)


def _pre_graph_definition_model() -> type[BaseModel]:
    """A 0.3.2-shaped definition reader: strict, and without `graph_format`."""
    fields: dict[str, Any] = {
        name: (Any, None) for name in ProcedureDefinition.model_fields if name != "graph_format"
    }
    return create_model(  # type: ignore[call-overload,no-any-return]
        "PreGraphProcedureDefinition",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )


def test_the_definition_model_is_strict_which_is_what_makes_the_lock_work() -> None:
    assert ProcedureDefinition.model_config["extra"] == "forbid"
    assert "graph_format" in ProcedureDefinition.model_fields


def test_t6_a_v2_definition_is_refused_by_a_pre_graph_reader() -> None:
    """The store row reader, the snapshot loader and the state-diff reader all
    go through this same model, so one refusal covers all three parse paths."""
    reader = _pre_graph_definition_model()
    with pytest.raises(ValidationError) as exc_info:
        reader.model_validate(_v2_dump())
    assert any(error["type"] == "extra_forbidden" for error in exc_info.value.errors())
    assert any("graph_format" in str(error["loc"]) for error in exc_info.value.errors())


def test_t6_a_v1_definition_is_still_accepted_by_a_pre_graph_reader() -> None:
    """The refusal is targeted, not a blanket break.

    A 0.4 instance holding only v1 procedures reads correctly on 0.3, which is
    the honest residue -- and it is what makes the convergence sweep the moment
    the break becomes visible.
    """
    reader = _pre_graph_definition_model()
    v1 = provider_definition("v1_probe").model_dump(mode="json", by_alias=True, exclude_none=True)
    assert reader.model_validate(v1) is not None


def test_t6_a_v2_definition_is_refused_through_the_snapshot_loader(tmp_path: Path) -> None:
    """The snapshot path refuses twice over.

    On 0.3 the exact-version gate fires first and `graph_format` is the backstop.
    Here the format is supported, so the strict definition model is what has to
    hold -- and it does, through the same ProcedureRecord validation.
    """
    artifact = json.dumps(
        {
            "format_version": 2,
            "procedures": [
                {
                    "procedure_id": "PRC-broken",
                    "definition": {**_v2_dump(), "not_a_field": 1},
                    "definition_digest": "sha256:x",
                    "proposed_actor_context": None,
                }
            ],
        }
    ).encode("utf-8")
    with pytest.raises(ConfigError, match="invalid procedure record"):
        _load_snapshot_procedures(artifact, snapshot_id="SNP-strict")


def test_t6_the_state_diff_reader_fails_closed_through_the_same_strict_model() -> None:
    """`state_diff.load_procedures` never checks a format version.

    That gap becomes a lock rather than a hole: the model it validates through
    is strict, so an unreadable definition is refused with no new version check
    anywhere.
    """
    assert ProcedureRecord.model_config["extra"] == "forbid"
    with pytest.raises(ValidationError):
        ProcedureRecord.model_validate(
            {
                "procedure_id": "PRC-1",
                "definition": {**_v2_dump(), "unknown_key": True},
                "definition_digest": "sha256:x",
                "proposed_actor_context": None,
            }
        )


def test_t8_a_run_receipt_carrying_pin_material_still_validates(
    procedure_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deliberately compatible.

    Pin material lives in the ROOT NODE's `detail`, which is
    `dict[str, Any]` by contract, so a 0.3 reader parses the receipt fine. That
    is why the material went there and not into a new top-level field -- which
    pydantic's default `extra="ignore"` would silently drop, leaving a receipt
    that looks complete and is not.
    """
    _stub_provider(monkeypatch, lambda payload: {"value": int(payload.get("value", 0))})
    procedure_id = _accept(procedure_instance, provider_definition("t8_probe"))
    result = service_run_procedure(procedure_instance, procedure_id, {"value": 5}, actor("runner"))
    receipt = _receipt(procedure_instance, result.run.receipt_id or "")
    assert "node_pins" in receipt.nodes[0].detail

    reparsed = Receipt.model_validate_json(receipt.model_dump_json())
    assert reparsed.nodes[0].detail["node_pins"] == receipt.nodes[0].detail["node_pins"]


def test_t8_a_calibration_finding_operation_type_is_refused_loudly() -> None:
    """A new OperationType literal is a hard compatibility boundary.

    The codebase documents this from the other direction: `group_clear` is
    retained specifically because REMOVING a literal made receipt reads raise on
    receipts 0.2.x instances had persisted. So accepting the loud refusal is a
    decision, not an oversight -- consistent with fail loud, upgrade.
    """
    payload = {
        "receipt_id": "RCP-1",
        "nodes": [{"node_id": "N1", "node_type": "query"}],
        "edges": [],
        "operation_type": "calibration_finding",
    }
    with pytest.raises(ValidationError):
        Receipt.model_validate(payload)
    assert "calibration_finding" not in str(OperationType)
