"""Verification and append orchestration for the Claim-attestation evidence door."""

from __future__ import annotations

from datetime import datetime
from typing import NoReturn

from cruxible_client.contracts.attestation_evidence import (
    ClaimAttestationEvidenceRequest,
    ClaimAttestationEvidenceResult,
)
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestationAppendRequest,
    ClaimAttestationAppendResult,
    VerifiedClaimAttestation,
    claim_attestation_v2_envelope_digest,
    claim_attestation_v2_statement_digest,
)
from cruxible_client.contracts.claims import (
    claim_artifact_digest,
    claim_path,
)
from cruxible_client.contracts.errors import CruxibleError
from cruxible_client.contracts.temporal import utc_now
from cruxible_core.evidence.attestation_verification import (
    ClaimAttestationRefusal,
    _accepted_claim,
    _refuse,
    verify_attestation_admission,
    verify_attestation_referent,
)
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.claims.claims import _claim_law_evidence


def service_append_claim_attestation(
    instance: PlaybillInstance,
    *,
    request: ClaimAttestationAppendRequest,
    actor_id: str,
    recorded_at: datetime | None = None,
) -> ClaimAttestationAppendResult:
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
    except CruxibleError as exc:
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
        producer_receipt_resolver=instance.capture_verification_context(),
        historical_capture_contract=instance.accepted_capture_contract_versions(
            AcceptedCoordinate.from_internal(referent)
        ),
    )
    current = _accepted_claim(append_tree, statement.claim_identity.name)
    account = VerifiedClaimAttestation(
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
    )


def _refuse_evidence(code: str, message: str, *, repair: str) -> NoReturn:
    raise ClaimAttestationRefusal(code, f"{message}. {repair}")


def service_prepare_claim_attestation_evidence(
    instance: PlaybillInstance,
    *,
    request: ClaimAttestationEvidenceRequest,
    actor_id: str,
    observed_at: datetime | None = None,
) -> ClaimAttestationEvidenceResult:
    """Make or resolve the Captures an attestation will cite, as ``write`` would (D-R42).

    ``because`` (and ``self`` evidence, and a section cited as ``own_words``)
    becomes a self-source Capture bound to the attested Claim; observed file or
    section evidence becomes a working-selection Capture under its source's
    accepted contract; ``capture`` and ``contract`` evidence resolve to an
    existing Capture through write's own resolvers. Nothing is proposed or
    accepted: an attestation is a signed append, so a source whose contract is
    not accepted yet refuses (a write citing the file installs it), and file or
    section evidence the writer's adapter did not observe refuses.
    """

    instance.require_writable()
    from cruxible_client.contracts.captures import (
        COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
        build_coordinator_self_source_capture,
        build_working_selection_capture,
        builtin_contract_for_write,
        capture_contract_path,
        foreign_source_capture_contract,
        parse_capture_contract,
    )
    from cruxible_client.contracts.claims import parse_claim
    from cruxible_client.contracts.write import (
        CaptureEvidence,
        ContractEvidence,
        FileEvidence,
        SectionEvidence,
        SelfEvidence,
        WriteRequest,
    )
    from cruxible_core.service.authoring.write_verbs import _Planner
    from cruxible_core.service.identity import require_authoring_principal

    require_authoring_principal(instance, actor_id)
    head = instance.accepted_coordinate()
    public = AcceptedCoordinate.from_internal(head)
    claim_id = request.claim_id.removeprefix("Claim:")
    path = claim_path(claim_id)
    content = instance.blob_at(head.git_oid, path)
    if content is None:
        _refuse("claim_not_found", "Claim is not accepted at the current head")
    claim = parse_claim(content, path=path)
    if claim.retirement is not None or claim.lifecycle.state != "live":
        _refuse("claim_terminally_retired", "Claim lineage is retired at the current head")
    at = observed_at or utc_now()
    store = instance.body_store()

    def blob(path: str) -> bytes | None:
        # Keyed blob reads at the head: never the whole tree.
        return instance.blob_at(head.git_oid, path)

    def self_source(body: bytes) -> str:
        path = capture_contract_path(COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT.identity.name)
        accepted = blob(path)
        contract = builtin_contract_for_write(
            COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
            None if accepted is None else parse_capture_contract(accepted, path=path),
        )
        return build_coordinator_self_source_capture(
            store=store,
            actor_id=actor_id,
            claim_id=claim_id,
            body=body,
            accepted_contract=contract,
            observed_at=at,
            accepted_coordinate=public,
        ).capture_digest

    digests: set[str] = set()
    if request.because is not None:
        digests.add(self_source(request.because.encode("utf-8")))
    evidence = request.evidence
    if isinstance(evidence, SelfEvidence):
        digests.add(self_source(evidence.self.encode("utf-8")))
    elif isinstance(evidence, FileEvidence | SectionEvidence):
        observation = evidence.observation
        if observation is None:
            _refuse_evidence(
                "evidence_unobserved",
                "file and section evidence must be read on the writer's side; the daemon "
                "never reads workspace files",
                repair="Attest through the CLI, the SDK or the MCP adapter beside the workspace",
            )
        if isinstance(evidence, SectionEvidence) and evidence.own_words:
            digests.add(self_source(observation.selected_content))
        else:
            source = observation.source_id
            contract_path = capture_contract_path(
                foreign_source_capture_contract(source).identity.name
            )
            accepted_bytes = blob(contract_path)
            accepted = (
                None
                if accepted_bytes is None
                else parse_capture_contract(accepted_bytes, path=contract_path)
            )
            if accepted is None or accepted.lifecycle.state != "live":
                _refuse_evidence(
                    "source_contract_unaccepted",
                    f"the workspace source {source!r} has no accepted CaptureContract; an "
                    "attestation is a signed append and installs none",
                    repair="Cite the file once in a write (it installs the source's contract), "
                    "then attest",
                )
            digests.add(
                build_working_selection_capture(
                    accepted_contract=builtin_contract_for_write(
                        foreign_source_capture_contract(source), accepted
                    ),
                    store=store,
                    actor_id=actor_id,
                    claim_id=claim_id,
                    rationale=request.because or "Attested evidence.",
                    observed_at=at,
                    accepted_coordinate=public,
                    source_id=source,
                    coordinate=observation.coordinate.model_dump(mode="json"),
                    selector=observation.selector.model_dump(mode="json"),
                    selected_content=observation.selected_content,
                ).capture_digest
            )
    elif isinstance(evidence, CaptureEvidence | ContractEvidence):
        planner = _Planner(
            instance,
            head=head,
            read_at=head,
            request=WriteRequest.model_construct(
                because=request.because or "Attested evidence.", changes=(), docs=()
            ),
        )
        subject_path = claim.statement.subject.artifact_path
        if isinstance(evidence, CaptureEvidence):
            digests.add(
                evidence.capture
                if evidence.capture.startswith("sha256:")
                else planner.capture_by_handle(evidence.capture, index=0, path="evidence.capture")
            )
        else:
            digests.add(
                planner.capture_by_contract(
                    evidence.contract,
                    subject=subject_path.removeprefix("subjects/").removesuffix(".json"),
                    subject_path_value=subject_path,
                    index=0,
                    path="evidence.contract",
                )
            )
    return ClaimAttestationEvidenceResult(
        claim_id=claim_id,
        referent_coordinate=public,
        capture_digests=tuple(sorted(digests, key=lambda item: item.encode("ascii"))),
    )


__all__ = [
    "ClaimAttestationRefusal",
    "service_append_claim_attestation",
    "service_prepare_claim_attestation_evidence",
]
