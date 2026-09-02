"""Self-attacks for Capture-v2 provenance and exact-tag succession."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.captures import (
    CaptureEnvelopeV1,
    CaptureEnvelopeV2,
    CaptureFormatError,
    CaptureRunCoordinateV1,
    ProcedureEgressCaptureEvidenceV1,
    ProviderInvocationCaptureEvidenceV1,
    build_cas_capture,
    build_provider_external_capture_v2,
    capture_digest,
    parse_capture_envelope,
    render_capture_envelope,
    verify_capture,
)
from cruxible_client.contracts.providers import provider_digest
from cruxible_client.contracts.source_references import (
    CasSourceReferenceV1,
    EvidenceCommitmentV1,
)
from tests.test_playbill._pc_c_support import NOW, body_store, capture_contract, digest, provider
from tests.test_playbill.p2b4_unit1._support import provider_capture_fixture


def test_v1_bytes_and_digest_are_unchanged_under_the_exact_tag_union(tmp_path: Path) -> None:
    contract = capture_contract()
    provider_artifact = provider(contract)
    built = build_cas_capture(
        store=body_store(tmp_path),
        contract=contract,
        source_body=b'{"status":"released"}',
        run_coordinate=CaptureRunCoordinateV1(
            run_kind="provider",
            run_id="provider-run-1",
            bound_generation=digest("test-generation", "g1"),
            executable_identity=provider_artifact.identity,
            executable_digest=provider_digest(provider_artifact).tagged,
        ),
        run_receipt_digest=digest("receipt", "cas"),
        producer=provider_artifact.identity,
        producer_binding_digest=digest("binding", "cas"),
        observed_at=NOW,
    )

    wire = render_capture_envelope(built.envelope)

    assert isinstance(parse_capture_envelope(wire), CaptureEnvelopeV1)
    assert capture_digest(parse_capture_envelope(wire)).tagged == built.capture_digest


def test_provider_v2_round_trips_and_replays_complete_runtime_evidence(tmp_path: Path) -> None:
    fixture = provider_capture_fixture(tmp_path)
    built = build_provider_external_capture_v2(
        store=fixture.store,
        contract=fixture.contract,
        result=fixture.result,
        receipt=fixture.receipt,
        occurrence=fixture.occurrence,
        producer=fixture.producer,
        bound_generation=fixture.bound_generation,
    )

    parsed = parse_capture_envelope(render_capture_envelope(built.envelope))

    assert isinstance(parsed, CaptureEnvelopeV2)
    assert parsed == built.envelope
    assert (
        verify_capture(
            built.capture_digest,
            store=fixture.store,
            contract=fixture.contract,
            producer_artifact_digests={
                fixture.producer.qualified: fixture.receipt.provider_artifact_digest
            },
            provider_invocation_receipt=fixture.receipt,
            provider_occurrence=fixture.occurrence,
        )
        == built.envelope
    )


@pytest.mark.parametrize(
    "field",
    [
        "interface_artifact_digest",
        "interface_digest",
        "implementation_digest",
        "materialization_digest",
        "input_bucket",
        "secret_references",
        "egress",
    ],
)
def test_every_provider_evidence_edge_is_mutation_sensitive(
    tmp_path: Path,
    field: str,
) -> None:
    fixture = provider_capture_fixture(tmp_path)
    built = build_provider_external_capture_v2(
        store=fixture.store,
        contract=fixture.contract,
        result=fixture.result,
        receipt=fixture.receipt,
        occurrence=fixture.occurrence,
        producer=fixture.producer,
        bound_generation=fixture.bound_generation,
    )
    assert isinstance(built.envelope, CaptureEnvelopeV2)
    evidence = built.envelope.production_evidence
    assert isinstance(evidence, ProviderInvocationCaptureEvidenceV1)
    replacement: object = digest("mutation", field)
    if field == "input_bucket":
        replacement = "kind=mutated"
    elif field == "secret_references":
        replacement = ()
    elif field == "egress":
        replacement = evidence.egress.model_copy(update={"observed_endpoints": ()})
    mutated = built.envelope.model_copy(
        update={"production_evidence": evidence.model_copy(update={field: replacement})}
    )
    fixture.store.store(render_capture_envelope(mutated))

    with pytest.raises(CaptureFormatError, match="runtime evidence does not correspond"):
        verify_capture(
            capture_digest(mutated).tagged,
            store=fixture.store,
            contract=fixture.contract,
            producer_artifact_digests={
                fixture.producer.qualified: fixture.receipt.provider_artifact_digest
            },
            provider_invocation_receipt=fixture.receipt,
            provider_occurrence=fixture.occurrence,
        )


def test_provider_result_cannot_diverge_from_the_durable_invocation_receipt(
    tmp_path: Path,
) -> None:
    fixture = provider_capture_fixture(tmp_path)
    changed = fixture.result.model_copy(update={"selector": {"id": 8, "relation": "orders"}})

    with pytest.raises(CaptureFormatError, match="differs from its invocation receipt"):
        build_provider_external_capture_v2(
            store=fixture.store,
            contract=fixture.contract,
            result=changed,
            receipt=fixture.receipt,
            occurrence=fixture.occurrence,
            producer=fixture.producer,
            bound_generation=fixture.bound_generation,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("occurrence_path", "source/other"),
        ("deployment_digest", digest("deployment", "other")),
        ("materialization_digest", digest("materialization", "other")),
        ("capture_contract_digest", digest("contract", "other")),
    ],
)
def test_invocation_receipt_cannot_be_crossed_with_another_admitted_occurrence(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    fixture = provider_capture_fixture(tmp_path)
    receipt = fixture.receipt.model_copy(update={field: value})

    with pytest.raises(CaptureFormatError, match="admitted contract"):
        build_provider_external_capture_v2(
            store=fixture.store,
            contract=fixture.contract,
            result=fixture.result,
            receipt=receipt,
            occurrence=fixture.occurrence,
            producer=fixture.producer,
            bound_generation=fixture.bound_generation,
        )


def test_procedure_evidence_arm_prohibits_provider_only_fields() -> None:
    receipt_digest = digest("producer-receipt", "procedure")
    procedure_digest = digest("procedure", "one")
    envelope = CaptureEnvelopeV2(
        capture_contract_digest=digest("contract", "one"),
        source=CasSourceReferenceV1(content_digest=digest("body", "one")),
        commitment=EvidenceCommitmentV1(
            digest_kind="exact_bytes",
            digest=digest("body", "one"),
            byte_length=1,
            materialization="cas",
        ),
        run_coordinate=CaptureRunCoordinateV1(
            run_kind="procedure",
            run_id="run-procedure",
            bound_generation=digest("generation", "one"),
            executable_identity=ArtifactIdentity(kind="Procedure", name="demo"),
            executable_digest=procedure_digest,
        ),
        producer_receipt_digest=receipt_digest,
        producer=ArtifactIdentity(kind="Procedure", name="demo"),
        producer_binding_digest=procedure_digest,
        observed_at=NOW,
        production_evidence=ProcedureEgressCaptureEvidenceV1(
            procedure_producer_receipt_digest=receipt_digest
        ),
    )
    payload = envelope.model_dump(mode="json")
    payload["production_evidence"]["implementation_digest"] = digest("implementation", "bad")

    with pytest.raises(ValidationError):
        CaptureEnvelopeV2.model_validate(payload)


def test_union_refuses_unknown_tags_and_noncanonical_wire(tmp_path: Path) -> None:
    fixture = provider_capture_fixture(tmp_path)
    built = build_provider_external_capture_v2(
        store=fixture.store,
        contract=fixture.contract,
        result=fixture.result,
        receipt=fixture.receipt,
        occurrence=fixture.occurrence,
        producer=fixture.producer,
        bound_generation=fixture.bound_generation,
    )
    payload = built.envelope.model_dump(mode="json")
    payload["tag"] = "playbill-capture-envelope-v3"

    with pytest.raises(CaptureFormatError):
        parse_capture_envelope(json.dumps(payload, sort_keys=True).encode())
    with pytest.raises(CaptureFormatError, match="canonical"):
        parse_capture_envelope(render_capture_envelope(built.envelope) + b"\n")
