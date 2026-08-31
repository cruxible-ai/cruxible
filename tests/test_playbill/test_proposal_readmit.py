"""Stale proposal readmission reuses the governed rebase and immutable evidence."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
)
from cruxible_client.contracts.errors import ProposalAdmissionError
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_propose_playbill_document,
    service_store_playbill_body,
    service_submit_playbill_approval,
)
from cruxible_core.service.playbill_proposals import service_readmit_playbill_proposal
from tests.test_playbill._support import client_material, initialize_local
from tests.test_playbill.test_activation import _sign

TIMESTAMP = "2026-08-22T12:00:00.000000Z"


def _shell(document_id: str, body_digest: str, *, title: str) -> DocumentShell:
    return DocumentShell(
        identity=f"document:{document_id}",
        document_kind="design",
        title=title,
        media_type="text/markdown",
        body_digest=body_digest,
        authority=DocumentAuthority(
            required_tier="graph_write",
        ),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )


def _accept(instance, owner, inspection) -> None:
    candidate = inspection.proposal.candidate
    assert candidate is not None
    approver = client_material(instance.root.parent, instance)
    approval = _sign(
        approver,
        candidate.candidate_digest,
        candidate.candidate.parent_semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=inspection.proposal.admission.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter=approver.principal.principal_id,
    )
    assert (
        service_activate_playbill_proposal(
            instance,
            proposal_id=inspection.proposal.admission.proposal_id,
            activated_by="owner",
        ).status
        == "accepted"
    )


def test_readmit_cleanly_rebases_and_response_loss_retry_returns_one_admission(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    alpha = service_store_playbill_body(instance, content=b"alpha").digest
    beta = service_store_playbill_body(instance, content=b"beta").digest
    accepted_first = service_propose_playbill_document(
        instance,
        shell=_shell("alpha", alpha, title="Alpha"),
        actor_id="owner",
        proposal_name="alpha",
        timestamp=TIMESTAMP,
    )
    stale = service_propose_playbill_document(
        instance,
        shell=_shell("beta", beta, title="Beta"),
        actor_id="owner",
        proposal_name="beta",
        timestamp=TIMESTAMP,
    )
    source_id = stale.proposal.admission.proposal_id
    source_bytes = (
        stale.proposal.admission.model_dump_json(),
        stale.proposal.evaluation.model_dump_json(),
    )
    _accept(instance, owner, accepted_first)

    with pytest.raises(ProposalAdmissionError, match="source proposal actor"):
        service_readmit_playbill_proposal(instance, proposal_id=source_id, actor_id="reviewer")

    first = service_readmit_playbill_proposal(
        instance,
        proposal_id=source_id,
        actor_id="owner",
    )
    retry = service_readmit_playbill_proposal(
        instance,
        proposal_id=source_id,
        actor_id="owner",
    )

    assert first == retry
    assert first.proposal.proposal.evaluation.verdict == "candidate"
    assert first.proposal.proposal.evaluation.rebased is True
    tree_oid = first.proposal.proposal.evaluation.evaluated_tree_oid
    assert tree_oid is not None
    assert set(instance.proposal_tree(tree_oid)) >= {
        "documents/alpha.json",
        "documents/beta.json",
    }
    coordinate = first.proposal.accepted_coordinate
    assert (
        first.operation_digest
        == typed_digest(
            Sha256Value,
            "playbill-proposal-readmit-v1",
            {
                "source_proposal_id": source_id,
                "current_accepted_coordinate": coordinate.model_dump(mode="json"),
            },
        ).tagged
    )
    assert first.proposal.proposal.admission.source_compilation_digest == first.operation_digest
    assert (
        instance.proposal_evidence().read_admission(source_id).model_dump_json(),
        instance.proposal_evidence().read_evaluation(source_id).model_dump_json(),
    ) == source_bytes
    with pytest.raises(ProposalAdmissionError, match="settled stale"):
        service_readmit_playbill_proposal(
            instance,
            proposal_id=first.proposal.proposal.admission.proposal_id,
            actor_id="owner",
        )


def test_readmit_returns_a_typed_refused_proposal_when_content_no_longer_preflights(
    tmp_path: Path,
) -> None:
    instance, owner = initialize_local(tmp_path)
    first_body = service_store_playbill_body(instance, content=b"first").digest
    other_body = service_store_playbill_body(instance, content=b"other").digest
    accepted = service_propose_playbill_document(
        instance,
        shell=_shell("same", first_body, title="Accepted"),
        actor_id="owner",
        proposal_name="accepted-same",
        timestamp=TIMESTAMP,
    )
    conflicting = service_propose_playbill_document(
        instance,
        shell=_shell("same", other_body, title="Conflicting"),
        actor_id="owner",
        proposal_name="conflicting-same",
        timestamp=TIMESTAMP,
    )
    _accept(instance, owner, accepted)

    result = service_readmit_playbill_proposal(
        instance,
        proposal_id=conflicting.proposal.admission.proposal_id,
        actor_id="owner",
    )

    assert result.proposal.proposal.evaluation.verdict == "refused"
    assert result.proposal.proposal.candidate is None
    assert result.proposal.proposal.evaluation.diagnostics
