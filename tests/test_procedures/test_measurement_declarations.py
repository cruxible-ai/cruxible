"""Digest-covered procedure measurement declarations and M1-M5 refusals."""

from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ConfigError
from cruxible_core.procedure.digest import registered_envelope_fields
from cruxible_core.procedure.graph_format import registered_v2_definition_fields
from cruxible_core.procedure.types import (
    ProcedureDefinition,
    compute_procedure_definition_digest,
)
from cruxible_core.service import service_propose_procedure
from tests.test_procedures.conftest import actor


def _attestation_measurement() -> dict[str, object]:
    return {
        "kind": "attestation",
        "relationship_type": "blocks",
        "from_type": "Task",
        "from_id": "T-1",
        "to_type": "Incident",
        "to_id": "I-1",
    }


def _definition_payload() -> dict[str, object]:
    return {
        "graph_format": 2,
        "name": "measured_routing",
        "contract_in": "cruxible.JsonObject",
        "steps": [
            {
                "id": "route",
                "guard": {"left": "$input.flag", "op": "eq", "right": True},
                "on_true": "shared",
                "on_false": "shared",
                "message": "flag required",
            },
            {"id": "shared", "assert_exists": {"ref": "$input.flag"}},
        ],
        "returns": "shared",
        "precondition": {},
        "budget": {"wall_clock_s": 30, "max_provider_calls": 0},
        "measurements": [
            {
                "name": "true_arm_quality",
                "granularity": "arm",
                "node_id": "shared",
                "from_node_id": "route",
                "arm_label": "on_true",
                "measurement": _attestation_measurement(),
                "check_after_days": 1,
                "expires_after_days": 30,
                "review_when": [
                    {
                        "name": "contrast",
                        "metric": "arm_contrast",
                        "op": "gte",
                        "value": 0.2,
                        "min_readings": 5,
                    }
                ],
            }
        ],
    }


def test_measurements_are_registered_in_format_and_digest_envelope() -> None:
    assert "measurements" in registered_envelope_fields()
    assert (ProcedureDefinition, "measurements") in registered_v2_definition_fields()

    measured = ProcedureDefinition.model_validate(_definition_payload())
    unmeasured_payload = _definition_payload()
    unmeasured_payload["measurements"] = None
    unmeasured = ProcedureDefinition.model_validate(unmeasured_payload)

    assert compute_procedure_definition_digest(measured) != compute_procedure_definition_digest(
        unmeasured
    )


def test_m1_refuses_unknown_non_unit_node() -> None:
    payload = _definition_payload()
    measurements = deepcopy(payload["measurements"])
    assert isinstance(measurements, list)
    measurements[0]["node_id"] = "missing"
    payload["measurements"] = measurements

    with pytest.raises(ValidationError, match="M1.*does not name a node"):
        ProcedureDefinition.model_validate(payload)


def test_m2_refuses_non_successor_arm_coordinates() -> None:
    payload = _definition_payload()
    measurements = deepcopy(payload["measurements"])
    assert isinstance(measurements, list)
    measurements[0]["arm_label"] = "on_false"
    payload["measurements"] = measurements
    steps = deepcopy(payload["steps"])
    assert isinstance(steps, list)
    steps[0]["on_false"] = "$abort"
    payload["steps"] = steps

    with pytest.raises(ValidationError, match="M2.*on_false successor"):
        ProcedureDefinition.model_validate(payload)


def test_m3_refuses_duplicate_measurement_names() -> None:
    payload = _definition_payload()
    measurements = deepcopy(payload["measurements"])
    assert isinstance(measurements, list)
    measurements.append(deepcopy(measurements[0]))
    measurements[1]["arm_label"] = "on_false"
    payload["measurements"] = measurements

    with pytest.raises(ValidationError, match="M3.*unique"):
        ProcedureDefinition.model_validate(payload)


def test_m4_refuses_arm_contrast_outside_arm_granularity() -> None:
    payload = _definition_payload()
    measurements = deepcopy(payload["measurements"])
    assert isinstance(measurements, list)
    measurements[0].update({"granularity": "node", "from_node_id": None, "arm_label": None})
    payload["measurements"] = measurements

    with pytest.raises(ValidationError, match="M4.*arm_contrast"):
        ProcedureDefinition.model_validate(payload)


def test_m5_refuses_procedure_measurement_kind() -> None:
    payload = _definition_payload()
    measurements = deepcopy(payload["measurements"])
    assert isinstance(measurements, list)
    measurements[0]["measurement"] = {"kind": "procedure"}
    payload["measurements"] = measurements

    with pytest.raises(ValidationError, match="M5.*measuring itself is a cycle"):
        ProcedureDefinition.model_validate(payload)


def test_situation_shape_entity_types_resolve_against_active_config(
    procedure_instance: CruxibleInstance,
) -> None:
    payload = _definition_payload()
    measurements = deepcopy(payload["measurements"])
    assert isinstance(measurements, list)
    measurements[0]["situation_shape"] = {"entity_types": ["MissingType"]}
    payload["measurements"] = measurements
    definition = ProcedureDefinition.model_validate(payload)

    with pytest.raises(ConfigError, match="unknown entity types.*MissingType"):
        service_propose_procedure(
            procedure_instance,
            definition,
            actor_context=actor("proposer"),
        )
