"""Open verified retained Captures through the shared bounded source reader."""

from __future__ import annotations

from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Literal

from cruxible_client.contracts.capture_reads import CaptureRead, CaptureReadRequest
from cruxible_client.contracts.captures import (
    CaptureContract,
    CaptureEnvelopeAny,
    ProducerReceiptResolverProtocol,
    classify_capture_reuse,
    parse_capture_envelope,
    verify_capture,
)
from cruxible_client.contracts.errors import CruxibleError, FormatError, ReadRefusalError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.repairs import RepairOperation
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.source_references import (
    CasSourceReference,
    LedgerSourceReference,
    OpenSourceRequest,
    SourceHandle,
)
from cruxible_core.errors import PermissionDeniedError
from cruxible_core.evidence.capture_erasures import erased_body
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.claims.claims import service_open_playbill_source
from cruxible_core.storage.capture_metadata import capture_envelope
from cruxible_core.storage.cas import BodyAccessContext
from cruxible_core.storage.read_positions import PositionedBodies, positioned_read, read_body


class CaptureReadInvalid(FormatError):
    code = "cruxible.capture.invalid"
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
        repair = RepairOperation(operation="cruxible.get", arguments={"ref": claims[0]})
        line = (
            f"Read it with get on the Claim ({claims[0]}), which shows the value as text; "
            'detail="evidence" names the Captures behind it'
        )
    elif documents:
        what = f"the body of {documents[0]}"
        repair = RepairOperation(
            operation="cruxible.get", arguments={"ref": documents[0], "detail": "body"}
        )
        line = f'Read it with get on {documents[0]} with detail="body"'
    else:
        what = "stored bytes that are not a Capture envelope"
        repair = RepairOperation(operation="cruxible.orient")
        line = (
            'Pass a Capture\'s digest; get on a Claim with detail="proof" carries the full '
            "digests of the Captures behind it"
        )
    return ReadRefusalError(
        "cruxible.capture.not_a_capture",
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
    """A full Capture digest; a ``CAP-`` handle or digest prefix resolves as every verb does.

    ``resolve_capture_handle`` names the Capture: cited at the read coordinate,
    or retained and verifying there. An ambiguous, unknown or unboundedly
    crowded prefix refuses with the candidates and the repair.
    """

    from cruxible_core.service.discovery.operational import capture_hex

    hex_digits = capture_hex(value)
    if hex_digits is None or len(hex_digits) == 64:
        return value if hex_digits is None else "sha256:" + hex_digits
    resolution = resolve_capture_handle(instance, coordinate, hex_digits)
    if isinstance(resolution, CaptureHandleResolved):
        return resolution.digest
    if isinstance(resolution, CaptureHandleAmbiguous):
        matches = resolution.candidates
        raise ReadRefusalError(
            "cruxible.capture.ref_ambiguous",
            f"{value!r} is a prefix of {len(matches)} Captures",
            http_status=409,
            candidates=matches[:_MAX_PREFIX_CANDIDATES],
            repair=RepairOperation(
                operation="cruxible.capture.read", arguments={"capture_digest": matches[0]}
            ),
            repair_line="Pass one of them in full",
            context={"capture_digest": value},
        )
    if isinstance(resolution, CaptureHandleExhausted):
        raise capture_handle_exhausted(value, field="capture_digest")
    raise ReadRefusalError(
        "cruxible.capture.not_found",
        f"no Capture this instance holds has a digest starting with {hex_digits}",
        http_status=404,
        repair=RepairOperation(operation="cruxible.orient", arguments={"section": "captures"}),
        repair_line='Run orient(section="captures") to list them, or pass a full digest',
        context={"capture_digest": value},
    )


def capture_handle_exhausted(value: str, *, field: str) -> ReadRefusalError:
    """A read refusal for a handle the bounded lookup could not resolve uniquely."""

    return ReadRefusalError(
        "cruxible.capture.ref_scan_exhausted",
        f"{value!r} was not resolved: more Captures share its prefix than one lookup examines",
        http_status=409,
        repair=RepairOperation(operation="cruxible.orient", arguments={"section": "captures"}),
        repair_line="Pass a longer handle, or the full sha256 digest",
        context={field: value},
    )


class _LedgerResolver:
    def __init__(self, instance: PlaybillInstance) -> None:
        self.instance = instance

    def read_ledger_source(self, source: LedgerSourceReference) -> bytes:
        coordinate = self.instance.resolve_accepted_coordinate(
            **source.coordinate.model_dump(mode="python", exclude={"tag"})
        )
        content = self.instance.blob_at(coordinate.git_oid, source.address.artifact_path)
        if content is None:
            raise CaptureReadInvalid("Capture ledger source is unavailable")
        return content


CaptureUnavailableReason = Literal[
    "capture_unavailable", "contract_not_at_coordinate", "body_unavailable"
]
#: A ``body_unavailable`` read whose body an erasure record names reads ``body_erased``.


@dataclass(frozen=True)
class VerifiedCapture:
    """A Capture verified against its exact contract accepted at one coordinate."""

    envelope: CaptureEnvelopeAny
    contract: CaptureContract
    contract_address: str


def verify_accepted_capture(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    digest: str,
    *,
    access: BodyAccessContext,
    captures: ProducerReceiptResolverProtocol | None = None,
) -> VerifiedCapture | CaptureUnavailableReason:
    """Verify one retained Capture against the contract accepted at ``coordinate``.

    Answers why it is unavailable when its bytes, its contract at the coordinate
    or its committed body are missing; raises ``CaptureReadInvalid`` when it is
    present but does not verify, and ``cruxible.capture.not_a_capture`` when the
    bytes are not a Capture envelope at all. A Capture no accepted Claim cites at
    the coordinate is held to what a new citation is: daemon proof that a run
    produced it. ``captures`` is the caller's verification context, shared by
    every Capture one request verifies.
    """

    store = instance.body_store()
    if not PositionedBodies(store).verify(digest):
        return "capture_unavailable"
    try:
        raw = read_body(store, digest, access=access)
    except CruxibleError as exc:
        raise CaptureReadInvalid(f"Capture verification failed: {exc}") from exc
    try:
        envelope = parse_capture_envelope(raw)
    except (CruxibleError, ValueError):
        # The bytes are present and intact but are not a Capture envelope: say
        # what they are and which read answers them, not "verification failed".
        raise _not_a_capture(instance, coordinate, digest) from None
    try:
        with instance.bind_accepted_projection(coordinate) as projection:
            path = projection.citations.capture_contract_path(envelope.capture_contract_digest)
            if path is None:
                return "contract_not_at_coordinate"
            cited = (
                projection.typed.connection.execute(
                    "SELECT 1 FROM captures WHERE capture_digest=?", (digest,)
                ).fetchone()
                is not None
            )
            row = projection.typed.connection.execute(
                "SELECT identity FROM capture_contracts WHERE path=? AND artifact_digest=?",
                (path, envelope.capture_contract_digest),
            ).fetchone()
            if row is None:
                raise CaptureReadInvalid("CaptureContract index does not reproduce")
            contract = projection.typed.source(row[0])
            if not isinstance(contract, CaptureContract):
                raise CaptureReadInvalid("CaptureContract source has the wrong type")
            producers = {}
            for identity in {envelope.producer, envelope.run_coordinate.executable_identity}:
                owner = projection.typed.envelope(identity.qualified)
                if owner is not None:
                    # source() validates the indexed member against the accepted tree.
                    projection.typed.source(identity.qualified)
                    producers[identity.qualified] = owner.artifact_digest
        if envelope.commitment.materialization == "cas" and not PositionedBodies(store).verify(
            envelope.commitment.digest
        ):
            return "body_unavailable"
        envelope = verify_capture(
            digest,
            store=PositionedBodies(store),
            contract=contract,
            ledger_resolver=_LedgerResolver(instance),
            producer_artifact_digests=producers,
            producer_receipt_resolver=(
                instance.capture_verification_context() if captures is None else captures
            ),
            require_production_proof=not cited,
        )
    except CaptureReadInvalid:
        raise
    except (CruxibleError, ValueError) as exc:
        raise CaptureReadInvalid(f"Capture verification failed: {exc}") from exc
    return VerifiedCapture(envelope=envelope, contract=contract, contract_address=path)


@dataclass(frozen=True)
class RetainedCapture:
    """One Capture envelope the instance's body store holds, cited or not."""

    digest: str
    envelope: CaptureEnvelopeAny


@dataclass(frozen=True)
class RetainedCaptureInventory:
    """A bounded inventory of retained Captures; ``complete`` says it saw them all."""

    captures: tuple[RetainedCapture, ...]
    complete: bool
    nearest: tuple[str, ...] = ()


@positioned_read
def retained_captures(
    instance: PlaybillInstance,
    *,
    budget: int,
    hex_prefix: str = "",
    nearest: int = 0,
) -> RetainedCaptureInventory:
    """The Captures the instance holds whose digest starts with ``hex_prefix``.

    Cited or not: a Capture is retained as soon as it is stored. At most
    ``budget`` indexed envelope candidates are examined; if more match, the
    inventory is incomplete and callers refuse a partial answer. Unrelated
    bodies do not enter the lookup. Nothing here is verified against a contract;
    ``verify_accepted_capture`` verifies what a caller keeps.
    """

    store = instance.body_store()
    digests, complete = store.capture_envelopes.prefix(hex_prefix, limit=budget)
    found: list[RetainedCapture] = []
    for digest in digests:
        try:
            envelope = capture_envelope(store, digest, access=_INVENTORY_ACCESS)
        except (CruxibleError, ValueError):
            continue
        found.append(RetainedCapture(digest=digest, envelope=envelope))
    return RetainedCaptureInventory(
        captures=tuple(found), complete=complete, nearest=digests[:nearest]
    )


_INVENTORY_ACCESS = BodyAccessContext(principal_id="playbill-capture-inventory", can_read_body=True)


@positioned_read
def contract_captures(
    instance: PlaybillInstance,
    *,
    contract_digests: Collection[str],
    budget: int,
) -> RetainedCaptureInventory:
    """The retained Captures under these contract versions, by index, never by a CAS walk.

    Every Capture envelope the body store holds is indexed by the contract it
    names when it is stored: a run's Source acquisition or ``emit_capture``
    terminal, a self-asserted selection, cited or not. At most ``budget`` are
    read; past it the inventory is incomplete and says so. Nothing here is
    verified; citation verifies what a caller keeps, at its coordinate.
    """

    store = instance.body_store()
    digests, complete = store.capture_envelopes.captures(contract_digests, limit=budget)
    found: list[RetainedCapture] = []
    for digest in digests:
        try:
            envelope = capture_envelope(store, digest, access=_INVENTORY_ACCESS)
        except (CruxibleError, ValueError):
            continue
        found.append(RetainedCapture(digest=digest, envelope=envelope))
    return RetainedCaptureInventory(captures=tuple(found), complete=complete)


#: How many stored objects one handle lookup examines in its shard.
CAPTURE_HANDLE_SCAN_BUDGET = 65_536
#: How many retained Captures sharing a handle's prefix are verified before giving up.
CAPTURE_HANDLE_MAX_VERIFIED = 64


@dataclass(frozen=True)
class CaptureHandleResolved:
    """The handle names exactly one Capture."""

    digest: str


@dataclass(frozen=True)
class CaptureHandleAmbiguous:
    """The handle is a prefix of more than one Capture, in digest order."""

    candidates: tuple[str, ...]


@dataclass(frozen=True)
class CaptureHandleNotFound:
    """No Capture has the handle; ``nearest`` are stored digests the scan saw near it."""

    nearest: tuple[str, ...]


@dataclass(frozen=True)
class CaptureHandleExhausted:
    """The bounded lookup could not see every Capture under the handle, so it names none."""

    reason: Literal["scan_budget", "verify_limit"]


CaptureHandleResolution = (
    CaptureHandleResolved | CaptureHandleAmbiguous | CaptureHandleNotFound | CaptureHandleExhausted
)


@positioned_read
def resolve_capture_handle(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    hex_prefix: str,
    *,
    verified: Callable[[str], bool] | None = None,
    nearest: int = 0,
) -> CaptureHandleResolution:
    """Resolve a ``CAP-`` handle (a digest prefix) to one Capture, for every verb alike.

    A Capture at ``coordinate`` is one an accepted Claim cites there (the
    accepted index), or one the instance retains that verifies against its
    contract accepted there -- citing a Capture for the first time is the
    common write. ``get``, ``read_capture`` and the write verbs all resolve
    through here, so a handle one accepts the others open.

    The lookup is bounded. When the store holds more objects under the prefix
    than one lookup examines, or more retained Captures than it verifies, it
    answers ``CaptureHandleExhausted`` rather than call a partial answer unique.
    ``verified`` replaces the default verification (a caller's memo of the
    same check); ``nearest`` asks the scan for that many near digests.
    """

    from cruxible_core.service.discovery.operational import captures_with_prefix

    with instance.bind_accepted_projection(coordinate) as projection:
        cited = set(
            captures_with_prefix(
                projection.typed.connection, hex_prefix, limit=CAPTURE_HANDLE_MAX_VERIFIED + 1
            )
        )
    inventory = retained_captures(
        instance, budget=CAPTURE_HANDLE_SCAN_BUDGET, hex_prefix=hex_prefix, nearest=nearest
    )
    if not inventory.complete:
        return CaptureHandleExhausted("scan_budget")
    if len(inventory.captures) > CAPTURE_HANDLE_MAX_VERIFIED:
        return CaptureHandleExhausted("verify_limit")
    uncited = [item.digest for item in inventory.captures if item.digest not in cited]
    if verified is None:
        captures = instance.capture_verification_context()
    check = verified or (lambda digest: _verifies(instance, coordinate, digest, captures))
    found = sorted(cited | {digest for digest in uncited if check(digest)})
    if len(found) == 1:
        return CaptureHandleResolved(found[0])
    if found:
        return CaptureHandleAmbiguous(tuple(found))
    return CaptureHandleNotFound(inventory.nearest)


def _verifies(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    digest: str,
    captures: ProducerReceiptResolverProtocol,
) -> bool:
    """Whether a retained Capture verifies against its contract accepted at ``coordinate``."""

    try:
        verified = verify_accepted_capture(
            instance, coordinate, digest, access=_INVENTORY_ACCESS, captures=captures
        )
    except (CaptureReadInvalid, ReadRefusalError, CruxibleError, ValueError):
        return False
    return not isinstance(verified, str)


@positioned_read
def service_read_playbill_capture(
    instance: PlaybillInstance,
    *,
    request: CaptureReadRequest,
    access: BodyAccessContext,
) -> CaptureRead:
    # Envelopes also carry body-derived source/selector metadata. Authorize
    # before reading either them or their bodies, including for in-process callers.
    if not access.can_read_body:
        raise PermissionDeniedError("cruxible_body_read", "read_only", "governed_write")
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
    verified = verify_accepted_capture(instance, coordinate, request.capture_digest, access=access)
    if isinstance(verified, str):
        erasure = None
        if verified == "body_unavailable":
            erasure = erased_body(
                instance,
                capture_envelope(instance.body_store(), request.capture_digest, access=access),
            )
        return CaptureRead(
            capture_digest=request.capture_digest,
            coordinate=public,
            status="unavailable",
            reason=verified if erasure is None else "body_erased",
            erasure=erasure,
        )
    store = instance.body_store()
    envelope, contract, path = verified.envelope, verified.contract, verified.contract_address
    try:
        # An external Capture can retain exact bytes locally. Open those bytes,
        # not the remote location; the original source remains in the envelope.
        source = (
            CasSourceReference(content_digest=envelope.commitment.digest)
            if envelope.commitment.materialization == "cas"
            else envelope.source
        )
        material_at = source.coordinate if isinstance(source, LedgerSourceReference) else public
        material = service_open_playbill_source(
            instance,
            request=OpenSourceRequest(
                source_handle=SourceHandle(
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
    except (CruxibleError, ValueError) as exc:
        raise CaptureReadInvalid(f"Capture verification failed: {exc}") from exc
    return CaptureRead(
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
