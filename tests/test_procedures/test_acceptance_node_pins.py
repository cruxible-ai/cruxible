"""Per-node acceptance pins: writing, the two-check verification, the receipt."""

from __future__ import annotations

from typing import Any

import pytest

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ConfigError
from cruxible_core.procedure.pins import (
    AcceptanceNodePin,
    compute_pin_digest,
    verify_pin_currency,
    verify_pin_integrity,
)
from cruxible_core.procedure.types import ProcedureDefinition
from cruxible_core.service import service_run_procedure
from cruxible_core.workflow.compiler import load_lock, resolve_lock_path
from tests.test_procedures.conftest import actor, provider_definition
from tests.test_procedures.test_execution import _accept, _receipt, _run, _stub_provider


def _pins(instance: CruxibleInstance, procedure_id: str) -> list[AcceptanceNodePin]:
    store = instance.get_procedure_store()
    try:
        return store.list_acceptance_node_pins(procedure_id)
    finally:
        store.close()


def test_acceptance_writes_one_pin_per_node_dependency(
    procedure_instance: CruxibleInstance,
) -> None:
    procedure_id = _accept(procedure_instance, provider_definition("pin_writer"))
    pins = _pins(procedure_instance, procedure_id)
    assert [(pin.node_id, pin.pin_kind, pin.pin_key) for pin in pins] == [
        ("invoke", "provider", "exported_action")
    ]
    payload = pins[0].pin_payload
    assert set(payload) == {
        "version",
        "ref",
        "provider_entrypoint_digest",
        "provider_command_path",
        "runtime",
        "deterministic",
        "side_effects",
        "artifact",
        "config",
    }
    assert pins[0].pin_digest == compute_pin_digest(payload)


def test_a_pin_is_a_payload_plus_its_digest_not_a_bare_digest(
    procedure_instance: CruxibleInstance,
) -> None:
    """A bare digest can be compared and cannot be READ.

    The payload is what lets a receipt reconstruct the accepted world and lets
    a mismatch name the field that moved.
    """
    procedure_id = _accept(procedure_instance, provider_definition("pin_readable"))
    pin = _pins(procedure_instance, procedure_id)[0]
    assert pin.pin_payload["runtime"] == "http_json"
    assert pin.pin_payload["deterministic"] is True
    assert pin.pin_payload["config"] == {"timeout_s": 5}


def test_integrity_catches_an_altered_payload_with_no_external_input() -> None:
    payload = {"version": "1.0", "ref": "https://example.invalid/a"}
    pin = AcceptanceNodePin(
        procedure_id="PRC-1",
        node_id="invoke",
        pin_kind="provider",
        pin_key="p",
        pin_payload=payload,
        pin_digest=compute_pin_digest(payload),
    )
    verify_pin_integrity([pin])
    tampered = pin.model_copy(update={"pin_payload": {**payload, "ref": "https://evil.invalid"}})
    with pytest.raises(ConfigError, match="storage corruption or tampering"):
        verify_pin_integrity([tampered])


def test_a_parameter_pin_has_no_currency_check_by_design(
    procedure_instance: CruxibleInstance,
) -> None:
    """Not omission.

    The pinned payload carries the VALUE, so it is the executable dependency;
    the only external candidate is the live revision, which is precisely what
    the pin exists to ignore. Comparing against it would turn every governed
    recalibration into a mass refusal of the procedures accepted under the old
    value.
    """
    payload = {
        "parameter_name": "kev_threshold",
        "revision_digest": "sha256:whatever",
        "value_type": "int",
        "value": 7,
    }
    pin = AcceptanceNodePin(
        procedure_id="PRC-1",
        node_id="gate",
        pin_kind="parameter",
        pin_key="kev_threshold",
        pin_payload=payload,
        pin_digest=compute_pin_digest(payload),
    )
    definition = provider_definition("param_currency")
    verify_pin_currency(
        [pin],
        definition=definition,
        config=procedure_instance.load_config(),
        lock=load_lock(resolve_lock_path(procedure_instance)),
    )


def test_the_run_receipt_carries_the_pin_material_in_the_root_node_detail(
    procedure_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Root-node `detail`, never a new top-level Receipt field.

    A new top-level field is silently DROPPED by a 0.3 reader -- the worst of
    the three behaviours, because the receipt would look complete and be
    incomplete. `detail` is arbitrary by contract.
    """
    _stub_provider(monkeypatch, lambda payload: {"value": int(payload.get("value", 0))})
    procedure_id = _accept(procedure_instance, provider_definition("pin_receipt"))
    result = service_run_procedure(
        procedure_instance,
        procedure_id,
        {"value": 5},
        actor("runner"),
    )
    receipt = _receipt(procedure_instance, result.run.receipt_id or "")
    root = receipt.nodes[0].detail
    assert root["node_pins"]["invoke"]["provider"]["exported_action"].startswith("sha256:")
    digest = root["node_pins"]["invoke"]["provider"]["exported_action"]
    # Self-contained: the run id alone recovers the accepted world.
    assert root["pin_payloads"][digest]["ref"] == "https://example.invalid/action"


def test_a_v2_procedure_with_its_pins_deleted_is_refused(
    procedure_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A v2 procedure only becomes live through an acceptance that writes pins."""
    _stub_provider(monkeypatch, lambda payload: {"value": int(payload.get("value", 0))})
    definition = ProcedureDefinition.model_validate(
        {
            **provider_definition("v2_needs_pins").model_dump(
                mode="json", by_alias=True, exclude_none=True
            ),
            "graph_format": 2,
        }
    )
    procedure_id = _accept(procedure_instance, definition)
    store = procedure_instance.get_procedure_store()
    try:
        store._conn.execute(
            "DELETE FROM procedure_acceptance_node_pins WHERE procedure_id = ?",
            (procedure_id,),
        )
        store._conn.commit()
    finally:
        store.close()
    with pytest.raises(ConfigError, match="format v2 but has no recorded per-node"):
        service_run_procedure(procedure_instance, procedure_id, {"value": 5}, actor("runner"))


def test_a_v1_procedure_without_per_node_pins_still_runs(
    procedure_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The only case, and it is finite.

    A v1 row's coarse acceptance digests remain authoritative, exactly as
    before; the convergence sweep drains the population rather than a refusal
    stranding it.
    """
    _stub_provider(monkeypatch, lambda payload: {"value": int(payload.get("value", 0))})
    procedure_id = _accept(procedure_instance, provider_definition("v1_no_pins"))
    store = procedure_instance.get_procedure_store()
    try:
        store._conn.execute(
            "DELETE FROM procedure_acceptance_node_pins WHERE procedure_id = ?",
            (procedure_id,),
        )
        store._conn.commit()
    finally:
        store.close()
    result = service_run_procedure(procedure_instance, procedure_id, {"value": 5}, actor("runner"))
    assert result.run.verdict == "succeeded"
    run = _run(procedure_instance, result.run.run_id)
    assert run.verdict == "succeeded"


def test_pins_ride_a_repeat_body(procedure_instance: CruxibleInstance) -> None:
    definition = ProcedureDefinition.model_validate(
        {
            "name": "pins_in_repeat",
            "contract_in": "ProcedureInput",
            "steps": [
                {
                    "id": "retry",
                    "as": "result",
                    "repeat": {
                        "max_attempts": 2,
                        "until": {
                            "left": "$steps.attempt.value",
                            "op": "gte",
                            "right": 0,
                            "message": "not settled",
                        },
                        "steps": [
                            {
                                "id": "attempt",
                                "provider": "exported_action",
                                "input": {"value": "$input.value"},
                                "as": "attempt",
                            }
                        ],
                    },
                }
            ],
            "returns": "result",
            "precondition": {},
            "budget": {"wall_clock_s": 30, "max_provider_calls": 4},
            "declared_tier": "graph_write",
        }
    )
    procedure_id = _accept(procedure_instance, definition)
    pins = _pins(procedure_instance, procedure_id)
    # The nested provider is pinned under the CONTAINER node: the repeat is one
    # node in the control graph, and its body is its own content.
    assert [(pin.node_id, pin.pin_key) for pin in pins] == [("retry", "exported_action")]


def _payload_of(pins: list[AcceptanceNodePin], key: str) -> dict[str, Any]:
    return next(pin.pin_payload for pin in pins if pin.pin_key == key)
