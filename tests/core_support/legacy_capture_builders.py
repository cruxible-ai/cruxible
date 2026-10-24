"""Frozen historical Capture fixtures; no production writer uses this profile."""

import hashlib
from datetime import datetime

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import CasDigest, Sha256Value, typed_digest
from cruxible_client.contracts.captures import (
    DIRECT_EXTERNAL_COORDINATE_TYPE,
    DIRECT_EXTERNAL_SELECTOR_TYPE,
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    DIRECT_SOURCE_IDENTITY,
    CaptureEnvelopeV1,
    CaptureFormatError,
    CaptureObjectStoreProtocol,
    CaptureRunCoordinateV1,
    DirectByteSpanSelection,
    DirectCaptureBuildResult,
    DirectClaimSource,
    DirectExternalSelection,
    _canonical_datetime,
    _direct_binding_digest,
    _direct_source_bytes,
    _store_capture_envelope,
    capture_contract_digest,
)
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.errors import CasError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.source_references import (
    CasSourceReference,
    EvidenceCommitment,
    ExternalSourceReference,
)


def build_direct_claim_capture(
    *,
    store: CaptureObjectStoreProtocol,
    actor_id: str,
    claim_id: str,
    value: object,
    rationale: str,
    observed_at: datetime,
    accepted_coordinate: AcceptedCoordinate,
    materialize_source: bool = True,
) -> DirectCaptureBuildResult:
    """Create one bounded self-asserted Capture from authenticated service inputs."""

    source = DirectClaimSource(
        authored_by=actor_id,
        claim_id=claim_id,
        value=value,
        rationale=rationale,
    )
    source_bytes = _direct_source_bytes(source)
    if len(source_bytes) > DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.selection_budget.max_bytes:
        raise CaptureFormatError("direct Claim source exceeds its accepted byte budget")
    source_digest = CasDigest(hashlib.sha256(source_bytes).hexdigest()).tagged
    contract_digest = capture_contract_digest(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT).tagged
    binding_digest = _direct_binding_digest(
        actor_id=actor_id,
        accepted_coordinate=accepted_coordinate,
    )
    receipt_digest = typed_digest(
        Sha256Value,
        "playbill-direct-capture-receipt-v1",
        {
            "actor_id": actor_id,
            "claim_id": claim_id,
            "observed_at": _canonical_datetime(observed_at),
            "source_digest": source_digest,
        },
    ).tagged
    if materialize_source:
        stored = store.store(source_bytes)
        if stored.digest != source_digest:
            raise CasError("direct source CAS digest did not reproduce")
        source_reference: CasSourceReference | ExternalSourceReference = CasSourceReference(
            content_digest=source_digest
        )
        commitment = EvidenceCommitment(
            digest_kind="exact_bytes",
            digest=source_digest,
            byte_length=len(source_bytes),
            materialization="cas",
        )
    else:
        source_reference = ExternalSourceReference(
            source_identity=DIRECT_SOURCE_IDENTITY,
            producer_binding_digest=binding_digest,
            coordinate_type="authenticated-request-v1",
            coordinate={
                "accepted_coordinate": accepted_coordinate.model_dump(mode="json"),
                "observed_at": _canonical_datetime(observed_at),
            },
            selector_type="direct-claim-source-v1",
            selector={"claim_id": claim_id},
            replayability="attested_only",
        )
        commitment = EvidenceCommitment(
            digest_kind="canonical_value",
            digest=source_digest,
            materialization="none",
        )
    envelope = CaptureEnvelopeV1(
        capture_contract_digest=contract_digest,
        source=source_reference,
        commitment=commitment,
        run_coordinate=CaptureRunCoordinateV1(
            run_kind="provider",
            run_id=f"direct:{claim_id.casefold()}",
            bound_generation=accepted_coordinate.generation_root,
            executable_identity=DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.identity,
            executable_digest=contract_digest,
        ),
        run_receipt_digest=receipt_digest,
        producer=ArtifactIdentity(kind="Principal", name=actor_id),
        producer_binding_digest=binding_digest,
        observed_at=observed_at,
    )
    return _store_capture_envelope(
        store=store,
        contract=DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
        envelope=envelope,
        source_body_digest=source_digest,
        source_body_materialized=materialize_source,
    )


def build_direct_claim_selection_capture(
    *,
    store: CaptureObjectStoreProtocol,
    actor_id: str,
    claim_id: str,
    rationale: str,
    observed_at: datetime,
    accepted_coordinate: AcceptedCoordinate,
    selection: DirectByteSpanSelection | DirectExternalSelection,
) -> DirectCaptureBuildResult:
    """Bind one exact span or typed external selector as self-asserted evidence.

    Both forms this builder accepts are content-addressed or unmaterialized, so
    neither names a logical source. A foreign-source selection does name one and
    is built by :func:`build_foreign_source_capture` under its own contract; it
    is refused here rather than being quietly signed under this one.
    """

    if not isinstance(selection, DirectByteSpanSelection | DirectExternalSelection):
        raise CaptureFormatError(
            "a logical-source selection is not admissible under the direct CaptureContract"
        )
    binding_digest = _direct_binding_digest(
        actor_id=actor_id,
        accepted_coordinate=accepted_coordinate,
    )
    contract_digest = capture_contract_digest(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT).tagged
    if isinstance(selection, DirectByteSpanSelection):
        if not store.verify(selection.span.content_digest):
            raise CaptureFormatError("selected span content is unavailable in CAS")
        source_bytes = store.read(
            selection.span.content_digest,
            access=BodyAccessContext(principal_id="playbill-service", can_read_body=True),
        )
        if selection.span.end_byte > len(source_bytes):
            raise CaptureFormatError("selected span exceeds its exact CAS body")
        source_reference: CasSourceReference | ExternalSourceReference = CasSourceReference(
            content_digest=selection.span.content_digest
        )
        commitment = EvidenceCommitment(
            digest_kind="exact_bytes",
            digest=selection.span.content_digest,
            byte_length=len(source_bytes),
            materialization="cas",
        )
        source_digest = selection.span.content_digest
        materialized = True
    else:
        source_reference = ExternalSourceReference(
            source_identity=DIRECT_SOURCE_IDENTITY,
            producer_binding_digest=binding_digest,
            coordinate_type=DIRECT_EXTERNAL_COORDINATE_TYPE,
            coordinate={
                "logical_source_identity": selection.logical_source_identity,
                "source_coordinate": selection.coordinate,
                "source_coordinate_type": selection.coordinate_type,
            },
            selector_type=DIRECT_EXTERNAL_SELECTOR_TYPE,
            selector={
                "claim_id": claim_id,
                "source_selector": selection.selector,
                "source_selector_type": selection.selector_type,
            },
            replayability="attested_only",
        )
        commitment = selection.commitment
        source_digest = selection.commitment.digest
        materialized = False
    receipt_digest = typed_digest(
        Sha256Value,
        "playbill-direct-selection-capture-receipt-v1",
        {
            "actor_id": actor_id,
            "claim_id": claim_id,
            "observed_at": _canonical_datetime(observed_at),
            "rationale": rationale,
            "selection": selection.model_dump(mode="json"),
        },
    ).tagged
    envelope = CaptureEnvelopeV1(
        capture_contract_digest=contract_digest,
        source=source_reference,
        commitment=commitment,
        run_coordinate=CaptureRunCoordinateV1(
            run_kind="provider",
            run_id=f"direct-selection:{claim_id.casefold()}",
            bound_generation=accepted_coordinate.generation_root,
            executable_identity=DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.identity,
            executable_digest=contract_digest,
        ),
        run_receipt_digest=receipt_digest,
        producer=ArtifactIdentity(kind="Principal", name=actor_id),
        producer_binding_digest=binding_digest,
        observed_at=observed_at,
    )
    return _store_capture_envelope(
        store=store,
        contract=DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
        envelope=envelope,
        source_body_digest=source_digest,
        source_body_materialized=materialized,
    )


def initialize_legacy_local(tmp_path, *, object_format="sha256", compiler=None):
    """A revision-35 world for tests constructing old stored contract/Claim pairs."""
    from unittest.mock import patch

    from cruxible_core.compiler.compiler import CLAIM_REBIND_COMPILER
    from tests.core_support._support import initialize_local

    with patch(
        "cruxible_core.runtime.instance.current_compiler_coordinate",
        return_value=compiler or CLAIM_REBIND_COMPILER,
    ):
        return initialize_local(tmp_path, object_format=object_format)
