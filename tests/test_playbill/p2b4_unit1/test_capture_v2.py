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
    CaptureRunCoordinateV2,
    ProcedureEgressCaptureEvidenceV1,
    ProviderInvocationCaptureEvidenceV1,
    ProviderProducerReceiptResolution,
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

EXPECTED_V1_CAPTURE_DIGEST = (
    "sha256:e872ed81e6058cf9a01138a0c6a9b8c02e01d30b0e85a6014d12ef9e56718431"
)
EXPECTED_V1_CAPTURE_WIRE = (
    b'{"capture_contract_digest":"sha256:cdf1d3ce82226ab844129b8600d1ccbf6407643ad79cecf82140d57b3d657c7b",'
    b'"commitment":{"byte_length":21,"digest":"sha256:322edaca0159df463b0d033e40dcbb8b0812c2f47852e0dc07fd675d24f2dfc3",'
    b'"digest_kind":"exact_bytes","materialization":"cas","tag":"playbill-evidence-commitment-v1"},'
    b'"input_receipt_set_manifest_digest":null,"observed_at":"2026-08-16T12:00:00Z",'
    b'"producer":{"kind":"Provider","name":"acme.orders"},'
    b'"producer_binding_digest":"sha256:6b92be85e21db00a71faac1c6c9c27a9b8e1e891b9c71aa37404e70c1c1392dd",'
    b'"reducer_digest":null,"run_coordinate":{"bound_generation":"sha256:9278319bfbb60518c408efdc93887d796cf96cfcd340421167b92a929cdb09a6",'
    b'"executable_digest":"sha256:5b99cfbe85166c0740b985efaf5b8d755786a1e6f0144327a3df92b03d5815e5",'
    b'"executable_identity":{"kind":"Provider","name":"acme.orders"},'
    b'"run_id":"provider-run-1","run_kind":"provider","tag":"playbill-capture-run-coordinate-v1"},'
    b'"run_receipt_digest":"sha256:2d9fde0432f8a085a798f58302678f3fa31dc8c4d2b7c3c7e4d4e9801ea54007",'
    b'"source":{"content_digest":"sha256:322edaca0159df463b0d033e40dcbb8b0812c2f47852e0dc07fd675d24f2dfc3",'
    b'"kind":"cas","tag":"playbill-cas-source-reference-v1"},'
    b'"source_effective_time":null,"tag":"playbill-capture-envelope-v1"}'
)


def test_v1_bytes_and_digest_are_unchanged_under_the_exact_tag_union(tmp_path: Path) -> None:
    contract = capture_contract()
    provider_artifact = provider(contract)
    store = body_store(tmp_path)
    built = build_cas_capture(
        store=store,
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

    assert wire == EXPECTED_V1_CAPTURE_WIRE
    assert built.capture_digest == EXPECTED_V1_CAPTURE_DIGEST
    assert isinstance(parse_capture_envelope(wire), CaptureEnvelopeV1)
    assert capture_digest(parse_capture_envelope(wire)).tagged == built.capture_digest
    assert (
        verify_capture(
            built.capture_digest,
            store=store,
            contract=contract,
            producer_artifact_digests={
                provider_artifact.identity.qualified: provider_digest(provider_artifact).tagged
            },
        )
        == built.envelope
    )


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
            producer_receipt_resolver=lambda value: (
                ProviderProducerReceiptResolution(
                    receipt=fixture.receipt,
                    occurrence=fixture.occurrence,
                )
                if value == built.envelope.producer_receipt_digest
                else None
            ),
        )
        == built.envelope
    )


@pytest.mark.parametrize(
    "field",
    [
        "interface_artifact_digest",
        "interface_id",
        "interface_digest",
        "implementation_digest",
        "materialization_digest",
        "input_bucket",
        "provider_artifact_digest",
        "external_occurrence_plan_digest",
        "invocation_receipt_digest",
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
    if field == "interface_id":
        replacement = "mutated.interface"
    elif field == "input_bucket":
        replacement = "kind=mutated"
    elif field == "secret_references":
        replacement = ()
    elif field == "egress":
        replacement = evidence.egress.model_copy(update={"observed_endpoints": ()})
    mutated = built.envelope.model_copy(
        update={"production_evidence": evidence.model_copy(update={field: replacement})}
    )
    mutated_digest = capture_digest(mutated).tagged
    fixture.store.store(render_capture_envelope(mutated))
    with pytest.raises(CaptureFormatError, match=field):
        verify_capture(
            mutated_digest,
            store=fixture.store,
            contract=fixture.contract,
            producer_artifact_digests={
                fixture.producer.qualified: fixture.receipt.provider_artifact_digest
            },
            producer_receipt_resolver=lambda value: (
                ProviderProducerReceiptResolution(
                    receipt=fixture.receipt,
                    occurrence=fixture.occurrence,
                )
                if value == built.envelope.producer_receipt_digest
                else None
            ),
        )


@pytest.mark.parametrize("resolver", [None, lambda _digest: None])
def test_v2_verification_refuses_and_names_an_unresolved_producer_receipt(
    tmp_path: Path,
    resolver,  # type: ignore[no-untyped-def]
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

    with pytest.raises(
        CaptureFormatError,
        match=built.envelope.producer_receipt_digest,
    ):
        verify_capture(
            built.capture_digest,
            store=fixture.store,
            contract=fixture.contract,
            producer_artifact_digests={
                fixture.producer.qualified: fixture.receipt.provider_artifact_digest
            },
            producer_receipt_resolver=resolver,
        )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("allowed_source_kinds", ("cas",)),
        ("allowed_materialization_modes", ("external",)),
    ],
)
def test_provider_builder_refuses_a_contract_that_cannot_verify_its_capture(
    tmp_path: Path,
    field: str,
    replacement: tuple[str, ...],
) -> None:
    fixture = provider_capture_fixture(tmp_path)
    contract = fixture.contract.model_copy(update={field: replacement})

    with pytest.raises(CaptureFormatError, match="does not permit"):
        build_provider_external_capture_v2(
            store=fixture.store,
            contract=contract,
            result=fixture.result,
            receipt=fixture.receipt,
            occurrence=fixture.occurrence,
            producer=fixture.producer,
            bound_generation=fixture.bound_generation,
        )


def test_v1_and_v2_run_id_grammars_are_disjoint_successors() -> None:
    common = {
        "run_kind": "provider",
        "run_id": "RUN-" + "0" * 64,
        "bound_generation": digest("generation", "grammar"),
        "executable_identity": ArtifactIdentity(kind="Provider", name="grammar"),
        "executable_digest": digest("provider", "grammar"),
    }

    with pytest.raises(ValidationError, match="run_id"):
        CaptureRunCoordinateV1.model_validate(common)
    assert CaptureRunCoordinateV2.model_validate(common).run_id == common["run_id"]


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
        run_coordinate=CaptureRunCoordinateV2(
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
