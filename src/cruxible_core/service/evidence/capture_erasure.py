"""Erase one retained Capture's body under the authority its contract grants.

The authority is governed state: the Capture's CaptureContract declares
``erasure: authorized_by_rule`` and pins the registered erasure rule. Accepting
that contract went through the instance's approval policy, and the declaration
is fixed for the contract's identity (succession keeps it equal), so the erase
itself is an operational act under it rather than another proposal: it cannot
make any Claim better supported, only less replayable, and every read shows
what was erased, by whom and why.

What is destroyed is the Capture's retained body, the objects replay reads: a
CAS-materialized commitment and a CAS source. The envelope (the evidence
record), its digest, the contract, the landing records and the erasure record
stay. A body is content-addressed, so the same bytes held by any other Capture
go with it, and reads of that Capture show the same erasure.

Refused, with the repair: a contract that does not authorize erasure; bytes
that live in the ledger (a ledger source or ledger materialization), which
erasing would mean rewriting accepted history and every mirror of it; a
Capture with no retained body; and a body that is also a citing Claim's exact
content, which accepted state must keep.

Cost: the Capture's own envelope and bodies, and the live Claims citing it
(one indexed range) with each one's citations. Nothing walks the instance.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from cruxible_client.contracts.capture_erasure import (
    CAPTURE_ERASURE_MAX_LISTED_CLAIMS,
    CaptureEraseRefusalCode,
    CaptureEraseRefusedError,
    CaptureEraseRequest,
    CaptureEraseResult,
    CaptureErasureRecord,
)
from cruxible_client.contracts.captures import (
    CAPTURE_COMPONENTS,
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
    CaptureContract,
    CaptureEnvelopeAny,
    capture_contract_digest,
    foreign_source_capture_contract,
    parse_capture_contract,
)
from cruxible_client.contracts.change_control import StateCoordinate
from cruxible_client.contracts.errors import CruxibleError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.repairs import (
    HandEditInstruction,
    HandEditRepair,
    RepairOperation,
    ServedRepair,
)
from cruxible_client.contracts.source_references import LedgerSourceReference
from cruxible_core.evidence.capture_erasures import (
    CaptureErasureStore,
    citing_live_claims,
    claims_left_without_evidence,
    retained_body_digests,
)
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.evidence.capture_reads import _full_capture_digest
from cruxible_core.storage.capture_metadata import capture_envelope
from cruxible_core.storage.cas import BodyAccessContext
from cruxible_core.storage.read_positions import body_present

_ACCESS = BodyAccessContext(principal_id="capture-erasure", can_read_body=True)
_BUILT_IN_CONTRACTS = {
    capture_contract_digest(contract).tagged: contract
    for contract in (
        DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
        COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    )
}


def _refuse(
    code: CaptureEraseRefusalCode,
    message: str,
    *,
    repair_line: str,
    digest: str,
    repair: ServedRepair | None = None,
) -> CaptureEraseRefusedError:
    """One erase refusal; the repair defaults to reading the Capture's card."""

    return CaptureEraseRefusedError(
        code,
        message,
        capture_digest=digest,
        repair=repair or RepairOperation(operation="cruxible.get", arguments={"ref": digest}),
        repair_line=repair_line,
    )


_PROHIBITED_REPAIR = (
    "A contract's erasure policy is fixed for its identity, so this body stays. For sources "
    "that may capture secrets, author a successor contract under a new name built with "
    "authorize_erasure(contract) (cruxible_client.contracts.captures), and capture under it"
)


def accepted_capture_contract(
    instance: PlaybillInstance,
    projection: Any,
    *,
    git_oid: str,
    envelope: CaptureEnvelopeAny,
) -> CaptureContract | None:
    """The exact contract version a Capture names, as accepted state holds it."""

    digest = envelope.capture_contract_digest
    contract: CaptureContract | None = _BUILT_IN_CONTRACTS.get(digest)
    if contract is not None:
        return contract
    path = projection.citations.capture_contract_path(digest)
    if path is not None:
        raw = instance.blob_at(git_oid, path)
        if raw is not None:
            parsed = parse_capture_contract(raw, path=path)
            if capture_contract_digest(parsed).tagged == digest:
                return parsed
    source = getattr(envelope.source, "source_identity", None)
    if isinstance(source, str):
        try:
            foreign = foreign_source_capture_contract(source)
        except (CruxibleError, ValueError):
            return None
        if capture_contract_digest(foreign).tagged == digest:
            return foreign
    return None


def _erasure_rule(contract: CaptureContract) -> str | None:
    """The registered erasure rule the contract pins, when it authorizes erasure."""

    policy = contract.retention_erasure_policy
    if policy.erasure != "authorized_by_rule" or policy.erasure_rule_digest is None:
        return None
    pinned = any(
        pin.role == "erasure-rule"
        and pin.artifact_digest == policy.erasure_rule_digest
        and CAPTURE_COMPONENTS.resolves(pin)
        for pin in contract.pins
    )
    return policy.erasure_rule_digest if pinned else None


def service_erase_capture(
    instance: PlaybillInstance,
    *,
    request: CaptureEraseRequest,
    actor_id: str,
    previewing: bool,
    observe: Callable[[StateCoordinate], None],
) -> CaptureEraseResult:
    """Erase (or, previewing, check erasing) one Capture's retained body.

    ``observe`` receives the Capture's erasure state read where the erase
    writes: a preview reports it as its coordinate, and a commit carrying ``at``
    refuses there if it moved. Every check runs under the accepted head held
    still, so what authorized the erase is what the head says while it lands.
    """

    store = CaptureErasureStore(instance)
    with instance.holding_accepted_head() as main:
        head = instance.accepted_coordinate()
        if head.git_oid != main:
            raise _refuse(
                "cruxible.capture.erasure_head_moved",
                "accepted state moved past this instance's verified head",
                repair_line="Run the erase again; it is judged at the new head",
                digest=request.capture_digest,
                repair=RepairOperation(
                    operation="cruxible.capture.erase",
                    arguments={"capture_digest": request.capture_digest},
                ),
            )
        digest = _full_capture_digest(instance, head, request.capture_digest)
        envelope, contract, bodies, citing, left = _checked(
            instance, head, digest, store=store, git_oid=main
        )
        bodies_state = {body: bool(body_present(instance.body_store(), body)) for body in bodies}
        existing = store.for_capture(digest)
        coordinate = StateCoordinate.of(
            f"capture_erasure:{digest}",
            {"bodies": bodies_state, "recorded": existing is not None},
        )
        observe(coordinate)
        already = existing is not None and not any(bodies_state.values())
        if not any(bodies_state.values()) and existing is None:
            raise _refuse(
                "cruxible.capture.erasure_nothing_retained",
                f"Capture {digest} holds no body to erase: the store no longer has it",
                repair_line=(
                    "Nothing is retained, so nothing can be erased; get the Capture to see "
                    "its status"
                ),
                digest=digest,
            )
        rule = _erasure_rule(contract)
        assert rule is not None  # _checked refused otherwise
        # An unfinished erasure (its bytes never landed again) is retried under its
        # record; bytes stored again since are erased anew, under a record of their own.
        resume = None if existing is None else store.unfinished(digest)
        draft = resume or CaptureErasureRecord(
            capture_digest=digest,
            capture_contract=contract.identity.qualified,
            capture_contract_digest=envelope.capture_contract_digest,
            erasure_rule_digest=rule,
            body_digests=bodies,
            erased_by=actor_id,
            erased_at=instance.recording_clock(),
            because=request.because,
        )
        status: Literal["erased", "would_erase", "already_erased"] = (
            "already_erased" if already else "would_erase" if previewing else "erased"
        )
        record = existing if already and existing is not None else draft
        if not previewing and not already:
            # The record lands first: an interrupted erase leaves it and the
            # bytes, and the retry finishes the unlink under the same record.
            record = store.record(
                draft, actor_id=actor_id, coordinate=AcceptedCoordinate.from_internal(head)
            )
            disk = instance.body_store()
            for body in record.body_digests:
                disk.erase(body)
    listed = tuple(
        item.removeprefix("Claim:") for item in citing[:CAPTURE_ERASURE_MAX_LISTED_CLAIMS]
    )
    return CaptureEraseResult(
        status=status,
        erasure=record,
        citing_claims=listed,
        citing_claim_count=len(citing),
        left_without_evidence=tuple(
            item.removeprefix("Claim:") for item in left[:CAPTURE_ERASURE_MAX_LISTED_CLAIMS]
        ),
        coordinate=coordinate,
    )


def _checked(
    instance: PlaybillInstance,
    head: AcceptedProjectionCoordinate,
    digest: str,
    *,
    store: CaptureErasureStore,
    git_oid: str,
) -> tuple[CaptureEnvelopeAny, CaptureContract, tuple[str, ...], list[str], list[str]]:
    """The Capture's envelope, authorizing contract, bodies and citing Claims, or a refusal."""

    try:
        envelope = capture_envelope(instance.body_store(), digest, access=_ACCESS)
    except (CruxibleError, ValueError) as exc:
        raise _refuse(
            "cruxible.capture.erasure_unavailable",
            f"Capture {digest} has no retained envelope to erase under: {exc}",
            repair_line="Pass the digest of a Capture this instance holds",
            digest=digest,
        ) from None
    with instance.bind_accepted_projection(head) as projection:
        connection = projection.typed.connection
        contract = accepted_capture_contract(
            instance, projection, git_oid=git_oid, envelope=envelope
        )
        if contract is None:
            raise _refuse(
                "cruxible.capture.erasure_contract_unavailable",
                f"Capture {digest} names CaptureContract version "
                f"{envelope.capture_contract_digest}, which accepted state does not hold",
                repair_line="Erasure is authorized by the Capture's accepted contract; "
                "accept that contract version first",
                digest=digest,
            )
        if _erasure_rule(contract) is None:
            raise _refuse(
                "cruxible.capture.erasure_prohibited",
                f"{contract.identity.qualified} does not authorize erasing its Captures",
                repair_line=_PROHIBITED_REPAIR,
                digest=digest,
                repair=HandEditRepair(
                    hand_edit=HandEditInstruction(
                        target=contract.identity.qualified,
                        required_change=_PROHIBITED_REPAIR,
                    )
                ),
            )
        if envelope.commitment.materialization == "ledger" or isinstance(
            envelope.source, LedgerSourceReference
        ):
            raise _refuse(
                "cruxible.capture.erasure_ledger_material",
                f"Capture {digest} reads its bytes from the ledger, which keeps them in "
                "accepted history and every mirror",
                repair_line="Erasure removes retained bodies only; bytes in the ledger stay",
                digest=digest,
            )
        bodies = retained_body_digests(envelope)
        if not bodies:
            raise _refuse(
                "cruxible.capture.erasure_nothing_retained",
                f"Capture {digest} retains no body: it commits to bytes it never stored",
                repair_line="Nothing is retained, so nothing can be erased",
                digest=digest,
            )
        citing = citing_live_claims(connection, digest)
        exact = [
            str(row[0])
            for claim in citing
            for row in connection.execute(
                "SELECT identity FROM claims WHERE identity=? AND object_content_digest IN ("
                + ",".join("?" for _ in bodies)
                + ")",
                (claim, *bodies),
            )
        ]
        if exact:
            claim = exact[0].removeprefix("Claim:")
            raise _refuse(
                "cruxible.capture.erasure_body_in_accepted_state",
                f"Capture {digest}'s body is also the exact content of {claim}, "
                "which accepted state keeps",
                repair=RepairOperation(operation="cruxible.get", arguments={"ref": claim}),
                repair_line=(
                    "Erasure removes evidence bodies only; a Claim's value is accepted state "
                    "and stays, with its history"
                ),
                digest=digest,
            )
        left = claims_left_without_evidence(instance, connection, citing, erasing=frozenset(bodies))
    return envelope, contract, bodies, citing, left


__all__ = [
    "accepted_capture_contract",
    "service_erase_capture",
]
