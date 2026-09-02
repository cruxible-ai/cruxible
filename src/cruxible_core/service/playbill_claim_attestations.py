"""Verification and append orchestration for the Claim-attestation evidence door."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import NoReturn

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.captures import (
    CaptureContractV1,
    capture_contract_is_self_asserted,
    parse_capture_envelope,
    verify_capture,
)
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestationAppendRequestV1,
    ClaimAttestationAppendResultV1,
    ClaimAttestationResolvedArtifactV1,
    ClaimAttestationStatementV2,
    VerifiedClaimAttestationV2,
    claim_attestation_v2_envelope_digest,
    claim_attestation_v2_statement_digest,
    verify_claim_attestation_v2_principal,
)
from cruxible_client.contracts.claim_types import claim_type_path, parse_claim_type
from cruxible_client.contracts.claims import (
    ClaimArtifactAny,
    ClaimArtifactV3,
    ClaimBackingV2,
    SubjectClaimObject,
    claim_artifact_digest,
    claim_path,
    claim_statement_digest,
    evaluate_capture_evidence_admissions,
    parse_claim,
)
from cruxible_client.contracts.errors import PlaybillError, PlaybillFormatError
from cruxible_client.contracts.principals import principal_registry_from_tree
from cruxible_client.contracts.procedures.artifacts import (
    parse_procedure,
    procedure_artifact_digest,
    procedure_path,
)
from cruxible_client.contracts.providers import parse_provider, provider_digest, provider_path
from cruxible_client.contracts.source_references import LedgerSourceReferenceV1
from cruxible_client.contracts.subjects import parse_subject, subject_digest
from cruxible_client.contracts.temporal import utc_now
from cruxible_client.contracts.types import PrincipalRecord
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.producer_receipts import local_producer_receipt_resolver
from cruxible_core.playbill.projection import AcceptedCoordinate, AcceptedProjectionCoordinate
from cruxible_core.service.playbill_claims import _claim_law_evidence
from cruxible_core.service.playbill_evidence import _capture_contracts


class ClaimAttestationRefusal(PlaybillFormatError):
    """One typed, stable refusal from the attestation door."""

    def __init__(self, suffix: str, message: str) -> None:
        self.error_code = f"playbill.claim_attestation.{suffix}"
        super().__init__(f"{self.error_code}: {message}")


def _refuse(suffix: str, message: str) -> NoReturn:
    raise ClaimAttestationRefusal(suffix, message)


@dataclass(frozen=True)
class _TreeLedgerResolver:
    tree: dict[str, bytes]
    coordinate: AcceptedCoordinate

    def read_ledger_source(self, source: LedgerSourceReferenceV1) -> bytes:
        if source.coordinate != self.coordinate:
            _refuse("capture_binding_invalid", "Capture names another accepted coordinate")
        content = self.tree.get(source.address.artifact_path)
        if content is None:
            _refuse("capture_unavailable", "Capture ledger source is absent")
        return content


def _accepted_claim(tree: dict[str, bytes], claim_id: str) -> ClaimArtifactAny:
    path = claim_path(claim_id)
    content = tree.get(path)
    if content is None:
        _refuse("claim_not_found_at_referent", "signed Claim is absent at the referent")
    try:
        return parse_claim(content, path=path)
    except (PlaybillError, ValueError) as exc:
        raise ClaimAttestationRefusal(
            "statement_binding_mismatch", "accepted Claim does not parse canonically"
        ) from exc


def _subject_shell_digest(tree: dict[str, bytes], claim: ClaimArtifactAny) -> str:
    path = claim.statement.subject.artifact_path
    content = tree.get(path)
    if content is None:
        _refuse("statement_binding_mismatch", "Claim subject shell is absent")
    try:
        return subject_digest(parse_subject(content, path=path)).tagged
    except (PlaybillError, ValueError) as exc:
        raise ClaimAttestationRefusal(
            "statement_binding_mismatch", "Claim subject shell does not reproduce"
        ) from exc


def _object_shell_digest(tree: dict[str, bytes], claim: ClaimArtifactAny) -> str | None:
    if not isinstance(claim.statement.object, SubjectClaimObject):
        return None
    path = claim.statement.object.address.artifact_path
    content = tree.get(path)
    if content is None:
        _refuse("statement_binding_mismatch", "Claim object shell is absent")
    try:
        return subject_digest(parse_subject(content, path=path)).tagged
    except (PlaybillError, ValueError) as exc:
        raise ClaimAttestationRefusal(
            "statement_binding_mismatch", "Claim object shell does not reproduce"
        ) from exc


def _principal_at(
    tree: dict[str, bytes],
    *,
    coordinate: AcceptedCoordinate,
    statement: ClaimAttestationStatementV2,
    phase: str,
) -> PrincipalRecord:
    registry = principal_registry_from_tree(tree, semantic_root=coordinate.semantic_root)
    try:
        principal = registry.require_active(statement.attesting_principal_id)
    except PlaybillError as exc:
        raise ClaimAttestationRefusal(
            f"principal_inactive_at_{phase}",
            f"attesting principal is not active at {phase}",
        ) from exc
    if principal.kind != "ordinary":
        _refuse("principal_not_ordinary", "recovery and daemon principals cannot attest")
    if principal.public_key_digest != statement.signing_key_digest:
        _refuse(
            f"signing_key_invalid_at_{phase}",
            f"attestation signing key is not valid at {phase}",
        )
    return principal


def _examined_capture_semantics(claim: ClaimArtifactAny, capture_digest: str) -> None:
    if capture_digest not in claim.backing.capture_digests:
        _refuse("examined_capture_not_backing", "Capture is not backing of the signed Claim")
    if not isinstance(claim.backing, ClaimBackingV2):
        return
    associations = tuple(
        item for item in claim.backing.citations if item.capture_digest == capture_digest
    )
    # No explicit association means immutable legacy-evidence semantics.
    if associations and not any(item.role == "evidence" for item in associations):
        _refuse("examined_capture_not_evidence", "copy-only backing is not attestable evidence")


def _provider_digests(tree: dict[str, bytes]) -> dict[str, str]:
    result: dict[str, str] = {}
    for path, content in tree.items():
        if not path.startswith("providers/"):
            continue
        provider = parse_provider(content, path=path)
        result[provider.identity.qualified] = provider_digest(provider).tagged
    return result


def _procedure_digests(tree: dict[str, bytes]) -> dict[str, str]:
    result: dict[str, str] = {}
    for path, content in tree.items():
        if not path.startswith("procedures/"):
            continue
        procedure = parse_procedure(content, path=path)
        result[procedure.identity.qualified] = procedure_artifact_digest(procedure).tagged
    return result


def _live_at_append(
    identity: ArtifactIdentity,
    *,
    append_tree: dict[str, bytes],
) -> bool:
    if identity.kind == "Provider":
        content = append_tree.get(provider_path(identity.name))
        if content is None:
            return False
        provider = parse_provider(content, path=provider_path(identity.name))
        return provider.lifecycle.state == "live"
    if identity.kind == "CaptureContract":
        return any(
            accepted.contract.identity == identity and accepted.contract.lifecycle.state == "live"
            for accepted in _capture_contracts(append_tree).values()
        )
    if identity.kind == "Procedure":
        content = append_tree.get(procedure_path(identity.name))
        if content is None:
            return False
        procedure = parse_procedure(content, path=procedure_path(identity.name))
        return procedure.lifecycle.state == "live"
    return False


def _new_capture_accounts(
    instance: PlaybillInstance,
    *,
    statement: ClaimAttestationStatementV2,
    claim: ClaimArtifactAny,
    referent: AcceptedProjectionCoordinate,
    referent_tree: dict[str, bytes],
    append_tree: dict[str, bytes],
) -> tuple[tuple[str, ...], tuple[ClaimAttestationResolvedArtifactV1, ...]]:
    contracts = _capture_contracts(referent_tree)
    providers = _provider_digests(referent_tree)
    procedures = _procedure_digests(referent_tree)
    producers = providers | procedures
    admitted: list[str] = []
    claim_type_content = referent_tree.get(claim_type_path(claim.statement.predicate))
    if claim_type_content is None:
        _refuse("capture_admission_refused", "ClaimType is absent at the signed referent")
    try:
        claim_type = parse_claim_type(
            claim_type_content,
            path=claim_type_path(claim.statement.predicate),
        )
        law = _claim_law_evidence(
            instance,
            path=claim_path(claim.identity.name),
            at=referent,
        )
    except (PlaybillError, ValueError) as exc:
        raise ClaimAttestationRefusal(
            "capture_admission_refused",
            "Claim evidence admission inputs do not reproduce at the signed referent",
        ) from exc
    principals = principal_registry_from_tree(
        referent_tree,
        semantic_root=referent.semantic_root,
    )
    bodies = instance.body_store()
    producer_receipt_resolver = local_producer_receipt_resolver(
        exhaust_root=instance.root / instance.descriptor.storage.exhaust,
        instance_id=instance.descriptor.instance_id,
        bodies=bodies,
    )
    resolved: dict[tuple[str, str], ClaimAttestationResolvedArtifactV1] = {}
    for digest in statement.cited_capture_digests:
        try:
            raw = bodies.read(
                digest,
                access=BodyAccessContext(
                    principal_id="playbill-claim-attestation",
                    can_read_body=True,
                ),
            )
        except PlaybillError as exc:
            raise ClaimAttestationRefusal(
                "capture_unavailable", "Capture is absent from CAS"
            ) from exc
        try:
            envelope = parse_capture_envelope(raw)
        except (PlaybillError, ValueError) as exc:
            raise ClaimAttestationRefusal("capture_invalid", "Capture envelope is invalid") from exc
        accepted = contracts.get(envelope.capture_contract_digest)
        if accepted is None:
            _refuse("capture_contract_unresolved", "CaptureContract is not accepted at referent")
        contract: CaptureContractV1 = accepted.contract
        if contract.lifecycle.state != "live":
            _refuse(
                "capture_contract_not_live_at_referent",
                "CaptureContract is not live at referent",
            )
        executable = envelope.run_coordinate.executable_identity
        if executable == contract.identity:
            if envelope.run_coordinate.executable_digest != accepted.artifact_digest:
                _refuse(
                    "capture_executable_unresolved",
                    "Capture executable does not resolve to its exact CaptureContract",
                )
        elif executable.kind in {"Provider", "Procedure"}:
            executable_digest = producers.get(executable.qualified)
            if executable_digest != envelope.run_coordinate.executable_digest:
                _refuse(
                    "capture_executable_unresolved",
                    "Capture executable producer is not accepted at its exact digest",
                )
        else:
            _refuse(
                "capture_executable_unresolved",
                "Capture executable is not an accepted CaptureContract or producer",
            )

        provenance_grade = (
            "self-asserted" if capture_contract_is_self_asserted(contract) else "daemon-fetched"
        )
        if envelope.producer.kind == "Provider":
            if envelope.producer.qualified not in providers:
                _refuse("capture_provider_unresolved", "Capture Provider is not accepted")
        elif envelope.producer.kind == "Principal":
            try:
                principals.require_active(envelope.producer.name)
            except PlaybillError as exc:
                raise ClaimAttestationRefusal(
                    "capture_provider_unresolved",
                    "Capture producer Principal is not active at the signed referent",
                ) from exc
            if provenance_grade != "self-asserted":
                _refuse(
                    "capture_provider_unresolved",
                    "daemon-fetched Capture provenance requires an accepted Provider",
                )
        elif envelope.producer.kind == "Procedure":
            if envelope.producer.qualified not in procedures:
                _refuse("capture_provider_unresolved", "Capture Procedure is not accepted")
        else:
            _refuse(
                "capture_provider_unresolved",
                "Capture producer is not an accepted Provider, Procedure, or active Principal",
            )
        try:
            verify_capture(
                digest,
                store=bodies,
                contract=contract,
                ledger_resolver=_TreeLedgerResolver(
                    referent_tree,
                    statement.referent_coordinate,
                ),
                producer_artifact_digests=producers,
                producer_receipt_resolver=producer_receipt_resolver,
            )
        except ClaimAttestationRefusal:
            raise
        except (PlaybillError, ValueError) as exc:
            raise ClaimAttestationRefusal(
                "capture_binding_invalid",
                "Capture does not reproduce against its accepted contract and producer",
            ) from exc
        try:
            decisions = evaluate_capture_evidence_admissions(
                claim,
                claim_type=claim_type,
                capture_digest=digest,
                capture_contract=accepted,
                envelope=envelope,
                verified_attestations=law.verified_attestations,
            )
        except (PlaybillError, ValueError) as exc:
            raise ClaimAttestationRefusal(
                "capture_admission_refused",
                "Capture evidence admission could not be evaluated",
            ) from exc
        if not decisions:
            _refuse(
                "capture_admission_refused",
                "CaptureContract declares no evaluable evidence kind",
            )
        if any(item.trace.result.verdict == "eligible" for item in decisions):
            admitted.append(digest)
        contract_resolved = ClaimAttestationResolvedArtifactV1(
            identity=contract.identity,
            artifact_digest=accepted.artifact_digest,
            live_at_append=_live_at_append(contract.identity, append_tree=append_tree),
        )
        resolved[(contract.identity.qualified, accepted.artifact_digest)] = contract_resolved
        for identity in {envelope.producer, envelope.run_coordinate.executable_identity}:
            if identity.kind not in {"Provider", "Procedure"}:
                continue
            artifact_digest = producers.get(identity.qualified)
            if artifact_digest is None:
                _refuse("capture_provider_unresolved", "Capture Provider is not accepted")
            resolved[(identity.qualified, artifact_digest)] = ClaimAttestationResolvedArtifactV1(
                identity=identity,
                artifact_digest=artifact_digest,
                live_at_append=_live_at_append(identity, append_tree=append_tree),
            )
    return (
        tuple(admitted),
        tuple(
            resolved[key]
            for key in sorted(
                resolved,
                key=lambda item: (item[0].encode("utf-8"), item[1].encode("ascii")),
            )
        ),
    )


def service_append_claim_attestation(
    instance: PlaybillInstance,
    *,
    request: ClaimAttestationAppendRequestV1,
    actor_id: str,
    recorded_at: datetime | None = None,
) -> ClaimAttestationAppendResultV1:
    """Verify one signed V2 observation and publish exactly one evidence event."""

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
    referent_tree = instance.tree_at(referent.git_oid)
    claim = _accepted_claim(referent_tree, statement.claim_identity.name)
    if claim.identity != statement.claim_identity:
        _refuse("statement_binding_mismatch", "signed Claim identity does not reproduce")
    if claim_artifact_digest(claim).tagged != statement.claim_artifact_digest:
        _refuse("claim_artifact_digest_mismatch", "signed Claim artifact digest differs")
    if claim_statement_digest(claim.statement).tagged != statement.claim_statement_digest:
        _refuse("statement_binding_mismatch", "signed Claim statement digest differs")
    if _subject_shell_digest(referent_tree, claim) != statement.subject_shell_digest or (
        _object_shell_digest(referent_tree, claim) != statement.object_shell_digest
    ):
        _refuse("statement_binding_mismatch", "signed Claim shell digest differs")
    at = recorded_at or utc_now()
    referent_principal = _principal_at(
        referent_tree,
        coordinate=statement.referent_coordinate,
        statement=statement,
        phase="referent",
    )
    try:
        verify_claim_attestation_v2_principal(
            request.attestation,
            principal=referent_principal,
        )
    except PlaybillError as exc:
        raise ClaimAttestationRefusal("signature_invalid", "signature does not verify") from exc
    if statement.attested_at > at:
        _refuse("request_invalid", "attested_at is in the future")
    duplicate = instance.claim_attestation_evidence_store().duplicate(
        attestation=request.attestation
    )
    if duplicate is not None:
        return duplicate

    append_coordinate = instance.accepted_coordinate()
    append_tree = instance.tree_at(append_coordinate.git_oid)
    _principal_at(
        append_tree,
        coordinate=AcceptedCoordinate.from_internal(append_coordinate),
        statement=statement,
        phase="append",
    )
    current = _accepted_claim(append_tree, statement.claim_identity.name)
    if isinstance(current, ClaimArtifactV3):
        _refuse("claim_terminally_retired", "Claim lineage is terminally retired")

    if statement.attestation_basis == "examined_existing":
        for digest in statement.cited_capture_digests:
            _examined_capture_semantics(claim, digest)
        admitted = statement.cited_capture_digests
        resolved: tuple[ClaimAttestationResolvedArtifactV1, ...] = ()
    else:
        admitted, resolved = _new_capture_accounts(
            instance,
            statement=statement,
            claim=claim,
            referent=referent,
            referent_tree=referent_tree,
            append_tree=append_tree,
        )
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
