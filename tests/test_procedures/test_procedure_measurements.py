"""Typed measurement declarations and digest-coverage laws."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactPin,
)
from cruxible_client.contracts.canonical import ArtifactDigest, typed_digest
from cruxible_client.contracts.captures import CanonicalDuration
from cruxible_client.contracts.procedures.artifacts import (
    ProcedureArtifact,
    procedure_runnability,
    render_procedure,
)
from cruxible_client.contracts.procedures.graph import (
    compute_procedure_definition_digest,
    compute_procedure_node_digests,
)
from cruxible_client.contracts.procedures.measurements import (
    AcceptedQueryProcedureMeasurement,
    ClaimAttestationProcedureMeasurement,
    ClaimStatementProcedureMeasurement,
    ProcedureMeasurementDeclaration,
    ProcedureMeasurementExpectation,
    ProcedureMeasurementReviewTrigger,
    ProcedureMeasurementSituationShape,
)
from cruxible_client.contracts.procedures.models import (
    GuardNode,
    GuardPredicate,
    PredicateOperand,
    ProcedureBudget,
    ProcedureDefinition,
    ProcedureHardCaps,
    ProjectNode,
    StateTapNode,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_core.compiler.compiler import (
    GOVERNED_TRIGGERS_COMPILER,
    projection_registry_for_compiler,
)
from cruxible_core.compiler.projection_artifacts import (
    GOVERNED_TRIGGERS_ARTIFACT_KINDS,
    parse_projection_tree,
)
from tests.support.procedures import owned_contract, owned_pin, procedure_artifact


def _digest(label: str) -> str:
    return typed_digest(
        ArtifactDigest,
        "playbill-procedure-measurement-test-v1",
        {"label": label},
    ).tagged


def _pin(role: str, kind: str, name: str) -> ArtifactPin:
    if kind == "Contract":
        # Contracts ride in the envelope: pin the exact owned Contract digest.
        return owned_pin(role, owned_contract(name))
    return ArtifactPin(
        role=role,
        target=ArtifactIdentity(kind=kind, name=name),
        artifact_digest=_digest(name),
    )


def _duration(microseconds: int) -> CanonicalDuration:
    return CanonicalDuration(microseconds=microseconds)


def _expectation() -> ProcedureMeasurementExpectation:
    return ProcedureMeasurementExpectation(
        min_count=1,
        condition={"status": "healthy"},
    )


def _query_measurement(name: str = "outcome-query") -> AcceptedQueryProcedureMeasurement:
    return AcceptedQueryProcedureMeasurement(
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
    review_when: tuple[ProcedureMeasurementReviewTrigger, ...] = (),
) -> ProcedureMeasurementDeclaration:
    return ProcedureMeasurementDeclaration(
        name=name,
        subject_grain=subject_grain,  # type: ignore[arg-type]
        node_id=node_id,
        from_node_id=from_node_id,
        arm_label=arm_label,  # type: ignore[arg-type]
        measurement=measurement or _query_measurement(),  # type: ignore[arg-type]
        check_after=_duration(0),
        expires_after=_duration(86_400_000_000),
        situation_shape=ProcedureMeasurementSituationShape(
            subject_kinds=("Claim",),
            task_category="release",
            tags=("health", "release"),
        ),
        review_when=review_when,
    )


def _predicate(alias: str) -> GuardPredicate:
    return GuardPredicate(
        left=PredicateOperand(kind="step", alias=alias),
        operator="eq",
        right=PredicateOperand(kind="literal", value=True),
    )


def _definition(
    measurements: tuple[ProcedureMeasurementDeclaration, ...] = (),
) -> ProcedureDefinition:
    contract_in = _pin("contract-in", "Contract", "empty-input")
    contract_out = _pin("contract-out", "Contract", "result")
    return ProcedureDefinition(
        name="measured-procedure",
        contract_in=contract_in,
        contract_out=contract_out,
        nodes=(
            StateTapNode(
                node_id="read",
                query=_pin("query", "QueryDefinition", "accepted-state"),
                as_="rows",
            ),
            GuardNode(
                node_id="gate",
                predicate=_predicate("rows"),
                on_true="hot",
                on_false="cold",
                refusal_code="outcome.branch",
                message="Choose one measured arm.",
            ),
            ProjectNode(
                node_id="hot",
                fields={"arm": "hot"},
                contract_out=contract_out,
                as_="hot_rows",
                next="finish",
            ),
            ProjectNode(
                node_id="cold",
                fields={"arm": "cold"},
                contract_out=contract_out,
                as_="cold_rows",
                next="finish",
            ),
            ProjectNode(
                node_id="finish",
                fields={"status": "complete"},
                contract_out=contract_out,
                as_="result",
            ),
        ),
        returns="result",
        measurements=measurements,
        budget=ProcedureBudget(
            wall_clock=_duration(1_000_000),
            max_provider_calls=0,
            max_capture_bytes=0,
            max_items=100,
        ),
        hard_caps=ProcedureHardCaps(
            max_wall_clock=_duration(2_000_000),
            max_provider_calls=0,
            max_capture_bytes=0,
            max_items=200,
            max_repeat_attempts=1,
        ),
        terminal_capability=1,
    )


def _artifact(definition: ProcedureDefinition, *, include_all_pins: bool) -> ProcedureArtifact:
    procedure = procedure_artifact(definition, activation_policy="snapshot")
    if include_all_pins:
        return procedure
    return ProcedureArtifact(
        **{
            **procedure.model_dump(mode="python", exclude={"artifact_format"}),
            "pins": tuple(pin for pin in procedure.pins if pin.target.name != "outcome-query"),
        }
    )


def test_measurement_declaration_moves_only_the_definition_envelope_digest() -> None:
    baseline = _definition()
    measured = _definition((_declaration(),))

    baseline_nodes = compute_procedure_node_digests(baseline)
    measured_nodes = compute_procedure_node_digests(measured)
    assert baseline_nodes == measured_nodes
    assert compute_procedure_definition_digest(baseline) != (
        compute_procedure_definition_digest(measured)
    )
    assert compute_procedure_definition_digest(measured).tagged == (
        "sha256:2769d12c4b50a4c966328173c8ebf90b54ac1613c938ff3d3ec10f192ea5c138"
    )
    payload = measured.model_dump(mode="json", by_alias=True)
    assert payload["annotations"] == {}
    assert payload["measurements"][0]["tag"] == ("playbill-procedure-measurement-declaration-v1")


def test_measurement_query_is_an_exact_envelope_dependency_not_a_line_slot() -> None:
    definition = _definition((_declaration(),))

    with pytest.raises(ValidationError, match="exact pins absent"):
        _artifact(definition, include_all_pins=False)
    assert procedure_runnability(_artifact(definition, include_all_pins=True).definition) == (
        "direct",
        (),
    )

    with pytest.raises(ValidationError, match="exact role='query'.*QueryDefinition"):
        AcceptedQueryProcedureMeasurement(
            query=_pin("provider", "Provider", "outcome-query"),
            expect=_expectation(),
        )
    slot_payload = _query_measurement().model_dump(mode="json")
    slot_payload["query"] = {
        "tag": "playbill-procedure-pin-slot-ref-v1",
        "slot_name": "query",
    }
    with pytest.raises(ValidationError):
        AcceptedQueryProcedureMeasurement.model_validate(slot_payload)


def test_measurements_are_projected_from_the_typed_field_not_annotations() -> None:
    definition = _definition((_declaration(),))
    procedure = _artifact(definition, include_all_pins=True)
    path = "procedures/measured-procedure.json"
    projection = parse_projection_tree(
        {path: render_procedure(procedure)},
        registry=projection_registry_for_compiler(GOVERNED_TRIGGERS_COMPILER),
        artifact_kinds=GOVERNED_TRIGGERS_ARTIFACT_KINDS,
    )
    fact = next(
        item
        for item in projection.semantic_facts
        if item.schema_id == "cruxible.procedure.definition"
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


def test_node_and_arm_measurements_bind_real_graph_coordinates() -> None:
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
        ProcedureDefinition.model_validate(unknown_payload)

    wrong_arm_payload = _definition((arm,)).model_dump(mode="json", by_alias=True)
    wrong_arm_payload["measurements"][0]["node_id"] = "cold"
    with pytest.raises(ValidationError, match="M2"):
        ProcedureDefinition.model_validate(wrong_arm_payload)


def test_self_measurement_and_non_arm_contrast_are_refused_before_activation() -> None:
    payload = _declaration().model_dump(mode="json")
    payload["measurement"] = {"kind": "procedure_reading"}
    with pytest.raises(ValidationError, match="M5"):
        ProcedureMeasurementDeclaration.model_validate(payload)

    contrast = ProcedureMeasurementReviewTrigger(
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
        ProcedureMeasurementDeclaration(
            **{
                **_declaration().model_dump(mode="python"),
                "check_after": _duration(10),
                "expires_after": _duration(10),
            }
        )
    with pytest.raises(ValidationError, match="vacuous satisfaction"):
        ProcedureMeasurementExpectation(condition={"ready": True})
    with pytest.raises(ValidationError, match="decimal spelling"):
        ProcedureMeasurementReviewTrigger(
            name="drift",
            metric="contradicted_rate",
            operator="gte",
            threshold={"$decimal": "0.20"},
            min_readings=5,
        )
    with pytest.raises(ValidationError, match="floating-point"):
        AcceptedQueryProcedureMeasurement(
            query=_pin("query", "QueryDefinition", "outcome-query"),
            parameters={"threshold": 0.5},
            expect=_expectation(),
        )


def test_claim_measurements_bind_exact_statement_addresses_and_digests() -> None:
    statement = SemanticAddress.claim_statement(
        "claims/ab/CLM-ab000000000000000000000000000000.json"
    )
    digest = _digest("claim-statement")
    attestation = ClaimAttestationProcedureMeasurement(
        claim_statement=statement,
        claim_statement_digest=digest,
        stances=("contradict", "support"),
        expect=ProcedureMeasurementExpectation(min_count=1),
    )
    claim = ClaimStatementProcedureMeasurement(
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
        ClaimStatementProcedureMeasurement(
            claim_statement=wrong_subject,
            claim_statement_digest=digest,
            acceptable_verdicts=("supported",),
        )
