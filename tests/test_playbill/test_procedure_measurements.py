"""Typed graph-v3 measurement declarations and digest-coverage laws."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactPin,
)
from cruxible_client.contracts.canonical import ArtifactDigest, typed_digest
from cruxible_client.contracts.captures import CanonicalDurationV1
from cruxible_client.contracts.procedures.artifacts import ProcedureArtifactV1, render_procedure
from cruxible_client.contracts.procedures.graph import (
    compute_procedure_definition_digest_v3,
    compute_procedure_node_digests_v3,
)
from cruxible_client.contracts.procedures.measurements import (
    AcceptedQueryProcedureMeasurementV1,
    ClaimAttestationProcedureMeasurementV1,
    ClaimStatementProcedureMeasurementV1,
    ProcedureMeasurementDeclarationV1,
    ProcedureMeasurementExpectationV1,
    ProcedureMeasurementReviewTriggerV1,
    ProcedureMeasurementSituationShapeV1,
)
from cruxible_client.contracts.procedures.models import (
    GuardNodeV3,
    GuardPredicateV1,
    PredicateOperandV1,
    ProcedureBudgetV3,
    ProcedureDefinitionV3,
    ProcedureHardCapsV3,
    ProjectNodeV3,
    StateTapNodeV3,
    iter_pin_bindings,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_core.playbill.compiler import PC_D_COMPILER, projection_registry_for_compiler
from cruxible_core.playbill.projection_artifacts import parse_projection_tree


def _digest(label: str) -> str:
    return typed_digest(
        ArtifactDigest,
        "playbill-procedure-measurement-test-v1",
        {"label": label},
    ).tagged


def _pin(role: str, kind: str, name: str) -> ArtifactPin:
    return ArtifactPin(
        role=role,
        target=ArtifactIdentity(kind=kind, name=name),
        artifact_digest=_digest(name),
    )


def _duration(microseconds: int) -> CanonicalDurationV1:
    return CanonicalDurationV1(microseconds=microseconds)


def _expectation() -> ProcedureMeasurementExpectationV1:
    return ProcedureMeasurementExpectationV1(
        min_count=1,
        condition={"status": "healthy"},
    )


def _query_measurement(name: str = "outcome-query") -> AcceptedQueryProcedureMeasurementV1:
    return AcceptedQueryProcedureMeasurementV1(
        query=_pin("query", "QueryDefinition", name),
        parameters={"status": "active"},
        execution_options={"relationship_state": "accepted"},
        expect=_expectation(),
    )


def _declaration(
    *,
    name: str = "healthy-outcome",
    subject_grain: str = "procedure_unit",
    node_id: str | None = None,
    from_node_id: str | None = None,
    arm_label: str | None = None,
    measurement: object | None = None,
    review_when: tuple[ProcedureMeasurementReviewTriggerV1, ...] = (),
) -> ProcedureMeasurementDeclarationV1:
    return ProcedureMeasurementDeclarationV1(
        name=name,
        subject_grain=subject_grain,  # type: ignore[arg-type]
        node_id=node_id,
        from_node_id=from_node_id,
        arm_label=arm_label,  # type: ignore[arg-type]
        measurement=measurement or _query_measurement(),  # type: ignore[arg-type]
        check_after=_duration(0),
        expires_after=_duration(86_400_000_000),
        situation_shape=ProcedureMeasurementSituationShapeV1(
            subject_kinds=("Claim",),
            task_category="release",
            tags=("health", "release"),
        ),
        review_when=review_when,
    )


def _predicate(alias: str) -> GuardPredicateV1:
    return GuardPredicateV1(
        left=PredicateOperandV1(kind="step", alias=alias),
        operator="eq",
        right=PredicateOperandV1(kind="literal", value=True),
    )


def _definition(
    measurements: tuple[ProcedureMeasurementDeclarationV1, ...] = (),
) -> ProcedureDefinitionV3:
    contract_in = _pin("contract-in", "Contract", "empty-input")
    contract_out = _pin("contract-out", "Contract", "result")
    return ProcedureDefinitionV3(
        name="measured-procedure",
        contract_in=contract_in,
        contract_out=contract_out,
        nodes=(
            StateTapNodeV3(
                node_id="read",
                query=_pin("query", "QueryDefinition", "accepted-state"),
                as_="rows",
            ),
            GuardNodeV3(
                node_id="gate",
                predicate=_predicate("rows"),
                on_true="hot",
                on_false="cold",
                refusal_code="outcome.branch",
                message="Choose one measured arm.",
            ),
            ProjectNodeV3(
                node_id="hot",
                fields={"arm": "hot"},
                contract_out=contract_out,
                as_="hot_rows",
                next="finish",
            ),
            ProjectNodeV3(
                node_id="cold",
                fields={"arm": "cold"},
                contract_out=contract_out,
                as_="cold_rows",
                next="finish",
            ),
            ProjectNodeV3(
                node_id="finish",
                fields={"status": "complete"},
                contract_out=contract_out,
                as_="result",
            ),
        ),
        returns="result",
        measurements=measurements,
        budget=ProcedureBudgetV3(
            wall_clock=_duration(1_000_000),
            max_provider_calls=0,
            max_capture_bytes=0,
            max_items=100,
        ),
        hard_caps=ProcedureHardCapsV3(
            max_wall_clock=_duration(2_000_000),
            max_provider_calls=0,
            max_capture_bytes=0,
            max_items=200,
            max_repeat_attempts=1,
        ),
        terminal_capability=1,
    )


def _artifact(definition: ProcedureDefinitionV3, *, include_all_pins: bool) -> ProcedureArtifactV1:
    exact_pins = {
        binding for binding in iter_pin_bindings(definition) if isinstance(binding, ArtifactPin)
    }
    pins = (
        exact_pins
        if include_all_pins
        else {pin for pin in exact_pins if pin.target.name != "outcome-query"}
    )
    return ProcedureArtifactV1(
        identity=ArtifactIdentity(kind="Procedure", name=definition.name),
        definition=definition,
        definition_digest=compute_procedure_definition_digest_v3(definition).tagged,
        pins=tuple(
            sorted(
                pins,
                key=lambda pin: (
                    pin.role.encode(),
                    pin.target.qualified.encode(),
                    pin.artifact_digest.encode(),
                ),
            )
        ),
        activation_policy="snapshot",
    )


def test_measurement_declaration_moves_only_the_v3_definition_envelope_digest() -> None:
    baseline = _definition()
    measured = _definition((_declaration(),))

    baseline_nodes = compute_procedure_node_digests_v3(baseline)
    measured_nodes = compute_procedure_node_digests_v3(measured)
    assert baseline_nodes == measured_nodes
    assert compute_procedure_definition_digest_v3(baseline) != (
        compute_procedure_definition_digest_v3(measured)
    )
    assert compute_procedure_definition_digest_v3(measured).tagged == (
        "sha256:06d1fb0552b1d1b1ed2c4044035e987cf1ed89af164d8ed44321629bf24cc841"
    )
    payload = measured.model_dump(mode="json", by_alias=True)
    assert payload["annotations"] == {}
    assert payload["measurements"][0]["tag"] == ("playbill-procedure-measurement-declaration-v1")


def test_measurement_query_is_an_exact_envelope_dependency_not_a_line_slot() -> None:
    definition = _definition((_declaration(),))

    with pytest.raises(ValidationError, match="exact pins absent"):
        _artifact(definition, include_all_pins=False)
    assert _artifact(definition, include_all_pins=True).directly_runnable is True

    with pytest.raises(ValidationError, match="exact role='query'.*QueryDefinition"):
        AcceptedQueryProcedureMeasurementV1(
            query=_pin("provider", "Provider", "outcome-query"),
            expect=_expectation(),
        )
    slot_payload = _query_measurement().model_dump(mode="json")
    slot_payload["query"] = {
        "tag": "playbill-procedure-pin-slot-ref-v1",
        "slot_name": "query",
    }
    with pytest.raises(ValidationError):
        AcceptedQueryProcedureMeasurementV1.model_validate(slot_payload)


def test_measurements_are_projected_from_the_typed_field_not_annotations() -> None:
    definition = _definition((_declaration(),))
    procedure = _artifact(definition, include_all_pins=True)
    path = "procedures/measured-procedure.json"
    projection = parse_projection_tree(
        {path: render_procedure(procedure)},
        registry=projection_registry_for_compiler(PC_D_COMPILER),
    )
    fact = next(
        item
        for item in projection.semantic_facts
        if item.schema_id == "playbill.procedure.definition"
    )

    assert fact.value["measurements"] == [definition.measurements[0].model_dump(mode="json")]
    assert "measurements" not in definition.annotations


def test_measurement_names_are_canonical_sorted_and_unique() -> None:
    later = _declaration(name="zeta")
    earlier = _declaration(name="alpha")
    with pytest.raises(ValidationError, match="M3"):
        _definition((later, earlier))
    with pytest.raises(ValidationError, match="M3"):
        _definition((earlier, earlier))
    with pytest.raises(ValidationError, match="canonical lowercase identifier"):
        _declaration(name="Not Canonical")


def test_node_and_arm_measurements_bind_real_graph_v3_coordinates() -> None:
    node = _declaration(name="node-outcome", subject_grain="node", node_id="hot")
    arm = _declaration(
        name="arm-outcome",
        subject_grain="arm",
        node_id="hot",
        from_node_id="gate",
        arm_label="on_true",
    )
    assert len(_definition((arm, node)).measurements) == 2

    unknown_payload = _definition((node,)).model_dump(mode="json", by_alias=True)
    unknown_payload["measurements"][0]["node_id"] = "missing"
    with pytest.raises(ValidationError, match="M1"):
        ProcedureDefinitionV3.model_validate(unknown_payload)

    wrong_arm_payload = _definition((arm,)).model_dump(mode="json", by_alias=True)
    wrong_arm_payload["measurements"][0]["node_id"] = "cold"
    with pytest.raises(ValidationError, match="M2"):
        ProcedureDefinitionV3.model_validate(wrong_arm_payload)


def test_self_measurement_and_non_arm_contrast_are_refused_before_activation() -> None:
    payload = _declaration().model_dump(mode="json")
    payload["measurement"] = {"kind": "procedure_reading"}
    with pytest.raises(ValidationError, match="M5"):
        ProcedureMeasurementDeclarationV1.model_validate(payload)

    contrast = ProcedureMeasurementReviewTriggerV1(
        name="arm-drift",
        metric="arm_contrast",
        operator="gte",
        threshold={"$decimal": "0.2"},
        min_readings=10,
    )
    with pytest.raises(ValidationError, match="M4"):
        _declaration(review_when=(contrast,))


def test_measurement_windows_expectations_and_review_thresholds_are_canonical() -> None:
    with pytest.raises(ValidationError, match="less than expires_after"):
        ProcedureMeasurementDeclarationV1(
            **{
                **_declaration().model_dump(mode="python"),
                "check_after": _duration(10),
                "expires_after": _duration(10),
            }
        )
    with pytest.raises(ValidationError, match="vacuous satisfaction"):
        ProcedureMeasurementExpectationV1(condition={"ready": True})
    with pytest.raises(ValidationError, match="decimal spelling"):
        ProcedureMeasurementReviewTriggerV1(
            name="drift",
            metric="contradicted_rate",
            operator="gte",
            threshold={"$decimal": "0.20"},
            min_readings=5,
        )
    with pytest.raises(ValidationError, match="floating-point"):
        AcceptedQueryProcedureMeasurementV1(
            query=_pin("query", "QueryDefinition", "outcome-query"),
            parameters={"threshold": 0.5},
            expect=_expectation(),
        )


def test_claim_measurements_bind_exact_statement_addresses_and_digests() -> None:
    statement = SemanticAddress.claim_statement(
        "claims/ab/CLM-ab000000000000000000000000000000.json"
    )
    digest = _digest("claim-statement")
    attestation = ClaimAttestationProcedureMeasurementV1(
        claim_statement=statement,
        claim_statement_digest=digest,
        stances=("contradict", "support"),
        expect=ProcedureMeasurementExpectationV1(min_count=1),
    )
    claim = ClaimStatementProcedureMeasurementV1(
        claim_statement=statement,
        claim_statement_digest=digest,
        acceptable_verdicts=("supported", "unresolved"),
    )
    assert _definition(
        (
            _declaration(name="attestation-outcome", measurement=attestation),
            _declaration(name="claim-outcome", measurement=claim),
        )
    ).measurements
    wrong_subject = SemanticAddress.procedure_unit("procedures/measured-procedure.json")
    with pytest.raises(ValidationError, match="Claim statement address"):
        ClaimStatementProcedureMeasurementV1(
            claim_statement=wrong_subject,
            claim_statement_digest=digest,
            acceptable_verdicts=("supported",),
        )
