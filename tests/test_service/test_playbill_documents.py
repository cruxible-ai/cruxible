"""PB-E service-level Document lifecycle and canonical read tests."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
)
from cruxible_client.contracts.errors import (
    ApprovalIntegrityError,
    DocumentNotFoundError,
    PlaybillCasError,
    PlaybillFormatError,
    SettlementIntegrityError,
)
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.service.playbill_documents import (
    PlaybillAcceptedCoordinate,
    service_activate_playbill_proposal,
    service_dereference_playbill_document,
    service_get_playbill_document,
    service_inspect_playbill_proposal,
    service_inspect_playbill_refusal,
    service_list_playbill_documents,
    service_list_playbill_principals,
    service_playbill_document_history,
    service_propose_playbill_document,
    service_propose_playbill_principal_change,
    service_store_playbill_body,
    service_submit_playbill_approval,
)
from cruxible_core.service.playbill_review import service_prepare_playbill_approval
from tests.test_playbill._support import FIXED_TIMESTAMP, generate_client
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_principal_history import _cloud_instance, _replacement_key

TIMESTAMP = "2026-08-13T12:00:00.000000Z"


def _instance(tmp_path: Path):
    managed = tmp_path / "managed"
    owner = generate_client(
        tmp_path,
        managed_root=managed,
        principal_id="owner",
        roles=("owner",),
    )
    reviewer = generate_client(
        tmp_path,
        managed_root=managed,
        principal_id="reviewer",
        roles=("reviewer",),
    )
    instance = PlaybillInstance.initialize(
        managed,
        instance_id="inst_service_playbill",
        client_principals=(owner.principal, reviewer.principal),
        workspace_roots=(tmp_path / "workspace",),
        timestamp=FIXED_TIMESTAMP,
    )
    return instance, owner, reviewer


def _shell(body_digest: str) -> DocumentShell:
    return DocumentShell(
        identity="document:design",
        document_kind="design",
        title="Playbill design",
        media_type="text/markdown",
        body_digest=body_digest,
        authority=DocumentAuthority(
            required_tier="graph_write",
            approval_roles=("owner", "reviewer"),
        ),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )


def test_service_document_lifecycle_keeps_state_boundaries_explicit(tmp_path: Path) -> None:
    instance, owner, reviewer = _instance(tmp_path)
    body_bytes = b"# Playbill design\n\nGoverned prose.\n"
    stored = service_store_playbill_body(instance, content=body_bytes)

    assert stored.present
    assert (
        service_list_playbill_documents(
            instance,
            access=BodyAccessContext(principal_id="reader"),
        ).documents
        == ()
    )

    proposed = service_propose_playbill_document(
        instance,
        shell=_shell(stored.digest),
        actor_id="owner",
        proposal_name="design",
        timestamp=TIMESTAMP,
        source_compilation_digest="sha256:" + "77" * 32,
    )
    proposal = proposed.proposal
    assert proposal.evaluation.verdict == "candidate"
    assert proposal.candidate is not None
    assert instance.accepted_coordinate().git_oid == proposed.accepted_coordinate.git_oid
    with pytest.raises(DocumentNotFoundError):
        service_get_playbill_document(
            instance,
            identity="document:design",
            access=BodyAccessContext(principal_id="reader"),
        )

    persisted = service_inspect_playbill_proposal(
        instance,
        proposal_id=proposal.admission.proposal_id,
    )
    assert persisted.proposal == proposal
    candidate = persisted.proposal.candidate
    assert candidate is not None
    signed = _sign(
        reviewer,
        candidate.candidate_digest,
        candidate.candidate.parent_semantic_root,
    )
    receipt = service_submit_playbill_approval(
        instance,
        proposal_id=proposal.admission.proposal_id,
        attestation=signed.attestation,
        authenticated_submitter="approval-relay",
    )
    assert receipt.signer_id == reviewer.principal.principal_id
    assert receipt.submitted_by == "approval-relay"

    activated = service_activate_playbill_proposal(
        instance,
        proposal_id=proposal.admission.proposal_id,
        activated_by="owner",
    )
    assert activated.status == "accepted"
    assert activated.accepted_coordinate is not None
    assert activated.accepted_coordinate.git_oid == instance.accepted_coordinate().git_oid

    redacted = service_get_playbill_document(
        instance,
        identity="document:design",
        access=BodyAccessContext(principal_id="reader"),
    )
    assert redacted.coordinate_kind == "canonical"
    assert redacted.coordinate == activated.accepted_coordinate
    assert not any(
        fact["schema_id"] == "playbill.document.source_mapping" for fact in redacted.facts
    )
    with pytest.raises(PlaybillCasError, match="denied"):
        service_dereference_playbill_document(
            instance,
            identity="document:design",
            access=BodyAccessContext(principal_id="reader"),
        )

    readable = service_dereference_playbill_document(
        instance,
        identity="document:design",
        access=BodyAccessContext(principal_id="owner", can_read_body=True),
    )
    assert readable.body_digest == stored.digest
    assert base64.b64decode(readable.content_base64) == body_bytes
    history = service_playbill_document_history(instance, identity="document:design")
    assert len(history.entries) == 1
    assert history.entries[0].candidate_digest == candidate.candidate_digest
    assert history.entries[0].coordinate == activated.accepted_coordinate


def test_service_refusal_and_coordinate_mixing_are_typed(tmp_path: Path) -> None:
    instance, _owner, _reviewer = _instance(tmp_path)
    missing_body = "sha256:" + "99" * 32
    refused = service_propose_playbill_document(
        instance,
        shell=_shell(missing_body),
        actor_id="owner",
        proposal_name="missing-body",
        timestamp=TIMESTAMP,
    )

    inspection = service_inspect_playbill_refusal(
        instance,
        proposal_id=refused.proposal.admission.proposal_id,
    )
    assert inspection.verdict == "refused"
    assert [item.code for item in inspection.diagnostics] == ["playbill.document.body_missing"]

    current = PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate())
    mixed = current.model_copy(update={"semantic_root": "sha256:" + "88" * 32})
    with pytest.raises(PlaybillFormatError, match="mixed"):
        service_list_playbill_documents(
            instance,
            access=BodyAccessContext(principal_id="reader"),
            at=mixed,
        )


def test_service_owner_rotation_and_recovery_require_lifecycle_actor_key_binding(
    tmp_path: Path,
) -> None:
    instance, owner, recovery = _cloud_instance(tmp_path)
    rotated_owner = _replacement_key(
        tmp_path,
        instance,
        custody_name="service-owner-rotated",
        principal_id="owner",
        roles=("owner",),
    )
    rotated = service_propose_playbill_principal_change(
        instance,
        principal=rotated_owner.principal,
        actor_id="owner",
        proposal_name="service-owner-rotation",
        timestamp="2026-08-13T12:01:00.000000Z",
    ).proposal
    assert rotated.candidate is not None
    with pytest.raises(SettlementIntegrityError, match="cryptographically approve"):
        service_activate_playbill_proposal(
            instance,
            proposal_id=rotated.admission.proposal_id,
            activated_by="owner",
        )
    rotation_challenge = service_prepare_playbill_approval(
        instance,
        proposal_id=rotated.admission.proposal_id,
        signer_id="owner",
        access=BodyAccessContext(principal_id="owner"),
    )
    rotation_signature = _sign(
        owner,
        rotation_challenge.statement.payload_digest,
        rotation_challenge.statement.signing_semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=rotated.admission.proposal_id,
        attestation=rotation_signature.attestation,
        authenticated_submitter="owner",
    )
    service_activate_playbill_proposal(
        instance,
        proposal_id=rotated.admission.proposal_id,
        activated_by="owner",
    )
    assert (
        next(
            item
            for item in service_list_playbill_principals(instance).principals
            if item.principal_id == "owner"
        )
        == rotated_owner.principal
    )

    recovered_owner = _replacement_key(
        tmp_path,
        instance,
        custody_name="service-owner-recovered",
        principal_id="owner",
        roles=("owner",),
    )
    recovered = service_propose_playbill_principal_change(
        instance,
        principal=recovered_owner.principal,
        actor_id="recovery",
        proposal_name="service-owner-recovery",
        timestamp="2026-08-13T12:02:00.000000Z",
    ).proposal
    assert recovered.candidate is not None
    with pytest.raises(SettlementIntegrityError, match="cryptographically approve"):
        service_activate_playbill_proposal(
            instance,
            proposal_id=recovered.admission.proposal_id,
            activated_by="recovery",
        )
    recovery_challenge = service_prepare_playbill_approval(
        instance,
        proposal_id=recovered.admission.proposal_id,
        signer_id="recovery",
        access=BodyAccessContext(principal_id="recovery"),
    )
    recovery_signature = _sign(
        recovery,
        recovery_challenge.statement.payload_digest,
        recovery_challenge.statement.signing_semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=recovered.admission.proposal_id,
        attestation=recovery_signature.attestation,
        authenticated_submitter="recovery",
    )
    service_activate_playbill_proposal(
        instance,
        proposal_id=recovered.admission.proposal_id,
        activated_by="recovery",
    )
    assert (
        next(
            item
            for item in service_list_playbill_principals(instance).principals
            if item.principal_id == "owner"
        )
        == recovered_owner.principal
    )

    body = service_store_playbill_body(instance, content=b"# Ordinary document\n")
    ordinary = service_propose_playbill_document(
        instance,
        shell=DocumentShell(
            identity="document:ordinary",
            document_kind="design",
            title="Ordinary",
            media_type="text/markdown",
            body_digest=body.digest,
            authority=DocumentAuthority(
                required_tier="graph_write",
                approval_roles=("owner",),
            ),
            governance_scope=("project:playbill",),
            lifecycle=DocumentLifecycle(revision=1),
        ),
        actor_id="owner",
        proposal_name="ordinary-after-recovery",
        timestamp="2026-08-13T12:03:00.000000Z",
    ).proposal
    with pytest.raises(ApprovalIntegrityError, match="recovery principal"):
        service_prepare_playbill_approval(
            instance,
            proposal_id=ordinary.admission.proposal_id,
            signer_id="recovery",
            access=BodyAccessContext(principal_id="recovery"),
        )
