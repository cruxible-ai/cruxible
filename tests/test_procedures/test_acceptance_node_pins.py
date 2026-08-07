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
    with pytest.raises(ConfigError, match="incomplete set of per-node acceptance pins"):
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
    # The nested provider is pinned under the NAMESPACED nested node id -- the
    # same id its Merkle identity carries. Attributing it to the container
    # would leave the pin naming a node whose digest does not exist, so no pin
    # could be joined to the digest of the node it actually pins.
    assert [(pin.node_id, pin.pin_key) for pin in pins] == [("retry/attempt", "exported_action")]

    store = procedure_instance.get_procedure_store()
    try:
        digest_ids = {digest.node_id for digest in store.list_node_digests(procedure_id)}
    finally:
        store.close()
    assert {pin.node_id for pin in pins} <= digest_ids, (
        "every pin must name a node that has a Merkle identity, or it cannot be "
        "joined to the decision point it pins"
    )


def _payload_of(pins: list[AcceptanceNodePin], key: str) -> dict[str, Any]:
    return next(pin.pin_payload for pin in pins if pin.pin_key == key)


def test_a_partially_deleted_pin_set_is_refused_though_the_coarse_digests_still_match(
    procedure_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Completeness is a SET question, not a non-emptiness test.

    Nothing about the config or the lock moved, so the coarse digests agree and
    every remaining pin verifies against itself. One dependency of two is
    simply unaccounted for -- and a receipt built from that set would look
    complete while describing half the world the run executed in.
    """
    _stub_provider(monkeypatch, lambda payload: {"value": int(payload.get("value", 0))})
    definition = ProcedureDefinition.model_validate(
        {
            "name": "two_dependencies",
            "contract_in": "ProcedureInput",
            "steps": [
                {
                    "id": "first",
                    "provider": "exported_action",
                    "input": {"value": "$input.value"},
                    "as": "a",
                },
                {
                    "id": "second",
                    "provider": "exported_action",
                    "input": {"value": "$input.value"},
                    "as": "result",
                },
            ],
            "returns": "result",
            "precondition": {},
            "budget": {"wall_clock_s": 30, "max_provider_calls": 4},
            "declared_tier": "graph_write",
        }
    )
    procedure_id = _accept(procedure_instance, definition)
    assert len(_pins(procedure_instance, procedure_id)) == 2

    store = procedure_instance.get_procedure_store()
    try:
        store._conn.execute(
            "DELETE FROM procedure_acceptance_node_pins WHERE procedure_id = ? AND node_id = ?",
            (procedure_id, "first"),
        )
        store._conn.commit()
    finally:
        store.close()

    with pytest.raises(ConfigError, match=r"missing \[\('first', 'provider'") as exc_info:
        service_run_procedure(procedure_instance, procedure_id, {"value": 5}, actor("runner"))
    assert "incomplete set of per-node acceptance pins" in str(exc_info.value)


def test_a_v2_graph_with_no_external_dependencies_expects_the_empty_pin_set(
    procedure_instance: CruxibleInstance,
) -> None:
    """The empty expected set is legal, and a run under it is not refused.

    A graph of guards and projections over `$input` alone declares no provider,
    no query and no artifact. Its complete pin set is empty, and treating
    emptiness as evidence of a missing acceptance would make an entire legal
    shape of v2 procedure unrunnable.
    """
    definition = ProcedureDefinition.model_validate(
        {
            "name": "input_only_graph",
            "contract_in": "ProcedureInput",
            "graph_format": 2,
            "steps": [
                {
                    "id": "gate",
                    "guard": {"left": "$input.value", "op": "gte", "right": 0},
                    "on_false": "$abort",
                    "message": "value must be non-negative",
                },
                {
                    "id": "shape",
                    "project": {"fields": {"echoed": "$input.value"}},
                    "as": "result",
                },
            ],
            "returns": "result",
            "precondition": {},
            "budget": {"wall_clock_s": 30, "max_provider_calls": 0},
            "declared_tier": "graph_write",
        }
    )
    procedure_id = _accept(procedure_instance, definition)
    assert _pins(procedure_instance, procedure_id) == []

    result = service_run_procedure(procedure_instance, procedure_id, {"value": 7}, actor("runner"))
    assert result.run.verdict == "succeeded"
    assert result.output == {"echoed": 7}
