"""Verification and append orchestration for the Claim-attestation evidence door."""

from __future__ import annotations

from datetime import datetime

from cruxible_client.contracts.claim_attestations import (
    ClaimAttestationAppendRequestV1,
    ClaimAttestationAppendResultV1,
    VerifiedClaimAttestationV2,
    claim_attestation_v2_envelope_digest,
    claim_attestation_v2_statement_digest,
)
from cruxible_client.contracts.claims import (
    claim_artifact_digest,
    claim_path,
)
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.temporal import utc_now
from cruxible_core.evidence.attestation_verification import (
    ClaimAttestationRefusal,
    _accepted_claim,
    _refuse,
    verify_attestation_admission,
    verify_attestation_referent,
)
from cruxible_core.exhaust.producer_receipts import local_producer_receipt_resolver
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.claims.claims import _claim_law_evidence


def service_append_claim_attestation(
    instance: PlaybillInstance,
    *,
    request: ClaimAttestationAppendRequestV1,
    actor_id: str,
    recorded_at: datetime | None = None,
) -> ClaimAttestationAppendResultV1:
    """Verify one signed V2 observation and publish exactly one evidence event."""

    instance.require_writable()
    statement = request.attestation.statement
    if actor_id != statement.attesting_principal_id:
        _refuse("actor_signer_mismatch", "authenticated actor must equal attesting principal")
    if statement.instance_id != instance.descriptor.instance_id:
        _refuse("statement_binding_mismatch", "attestation belongs to another instance")
    try:
        referent = instance.resolve_accepted_coordinate(
            git_oid=statement.referent_coordinate.git_oid,
            semantic_root=statement.referent_coordinate.semantic_root,
            generation_root=statement.referent_coordinate.generation_root,
            compiler_digest=statement.referent_coordinate.compiler_digest,
        )
    except PlaybillError as exc:
        raise ClaimAttestationRefusal(
            "referent_coordinate_unaccepted", "referent is not an accepted coordinate"
        ) from exc
    from cruxible_core.service.evidence.evidence import ClaimVerdictReadContext

    at = recorded_at or utc_now()
    referent_tree = ClaimVerdictReadContext(instance, referent).tree
    principals = instance.accepted_principal_registry(referent)
    claim = verify_attestation_referent(
        request.attestation,
        instance_id=instance.descriptor.instance_id,
        referent_tree=referent_tree,
        referent_principals=principals,
        at=at,
    )
    duplicate = instance.claim_attestation_evidence_store().duplicate(
        attestation=request.attestation
    )
    if duplicate is not None:
        return duplicate
    append_coordinate = instance.accepted_coordinate()
    append_tree = ClaimVerdictReadContext(instance, append_coordinate).tree
    admitted, resolved = verify_attestation_admission(
        request.attestation,
        claim=claim,
        referent_tree=referent_tree,
        referent_principals=principals,
        current_tree=append_tree,
        current_principals=instance.accepted_principal_registry(append_coordinate),
        bodies=instance.body_store(),
        law=(
            _claim_law_evidence(
                instance, path=claim_path(statement.claim_identity.name), at=referent
            )
            if statement.attestation_basis == "new_capture"
            else None
        ),
        producer_receipt_resolver=local_producer_receipt_resolver(
            exhaust_root=instance.root / instance.descriptor.storage.exhaust,
            instance_id=instance.descriptor.instance_id,
            bodies=instance.body_store(),
        ),
    )
    current = _accepted_claim(append_tree, statement.claim_identity.name)
    account = VerifiedClaimAttestationV2(
        statement_digest=claim_attestation_v2_statement_digest(statement),
        envelope_digest=claim_attestation_v2_envelope_digest(request.attestation),
        statement=statement,
        referent_coordinate=statement.referent_coordinate,
        append_coordinate=AcceptedCoordinate.from_internal(append_coordinate),
        attesting_principal_id=statement.attesting_principal_id,
        submitted_by=actor_id,
        current_at_append=(
            claim_artifact_digest(current).tagged == statement.claim_artifact_digest
        ),
        resolved_artifacts=resolved,
        admitted_capture_digests=admitted,
        recorded_at=at,
    )
    return instance.claim_attestation_evidence_store().append(
        attestation=request.attestation,
        verification_account=account,
        note=request.note,
    )


__all__ = [
    "ClaimAttestationRefusal",
    "service_append_claim_attestation",
]
