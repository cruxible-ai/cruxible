"""Open verified retained Captures through the shared bounded source reader."""

from __future__ import annotations

from cruxible_client.contracts.capture_reads import CaptureReadRequestV1, CaptureReadV1
from cruxible_client.contracts.captures import (
    CaptureContractV1,
    classify_capture_reuse,
    parse_capture_envelope,
    verify_capture,
)
from cruxible_client.contracts.errors import PlaybillError, PlaybillFormatError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.source_references import (
    CasSourceReferenceV1,
    LedgerSourceReferenceV1,
    OpenSourceRequestV1,
    SourceHandleV1,
)
from cruxible_core.errors import PermissionDeniedError
from cruxible_core.exhaust.producer_receipts import local_producer_receipt_resolver
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.claims.claims import service_open_playbill_source
from cruxible_core.storage.cas import BodyAccessContext


class CaptureReadInvalid(PlaybillFormatError):
    code = "playbill.capture.invalid"
    http_status = 400


class _LedgerResolver:
    def __init__(self, instance: PlaybillInstance) -> None:
        self.instance = instance

    def read_ledger_source(self, source: LedgerSourceReferenceV1) -> bytes:
        coordinate = self.instance.resolve_accepted_coordinate(
            **source.coordinate.model_dump(mode="python", exclude={"tag"})
        )
        content = self.instance.blob_at(coordinate.git_oid, source.address.artifact_path)
        if content is None:
            raise CaptureReadInvalid("Capture ledger source is unavailable")
        return content


def service_read_playbill_capture(
    instance: PlaybillInstance,
    *,
    request: CaptureReadRequestV1,
    access: BodyAccessContext,
) -> CaptureReadV1:
    # Envelopes also carry body-derived source/selector metadata. Authorize
    # before reading either them or their bodies, including for in-process callers.
    if not access.can_read_body:
        raise PermissionDeniedError("cruxible_playbill_body_read", "read_only", "governed_write")
    coordinate = (
        instance.accepted_coordinate()
        if request.at is None
        else instance.resolve_accepted_coordinate(
            **request.at.model_dump(mode="python", exclude={"tag"})
        )
    )
    public = AcceptedCoordinate.from_internal(coordinate)
    store = instance.body_store()
    if not store.metadata(request.capture_digest, access=access).present:
        return CaptureReadV1(
            capture_digest=request.capture_digest,
            coordinate=public,
            status="unavailable",
            reason="capture_unavailable",
        )
    try:
        envelope = parse_capture_envelope(store.read(request.capture_digest, access=access))
        with instance.bind_accepted_projection(coordinate) as projection:
            path = projection.citations.capture_contract_path(envelope.capture_contract_digest)
            if path is None:
                return CaptureReadV1(
                    capture_digest=request.capture_digest,
                    coordinate=public,
                    status="unavailable",
                    reason="contract_not_at_coordinate",
                )
            row = projection.typed.connection.execute(
                "SELECT identity FROM capture_contracts WHERE path=? AND artifact_digest=?",
                (path, envelope.capture_contract_digest),
            ).fetchone()
            if row is None:
                raise CaptureReadInvalid("CaptureContract index does not reproduce")
            contract = projection.typed.source(row[0])
            if not isinstance(contract, CaptureContractV1):
                raise CaptureReadInvalid("CaptureContract source has the wrong type")
            producers = {}
            for identity in {envelope.producer, envelope.run_coordinate.executable_identity}:
                owner = projection.typed.envelope(identity.qualified)
                if owner is not None:
                    # source() validates the indexed member against the accepted tree.
                    projection.typed.source(identity.qualified)
                    producers[identity.qualified] = owner.artifact_digest
        if (
            envelope.commitment.materialization == "cas"
            and not store.metadata(envelope.commitment.digest, access=access).present
        ):
            return CaptureReadV1(
                capture_digest=request.capture_digest,
                coordinate=public,
                status="unavailable",
                reason="body_unavailable",
            )
        envelope = verify_capture(
            request.capture_digest,
            store=store,
            contract=contract,
            ledger_resolver=_LedgerResolver(instance),
            producer_artifact_digests=producers,
            producer_receipt_resolver=local_producer_receipt_resolver(
                exhaust_root=instance.root / instance.descriptor.storage.exhaust,
                instance_id=instance.descriptor.instance_id,
                bodies=store,
            ),
        )
        # An external Capture can retain exact bytes locally. Open those bytes,
        # not the remote location; the original source remains in the envelope.
        source = (
            CasSourceReferenceV1(content_digest=envelope.commitment.digest)
            if envelope.commitment.materialization == "cas"
            else envelope.source
        )
        material_at = source.coordinate if isinstance(source, LedgerSourceReferenceV1) else public
        material = service_open_playbill_source(
            instance,
            request=OpenSourceRequestV1(
                source_handle=SourceHandleV1(
                    subject=SemanticAddress.whole_artifact(path),
                    at=material_at,
                    source=source,
                    commitment=envelope.commitment,
                    access_class="instance",
                ),
                resource_budget_bytes=min(request.max_bytes, contract.selection_budget.max_bytes),
            ),
            access=access,
            at=material_at,
        )
    except CaptureReadInvalid:
        raise
    except (PlaybillError, ValueError) as exc:
        raise CaptureReadInvalid(f"Capture verification failed: {exc}") from exc
    return CaptureReadV1(
        capture_digest=request.capture_digest,
        coordinate=public,
        status="verified",
        envelope=envelope,
        contract_address=path,
        epistemic_grade=contract.epistemic_grade,
        citation_role=(
            "evidence"
            if classify_capture_reuse(envelope, contract=contract, store=store, claim_id="")
            == "shareable"
            else "copy"
        ),
        material=material,
    )
