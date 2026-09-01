"""PC-A2 locator-free source-reference and commitment contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from cruxible_client.contracts.semantic import ContentSpan, SemanticAddress
from cruxible_client.contracts.source_references import (
    CasSourceReferenceV1,
    EvidenceCommitmentV1,
    ExternalSourceReferenceV1,
    LedgerSourceReferenceV1,
    SemanticReadCoordinateV1,
    SourceHandleV1,
    SourceSchemaRegistry,
    source_handle_digest,
    validate_local_read_coordinate,
    validate_source_commitment,
)
from cruxible_core.playbill.projection import AcceptedCoordinate

DIGEST_A = "sha256:" + "11" * 32
DIGEST_B = "sha256:" + "22" * 32


def accepted_coordinate() -> AcceptedCoordinate:
    return AcceptedCoordinate(
        git_oid="a" * 40,
        semantic_root="sha256:" + "33" * 32,
        generation_root="sha256:" + "44" * 32,
        compiler_digest="sha256:" + "55" * 32,
    )


def external_reference(
    *,
    replayability: str = "exact",
    selector: object | None = None,
) -> ExternalSourceReferenceV1:
    return ExternalSourceReferenceV1.model_validate(
        {
            "source_identity": "commerce.production.orders",
            "producer_binding_digest": DIGEST_B,
            "coordinate_type": "postgres-lsn-v1",
            "coordinate": "0/16B6C50",
            "selector_type": "relation-primary-key-v1",
            "selector": (
                {"key": {"order_id": "ord-482"}, "relation": "orders"}
                if selector is None
                else selector
            ),
            "replayability": replayability,
        }
    )


def test_ledger_cas_external_references_and_handle_match_frozen_golden() -> None:
    fixture_path = Path(__file__).parents[1] / "goldens" / "playbill" / "source-reference-v1.json"
    fixture = json.loads(fixture_path.read_bytes())
    ledger = LedgerSourceReferenceV1.model_validate(fixture["ledger_reference"])
    cas = CasSourceReferenceV1.model_validate(fixture["cas_reference"])
    external = ExternalSourceReferenceV1.model_validate(fixture["external_reference"])
    handle = SourceHandleV1.model_validate(fixture["source_handle"])

    assert ledger.coordinate == accepted_coordinate()
    assert cas.content_digest == DIGEST_A
    assert external.source_identity == "commerce.production.orders"
    assert source_handle_digest(handle) == fixture["source_handle_digest"]


def test_source_handle_spans_require_exact_bytes_and_stay_within_commitment() -> None:
    source = CasSourceReferenceV1(content_digest=DIGEST_A)
    exact = EvidenceCommitmentV1(
        digest_kind="exact_bytes",
        digest=DIGEST_A,
        byte_length=10,
        materialization="cas",
    )
    handle = SourceHandleV1(
        subject=SemanticAddress.whole_artifact("documents/design.json"),
        at=accepted_coordinate(),
        source=source,
        commitment=exact,
        exact_spans=(ContentSpan(content_digest=DIGEST_A, start_byte=1, end_byte=5),),
        access_class="instance",
    )
    assert handle.exact_spans[0].end_byte == 5

    with pytest.raises(ValidationError, match="exceeds"):
        SourceHandleV1(
            **handle.model_dump(exclude={"exact_spans"}),
            exact_spans=(ContentSpan(content_digest=DIGEST_A, start_byte=1, end_byte=11),),
        )
    with pytest.raises(ValidationError, match="exact_bytes"):
        SourceHandleV1(
            **handle.model_dump(exclude={"commitment", "exact_spans"}),
            commitment=EvidenceCommitmentV1(
                digest_kind="canonical_value",
                digest=DIGEST_A,
                materialization="cas",
            ),
            exact_spans=(ContentSpan(content_digest=DIGEST_A, start_byte=0, end_byte=1),),
        )


def test_materialization_and_replayability_correspondence_refuses_fail_closed() -> None:
    exact_external = external_reference()
    validate_source_commitment(
        exact_external,
        EvidenceCommitmentV1(
            digest_kind="canonical_value",
            digest=DIGEST_A,
            materialization="external",
        ),
    )
    with pytest.raises(ValueError, match="exact replayability"):
        validate_source_commitment(
            external_reference(replayability="attested_only"),
            EvidenceCommitmentV1(
                digest_kind="canonical_value",
                digest=DIGEST_A,
                materialization="external",
            ),
        )
    with pytest.raises(ValueError, match="attested-only"):
        validate_source_commitment(
            exact_external,
            EvidenceCommitmentV1(
                digest_kind="provider_statement",
                digest=DIGEST_A,
                materialization="none",
            ),
        )
    with pytest.raises(ValidationError, match="byte_length"):
        EvidenceCommitmentV1(
            digest_kind="canonical_value",
            digest=DIGEST_A,
            byte_length=4,
            materialization="cas",
        )


def test_external_source_ref_refuses_secret_locators_and_unregistered_schemas() -> None:
    with pytest.raises(ValidationError, match="secret-bearing"):
        external_reference(selector={"host": "db.internal", "relation": "orders"})
    with pytest.raises(ValidationError, match="locator"):
        external_reference(selector={"url": "https://db.example/orders/482"})

    registry = SourceSchemaRegistry(
        coordinate_types=("postgres-lsn-v1",),
        selector_types=("relation-primary-key-v1",),
    )
    registry.require(external_reference())
    unknown = external_reference().model_copy(update={"selector_type": "arbitrary-python-v1"})
    with pytest.raises(ValueError, match="unregistered external selector"):
        registry.require(unknown)


def test_semantic_read_coordinate_union_is_discriminator_led_and_remote_refuses() -> None:
    adapter = TypeAdapter(SemanticReadCoordinateV1)
    assert adapter.validate_python(accepted_coordinate().model_dump(mode="json")) == (
        accepted_coordinate()
    )
    with pytest.raises(ValidationError):
        adapter.validate_python({"tag": "playbill-remote-latest-coordinate-v1"})

    foreign = accepted_coordinate().model_copy(update={"semantic_root": "sha256:" + "66" * 32})
    with pytest.raises(ValueError, match="remote or unverified"):
        validate_local_read_coordinate(foreign, expected_accepted=accepted_coordinate())
