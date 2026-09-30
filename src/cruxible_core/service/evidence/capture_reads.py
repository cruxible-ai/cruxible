"""Open verified retained Captures through the shared bounded source reader."""

from __future__ import annotations

from cruxible_client.contracts.capture_reads import CaptureReadRequestV1, CaptureReadV1
from cruxible_client.contracts.captures import (
    CaptureContractV1,
    classify_capture_reuse,
    parse_capture_envelope,
    verify_capture,
)
from cruxible_client.contracts.errors import PlaybillError, PlaybillFormatError, ReadRefusalError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.repairs import RepairOperationV1
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.source_references import (
    CasSourceReferenceV1,
    LedgerSourceReferenceV1,
    OpenSourceRequestV1,
    SourceHandleV1,
)
from cruxible_core.errors import PermissionDeniedError
from cruxible_core.exhaust.producer_receipts import local_producer_receipt_resolver
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.claims.claims import service_open_playbill_source
from cruxible_core.storage.cas import BodyAccessContext


class CaptureReadInvalid(PlaybillFormatError):
    code = "playbill.capture.invalid"
    http_status = 400


_MAX_OWNERS = 10


def _not_a_capture(
    instance: PlaybillInstance, coordinate: AcceptedProjectionCoordinate, digest: str
) -> ReadRefusalError:
    """Refuse a digest whose bytes are not a Capture envelope, naming what they are.

    A digest an agent sees is usually a Capture's, but an exact-content Claim's
    value and a Document's body are digests of stored bytes too. Those bytes are
    read through ``get`` on their owner, which shows them as text; the Captures
    that back a Claim are listed by ``get(<claim>, detail="evidence")``.
    """

    with instance.bind_accepted_projection(coordinate) as projection:
        connection = projection.typed.connection
        claims = [
            str(identity).removeprefix("Claim:")
            for (identity,) in connection.execute(
                "SELECT identity FROM claims WHERE object_content_digest=? AND lifecycle='live' "
                "ORDER BY identity LIMIT ?",
                (digest, _MAX_OWNERS),
            )
        ]
        documents = [
            "Document:" + str(identity).removeprefix("document:")
            for (identity,) in connection.execute(
                "SELECT identity FROM documents WHERE body_digest=? ORDER BY identity LIMIT ?",
                (digest, _MAX_OWNERS),
            )
        ]
    owners = [*claims, *documents][:_MAX_OWNERS]
    if claims:
        what = f"the exact content of Claim {claims[0]}"
        repair = RepairOperationV1(operation="playbill.get", arguments={"ref": claims[0]})
        line = (
            f"Read it with get on the Claim ({claims[0]}), which shows the value as text; "
            'detail="evidence" names the Captures behind it'
        )
    elif documents:
        what = f"the body of {documents[0]}"
        repair = RepairOperationV1(
            operation="playbill.get", arguments={"ref": documents[0], "detail": "body"}
        )
        line = f'Read it with get on {documents[0]} with detail="body"'
    else:
        what = "stored bytes that are not a Capture envelope"
        repair = RepairOperationV1(operation="playbill.orient")
        line = (
            'Pass a Capture\'s digest; get on a Claim with detail="proof" carries the full '
            "digests of the Captures behind it"
        )
    return ReadRefusalError(
        "playbill.capture.not_a_capture",
        f"{digest} is not a Capture: it is {what}",
        http_status=404,
        candidates=owners,
        repair=repair,
        repair_line=line,
        context={"capture_digest": digest},
    )


_MAX_PREFIX_CANDIDATES = 10


def _full_capture_digest(
    instance: PlaybillInstance, coordinate: AcceptedProjectionCoordinate, value: str
) -> str:
    """A full Capture digest; a ``CAP-`` handle or digest prefix resolves among accepted Captures.

    A prefix must name exactly one Capture accepted Claims cite at the read
    coordinate; an ambiguous or unknown one refuses with the candidates and the
    orient section that lists Captures.
    """

    from cruxible_core.service.discovery.operational import capture_hex, captures_with_prefix

    hex_digits = capture_hex(value)
    if hex_digits is None or len(hex_digits) == 64:
        return value if hex_digits is None else "sha256:" + hex_digits
    with instance.bind_accepted_projection(coordinate) as projection:
        matches = captures_with_prefix(
            projection.typed.connection, hex_digits, limit=_MAX_PREFIX_CANDIDATES + 1
        )
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise ReadRefusalError(
            "playbill.capture.ref_ambiguous",
            f"{value!r} is a prefix of {len(matches)} accepted Captures",
            http_status=409,
            candidates=matches[:_MAX_PREFIX_CANDIDATES],
            repair=RepairOperationV1(
                operation="playbill.capture.read", arguments={"capture_digest": matches[0]}
            ),
            repair_line="Pass one of them in full",
            context={"capture_digest": value},
        )
    raise ReadRefusalError(
        "playbill.capture.not_found",
        f"no accepted Capture has a digest starting with {hex_digits}",
        http_status=404,
        repair=RepairOperationV1(operation="playbill.orient", arguments={"section": "captures"}),
        repair_line='Run orient(section="captures") to list them, or pass a full digest',
        context={"capture_digest": value},
    )


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
    request = request.model_copy(
        update={
            "capture_digest": _full_capture_digest(instance, coordinate, request.capture_digest)
        }
    )
    store = instance.body_store()
    if not store.metadata(request.capture_digest, access=access).present:
        return CaptureReadV1(
            capture_digest=request.capture_digest,
            coordinate=public,
            status="unavailable",
            reason="capture_unavailable",
        )
    try:
        raw = store.read(request.capture_digest, access=access)
    except PlaybillError as exc:
        raise CaptureReadInvalid(f"Capture verification failed: {exc}") from exc
    try:
        envelope = parse_capture_envelope(raw)
    except (PlaybillError, ValueError):
        # The bytes are present and intact but are not a Capture envelope: say
        # what they are and which read answers them, not "verification failed".
        raise _not_a_capture(instance, coordinate, request.capture_digest) from None
    try:
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
