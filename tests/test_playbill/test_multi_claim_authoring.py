"""Multi-Claim authoring: one proposal, one change set, one generation."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_core.playbill.artifacts import ArtifactIdentity
from cruxible_core.playbill.claim_types import ClaimType, claim_type_digest
from cruxible_core.playbill.claims import (
    ClaimStatement,
    LiteralClaimObject,
    SubjectClaimObject,
    claim_path,
)
from cruxible_core.playbill.descriptor_claim_types import descriptor_claim_type
from cruxible_core.playbill.errors import ProposalIntegrityError
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.semantic import SemanticAddress
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.playbill.settlement import ChangeActorBinding
from cruxible_core.service.playbill_claims import (
    DirectClaimAuthoringV1,
    DirectClaimBatchProposalV1,
    service_propose_playbill_claim,
    service_propose_playbill_claims,
)
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_claims import _claim_type, _subject

TIMESTAMP = "2026-08-16T20:00:00.000000Z"
STATUS_CLAIM_ID = "CLM-" + "1" * 32
SUMMARY_CLAIM_ID = "CLM-" + "2" * 32
DISTINCT_CLAIM_ID = "CLM-" + "3" * 32


def _summary_claim_type() -> ClaimType:
    return _claim_type().model_copy(
        update={
            "identity": ArtifactIdentity(kind="ClaimType", name="project.work_item.summary"),
            "predicate": "project.work_item.summary",
            "literal_schema": {"type": "string"},
        }
    )


def _status_authoring(*, claim_id: str | None = STATUS_CLAIM_ID) -> DirectClaimAuthoringV1:
    shell = _subject()
    status_type = _claim_type()
    return DirectClaimAuthoringV1(
        statement=ClaimStatement(
            subject=SemanticAddress.whole_artifact(
                f"subjects/{shell.subject_kind}/{shell.subject_id}.yaml"
            ),
            claim_type=status_type.identity,
            claim_type_digest=claim_type_digest(status_type).tagged,
            predicate=status_type.predicate,
            object=LiteralClaimObject(value="ready"),
            role="observation",
        ),
        rationale="The review inputs are complete.",
        claim_id=claim_id,
        subject_shell=shell,
        claim_type_artifact=status_type,
    )


def _summary_authoring(*, claim_id: str | None = SUMMARY_CLAIM_ID) -> DirectClaimAuthoringV1:
    shell = _subject()
    summary_type = _summary_claim_type()
    return DirectClaimAuthoringV1(
        statement=ClaimStatement(
            subject=SemanticAddress.whole_artifact(
                f"subjects/{shell.subject_kind}/{shell.subject_id}.yaml"
            ),
            claim_type=summary_type.identity,
            claim_type_digest=claim_type_digest(summary_type).tagged,
            predicate=summary_type.predicate,
            object=LiteralClaimObject(value="Ship the review surface"),
            role="observation",
        ),
        rationale="The summary is the one line the work item is tracked by.",
        claim_id=claim_id,
        subject_shell=shell,
        claim_type_artifact=summary_type,
    )


def _activate(
    instance: PlaybillInstance,
    owner: object,
    proposed: DirectClaimBatchProposalV1,
    *,
    sequence: int = 1,
) -> None:
    base = instance.accepted_coordinate()
    candidate = proposed.proposal.proposal.candidate
    assert candidate is not None
    evaluated_oid = proposed.proposal.proposal.evaluation.evaluated_tree_oid
    assert evaluated_oid is not None
    bundle = instance.prepare_generation(
        base=base,
        candidate_tree=instance.proposal_tree(evaluated_oid),
        candidate=candidate,
        approvals=(_sign(owner, candidate.candidate_digest, base.semantic_root),),
        actor_binding=ChangeActorBinding(actor_id="owner"),
        sequence=sequence,
    )
    publisher = instance.activation_publisher()
    projection = publisher.prebuild(bundle, base=base)
    assert publisher.activate(bundle, projection, base=base).status == "accepted"
    instance.refresh()


def test_every_authored_claim_settles_in_one_generation(tmp_path: Path) -> None:
    """Two Claims proposed together are members of one change set, not two."""

    instance, owner = initialize_local(tmp_path)
    proposed = service_propose_playbill_claims(
        instance,
        authorings=(_status_authoring(), _summary_authoring()),
        actor_id="owner",
        proposal_name="status-and-summary",
        timestamp=TIMESTAMP,
    )

    assert tuple(item.claim_path for item in proposed.claims) == (
        claim_path(STATUS_CLAIM_ID),
        claim_path(SUMMARY_CLAIM_ID),
    )
    candidate = proposed.proposal.proposal.candidate
    assert candidate is not None
    claim_members = {member.path for member in candidate.members if member.artifact_kind == "claim"}
    assert claim_members == {item.claim_path for item in proposed.claims}

    _activate(instance, owner, proposed)
    history = instance.accepted_history()
    assert len(history) == 2
    record = history[1].record
    assert record is not None
    assert {item.claim_path for item in proposed.claims} <= {
        member.path for member in record.members
    }
    accepted_tree = instance.tree_at(history[1].oid)
    for authored in proposed.claims:
        assert authored.claim_path in accepted_tree


def test_one_authoring_is_the_same_proposal_through_either_entrypoint(tmp_path: Path) -> None:
    """The singular entrypoint is a delegation, so its candidate must not move.

    Both proposals are taken from the same accepted base with the same pinned
    Claim id and the same request timestamp, so every digest below is a
    function of the candidate tree alone. Comparing the candidate digest is the
    load-bearing assertion: it commits the whole tree, not just the Claim.
    """

    instance, _owner = initialize_local(tmp_path)
    singular = service_propose_playbill_claim(
        instance,
        authoring=_status_authoring(),
        actor_id="owner",
        proposal_name="singular-entrypoint",
        timestamp=TIMESTAMP,
    )
    plural = service_propose_playbill_claims(
        instance,
        authorings=(_status_authoring(),),
        actor_id="owner",
        proposal_name="plural-entrypoint",
        timestamp=TIMESTAMP,
    )

    singular_candidate = singular.proposal.proposal.candidate
    plural_candidate = plural.proposal.proposal.candidate
    assert singular_candidate is not None and plural_candidate is not None
    assert plural_candidate.candidate_digest == singular_candidate.candidate_digest
    assert singular.proposal.proposal.admission.candidate_tree_oid == (
        plural.proposal.proposal.admission.candidate_tree_oid
    )

    (authored,) = plural.claims
    assert authored.claim_identity == singular.claim_identity
    assert authored.claim_path == singular.claim_path
    assert authored.statement_digest == singular.statement_digest
    assert authored.artifact_digest == singular.artifact_digest
    assert authored.capture_digest == singular.capture_digest
    assert authored.capture_digests == singular.capture_digests
    assert authored.observed_at == singular.observed_at
    assert authored.existing_statements == singular.existing_statements
    assert authored.handoffs == singular.handoffs


def test_a_distinct_relation_and_the_vocabulary_it_discriminates_settle_together(
    tmp_path: Path,
) -> None:
    """The near-ClaimType reuse law is satisfied inside one change set.

    Splitting these across generations would mean admitting a near-duplicate
    predicate in a generation where nothing yet distinguishes it, which is the
    exact ordering the plural operation exists to remove.
    """

    instance, owner = initialize_local(tmp_path)
    state_type = _claim_type().model_copy(
        update={
            "identity": ArtifactIdentity(kind="ClaimType", name="project.work_item.state"),
            "predicate": "project.work_item.state",
        }
    )
    distinct_type = descriptor_claim_type("semantic.distinct_from")
    distinct_authoring = DirectClaimAuthoringV1(
        statement=ClaimStatement(
            subject=SemanticAddress.whole_artifact("claim-types/project.work_item/state.yaml"),
            claim_type=distinct_type.identity,
            claim_type_digest=claim_type_digest(distinct_type).tagged,
            predicate=distinct_type.predicate,
            object=SubjectClaimObject(
                address=SemanticAddress.whole_artifact("claim-types/project.work_item/status.yaml")
            ),
            role="normative",
        ),
        rationale="State is the broad lifecycle concept; status is its current value.",
        claim_id=DISTINCT_CLAIM_ID,
        claim_type_artifact=distinct_type,
        dependency_claim_types=(state_type,),
    )
    proposed = service_propose_playbill_claims(
        instance,
        authorings=(_status_authoring(), distinct_authoring),
        actor_id="owner",
        proposal_name="status-with-its-distinct-relation",
        timestamp=TIMESTAMP,
    )

    candidate = proposed.proposal.proposal.candidate
    assert candidate is not None
    reuse = next(
        item.result["reuse"]
        for item in candidate.law_evidence
        if item.path == "claim-types/project.work_item/state.yaml"
    )
    assert reuse["verdict"] == "satisfied"
    assert reuse["distinct_relation_members"] == [
        {
            "claim_address": SemanticAddress.claim_statement(
                claim_path(DISTINCT_CLAIM_ID)
            ).model_dump(mode="json"),
            "claim_artifact_digest": proposed.claims[1].artifact_digest,
            "subject": SemanticAddress.whole_artifact(
                "claim-types/project.work_item/state.yaml"
            ).model_dump(mode="json"),
            "object": SemanticAddress.whole_artifact(
                "claim-types/project.work_item/status.yaml"
            ).model_dump(mode="json"),
        }
    ]
    _activate(instance, owner, proposed)
    accepted_tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    assert "claim-types/project.work_item/state.yaml" in accepted_tree
    assert claim_path(DISTINCT_CLAIM_ID) in accepted_tree


def test_an_empty_authoring_set_is_a_typed_refusal(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    with pytest.raises(ProposalIntegrityError, match="at least one Claim"):
        service_propose_playbill_claims(
            instance,
            authorings=(),
            actor_id="owner",
            proposal_name="nothing-at-all",
            timestamp=TIMESTAMP,
        )


def test_two_authorings_may_not_write_the_same_claim_path(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    with pytest.raises(ProposalIntegrityError, match="same Claim path"):
        service_propose_playbill_claims(
            instance,
            authorings=(
                _status_authoring(),
                _summary_authoring(claim_id=STATUS_CLAIM_ID),
            ),
            actor_id="owner",
            proposal_name="colliding-lineages",
            timestamp=TIMESTAMP,
        )


def test_two_authorings_may_not_disagree_on_one_subject(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    conflicting = _summary_authoring().model_copy(
        update={
            "subject_shell": _subject().model_copy(
                update={"lifecycle": _subject().lifecycle.model_copy(update={"state": "retired"})}
            )
        }
    )
    with pytest.raises(ProposalIntegrityError, match="disagree on their Subject bytes"):
        service_propose_playbill_claims(
            instance,
            authorings=(_status_authoring(), conflicting),
            actor_id="owner",
            proposal_name="two-shells-one-path",
            timestamp=TIMESTAMP,
        )


def test_two_authorings_may_not_declare_contradictory_dependency_claim_types(
    tmp_path: Path,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    state_type = _claim_type().model_copy(
        update={
            "identity": ArtifactIdentity(kind="ClaimType", name="project.work_item.state"),
            "predicate": "project.work_item.state",
        }
    )
    divergent_state_type = state_type.model_copy(
        update={"literal_schema": {"enum": ["closed", "open"], "type": "string"}}
    )
    first = _status_authoring().model_copy(update={"dependency_claim_types": (state_type,)})
    second = _summary_authoring().model_copy(
        update={"dependency_claim_types": (divergent_state_type,)}
    )
    with pytest.raises(ProposalIntegrityError, match="dependency ClaimType bytes conflict"):
        service_propose_playbill_claims(
            instance,
            authorings=(first, second),
            actor_id="owner",
            proposal_name="two-vocabularies-one-path",
            timestamp=TIMESTAMP,
        )


def test_a_multi_claim_proposal_from_a_stale_base_rebases_deterministically(
    tmp_path: Path,
) -> None:
    """A stale base engages the ordinary v2 rebase; nothing here is Claim-count specific."""

    instance, owner = initialize_local(tmp_path)
    genesis = PlaybillAcceptedCoordinate.from_internal(instance.accepted_coordinate())
    first = service_propose_playbill_claims(
        instance,
        authorings=(_status_authoring(),),
        actor_id="owner",
        proposal_name="status-first",
        timestamp=TIMESTAMP,
    )
    _activate(instance, owner, first)
    assert instance.accepted_coordinate().git_oid != genesis.git_oid

    stale = service_propose_playbill_claims(
        instance,
        authorings=(
            _summary_authoring(),
            _summary_authoring(claim_id=DISTINCT_CLAIM_ID).model_copy(
                update={
                    "statement": _summary_authoring().statement.model_copy(
                        update={"object": LiteralClaimObject(value="A second summary")}
                    )
                }
            ),
        ),
        actor_id="owner",
        proposal_name="summaries-from-genesis",
        timestamp="2026-08-16T20:05:00.000000Z",
        base=genesis,
    )

    assert stale.proposal.proposal.evaluation.rebased is True
    assert stale.proposal.proposal.evaluation.evaluated_base_oid == (
        instance.accepted_coordinate().git_oid
    )
    candidate = stale.proposal.proposal.candidate
    assert candidate is not None
    rebased_tree = instance.proposal_tree(
        stale.proposal.proposal.evaluation.evaluated_tree_oid or ""
    )
    # The rebase carried the accepted Claim forward beside both new ones.
    assert claim_path(STATUS_CLAIM_ID) in rebased_tree
    assert {item.claim_path for item in stale.claims} <= set(rebased_tree)
