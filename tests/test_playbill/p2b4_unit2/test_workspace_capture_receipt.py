from __future__ import annotations

import base64
import hashlib
from dataclasses import replace

import pytest
from tests.test_playbill.p2b4_unit1._support import digest, provider_capture_fixture

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.captures import (
    CaptureFormatError,
    ProviderProducerReceiptResolution,
    ProviderResultToExternalCaptureV1,
    build_provider_external_capture_v2,
    verify_capture,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.provider_execution import (
    ProviderEgressObservationV1,
    ProviderInvocationOutputDigestV1,
    ProviderSecretResolutionPlanV1,
    provider_invocation_output_digest,
)
from cruxible_client.contracts.workspace_file import SourceReadReceiptV1


def _workspace_fixture(tmp_path):  # type: ignore[no-untyped-def]
    fixture = provider_capture_fixture(tmp_path)
    local = fixture.occurrence.local_execution.model_copy(
        update={"interface_id": "workspace.file", "declared_endpoints": ()}
    )
    occurrence = fixture.occurrence.model_copy(
        update={
            "interface_id": "workspace.file",
            "local_execution": local,
            "secret_plan": ProviderSecretResolutionPlanV1(
                references=(), binding_identity_digests=()
            ),
        }
    )
    coordinate = AcceptedCoordinate(
        git_oid="a" * 64,
        semantic_root="sha256:" + "b" * 64,
        generation_root=fixture.bound_generation,
        compiler_digest="sha256:" + "c" * 64,
    )
    source_read = SourceReadReceiptV1(
        run_id=fixture.receipt.run_id,
        admission_binding_digest=fixture.receipt.admission_binding_digest,
        occurrence_path=fixture.receipt.occurrence_path,
        logical_source=fixture.result.source_identity,
        workspace_binding_digest=digest("workspace-binding", "root"),
        relative_path="docs/orders.json",
        bytes_digest=digest("workspace-bytes", "orders"),
        byte_length=12,
        policy_coordinate=coordinate,
        resolved_max_bytes=4096,
        derived_request_digest=digest("derived-request", "orders"),
        provider_input_digest=fixture.receipt.input_digest,
        read_at=fixture.result.observed_at,
    )
    provider_output = {
        "input_bucket": fixture.receipt.input_bucket,
        "source": {
            "logical_source": source_read.logical_source,
            "commitment_digest": source_read.derived_request_digest,
            "bytes_digest": source_read.bytes_digest,
            "byte_length": source_read.byte_length,
        },
        "content": {"kind": "text", "text": "orders"},
    }
    material = canonical_bytes(provider_output)
    result = ProviderResultToExternalCaptureV1(
        **{
            **fixture.result.model_dump(mode="python"),
            "content_base64": base64.b64encode(material).decode("ascii"),
            "byte_length": len(material),
            "bytes_digest": "sha256:" + hashlib.sha256(material).hexdigest(),
            "observed_at": source_read.read_at,
        }
    )
    egress = ProviderEgressObservationV1(observer_backend="sandbox", observer_grade="conformance")
    receipt = fixture.receipt.model_copy(
        update={
            "interface_id": "workspace.file",
            "output": ProviderInvocationOutputDigestV1(
                output_digest=provider_invocation_output_digest(provider_output)
            ).model_dump(mode="json"),
            "egress": egress,
            "secret_references": (),
        }
    )
    return (
        replace(
            fixture,
            occurrence=occurrence,
            receipt=receipt,
            result=result,
        ),
        source_read,
        provider_output,
    )


def test_workspace_capture_commits_and_replays_both_receipts(tmp_path) -> None:  # type: ignore[no-untyped-def]
    fixture, source_read, _output = _workspace_fixture(tmp_path)
    built = build_provider_external_capture_v2(
        store=fixture.store,
        contract=fixture.contract,
        result=fixture.result,
        receipt=fixture.receipt,
        occurrence=fixture.occurrence,
        producer=fixture.producer,
        bound_generation=fixture.bound_generation,
        source_read_receipt=source_read,
    )
    evidence = built.envelope.production_evidence
    assert evidence.source_read_receipt_digest is not None  # type: ignore[union-attr]
    assert (
        verify_capture(
            built.capture_digest,
            store=fixture.store,
            contract=fixture.contract,
            producer_artifact_digests={
                fixture.producer.qualified: fixture.receipt.provider_artifact_digest
            },
            producer_receipt_resolver=lambda _digest: ProviderProducerReceiptResolution(
                receipt=fixture.receipt,
                occurrence=fixture.occurrence,
                source_read_receipt=source_read,
            ),
        )
        == built.envelope
    )


def test_workspace_capture_forbids_missing_or_mixed_read_receipt(tmp_path) -> None:  # type: ignore[no-untyped-def]
    fixture, source_read, provider_output = _workspace_fixture(tmp_path)
    with pytest.raises(CaptureFormatError, match="requires exactly one"):
        build_provider_external_capture_v2(
            store=fixture.store,
            contract=fixture.contract,
            result=fixture.result,
            receipt=fixture.receipt,
            occurrence=fixture.occurrence,
            producer=fixture.producer,
            bound_generation=fixture.bound_generation,
        )
    mixed = source_read.model_copy(update={"bytes_digest": digest("other", "attempt")})
    with pytest.raises(CaptureFormatError, match="mixes source attempts"):
        build_provider_external_capture_v2(
            store=fixture.store,
            contract=fixture.contract,
            result=fixture.result,
            receipt=fixture.receipt,
            occurrence=fixture.occurrence,
            producer=fixture.producer,
            bound_generation=fixture.bound_generation,
            source_read_receipt=mixed,
        )
    assert provider_output["source"]["bytes_digest"] == source_read.bytes_digest  # type: ignore[index]


@pytest.mark.parametrize(
    "changed",
    (
        {"resolved_max_bytes": 4095},
        {
            "policy_coordinate": AcceptedCoordinate(
                git_oid="a" * 64,
                semantic_root="sha256:" + "b" * 64,
                generation_root="sha256:" + "9" * 64,
                compiler_digest="sha256:" + "c" * 64,
            )
        },
    ),
)
def test_workspace_capture_binds_admitted_policy_coordinate_and_cap(
    tmp_path, changed: dict[str, object]
) -> None:  # type: ignore[no-untyped-def]
    fixture, source_read, _output = _workspace_fixture(tmp_path)

    with pytest.raises(CaptureFormatError, match="does not correspond"):
        build_provider_external_capture_v2(
            store=fixture.store,
            contract=fixture.contract,
            result=fixture.result,
            receipt=fixture.receipt,
            occurrence=fixture.occurrence,
            producer=fixture.producer,
            bound_generation=fixture.bound_generation,
            source_read_receipt=source_read.model_copy(update=changed),
        )


def test_non_workspace_capture_forbids_source_read_receipt(tmp_path) -> None:  # type: ignore[no-untyped-def]
    fixture, source_read, _output = _workspace_fixture(tmp_path)
    ordinary = provider_capture_fixture(tmp_path / "ordinary")
    with pytest.raises(CaptureFormatError, match="requires exactly one"):
        build_provider_external_capture_v2(
            store=ordinary.store,
            contract=ordinary.contract,
            result=ordinary.result,
            receipt=ordinary.receipt,
            occurrence=ordinary.occurrence,
            producer=ordinary.producer,
            bound_generation=ordinary.bound_generation,
            source_read_receipt=source_read,
        )
