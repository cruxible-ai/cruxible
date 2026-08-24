"""Shared seeding helpers for the PC-G-S1a knowledge-loop service tests.

Every helper drives the public service surfaces and the accepted activation
path, so the state these tests read is accepted state rather than a fixture.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cruxible_client.contracts.artifacts import ArtifactAuthority, ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.claim_types import ClaimType, claim_type_digest
from cruxible_client.contracts.claims import ClaimStatement, LiteralClaimObject
from cruxible_client.contracts.query.definitions import QueryDefinitionV1, QueryEvaluationPolicyV1
from cruxible_client.contracts.query.grammar import (
    QueryBudgetsV1,
    QueryClaimValueRefV1,
    QueryEntryV1,
    QueryProjectionFieldV1,
    QueryProjectionV1,
    QuerySubjectFieldRefV1,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import SubjectShell
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.keys import GeneratedKeyMaterial
from cruxible_core.playbill.settlement import ChangeActorBinding
from cruxible_core.service.playbill_claims import (
    DirectClaimAuthoringV1,
    service_propose_playbill_claim,
)
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_claims import _claim_type

TIMESTAMP = "2026-08-16T20:00:00.000000Z"
EVALUATION_TIME = "2026-08-16T21:00:00+00:00"
SUBJECT_KIND = "project.work_item"
PREDICATE = "project.work_item.status"
QUERY_NAME = "project.work_items"
AUTHORITY = ArtifactAuthority(propose_roles=("owner",), approve_roles=("owner",))


def subject_shell(subject_id: str) -> SubjectShell:
    """Return one identity-only Subject shell of the work-item kind."""

    return SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name=f"{SUBJECT_KIND}/{subject_id}"),
        subject_kind=SUBJECT_KIND,
        subject_id=subject_id,
        authority=AUTHORITY,
    )


def subject_address(subject_id: str) -> SemanticAddress:
    """Return the whole-artifact address of one work-item Subject."""

    return SemanticAddress.whole_artifact(f"subjects/{SUBJECT_KIND}/{subject_id}.yaml")


def authoring(subject_id: str, value: str, *, with_claim_type: bool) -> DirectClaimAuthoringV1:
    """Return one direct-authoring request for a work-item status Claim."""

    claim_type = _claim_type()
    return DirectClaimAuthoringV1(
        statement=ClaimStatement(
            subject=subject_address(subject_id),
            claim_type=claim_type.identity,
            claim_type_digest=claim_type_digest(claim_type).tagged,
            predicate=claim_type.predicate,
            object=LiteralClaimObject(value=value),
            role="observation",
        ),
        rationale=f"The reviewed status of {subject_id} is {value}.",
        subject_shell=subject_shell(subject_id),
        claim_type_artifact=claim_type if with_claim_type else None,
    )


def activate(
    instance: PlaybillInstance,
    owner: GeneratedKeyMaterial,
    proposed: Any,
    *,
    sequence: int,
) -> None:
    """Approve and activate one direct-Claim proposal at the accepted head."""

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


def seed_claims(tmp_path: Path) -> tuple[PlaybillInstance, GeneratedKeyMaterial]:
    """Return an instance holding two accepted work-item status Claims."""

    instance, owner = initialize_local(tmp_path)
    first = service_propose_playbill_claim(
        instance,
        authoring=authoring("wi-42", "ready", with_claim_type=True),
        actor_id="owner",
        proposal_name="seed-first",
        timestamp=TIMESTAMP,
    )
    activate(instance, owner, first, sequence=1)
    second = service_propose_playbill_claim(
        instance,
        authoring=authoring("wi-43", "blocked", with_claim_type=False),
        actor_id="owner",
        proposal_name="seed-second",
        timestamp=TIMESTAMP,
    )
    activate(instance, owner, second, sequence=2)
    return instance, owner


def work_item_query(name: str = QUERY_NAME) -> QueryDefinitionV1:
    """Return one many-cardinality Subject read over every accepted work item."""

    claim_type: ClaimType = _claim_type()
    return QueryDefinitionV1(
        identity=ArtifactIdentity(kind="QueryDefinition", name=name),
        entry=QueryEntryV1(binding="item", subject_kinds=(SUBJECT_KIND,)),
        result_binding="item",
        result_shape="subject",
        result_cardinality="many",
        dedupe="subject",
        projection=QueryProjectionV1(
            fields=(
                QueryProjectionFieldV1(
                    name="item_id",
                    value=QuerySubjectFieldRefV1(binding="item", field="subject_id"),
                ),
                QueryProjectionFieldV1(
                    name="status",
                    value=QueryClaimValueRefV1(binding="item", predicate=PREDICATE),
                ),
            )
        ),
        evaluation_policy=QueryEvaluationPolicyV1(
            visible_verdicts=("supported",),
            visible_currency=("current",),
            conflict_behavior="surface_conflicts",
        ),
        default_budgets=QueryBudgetsV1(max_results=10, max_traversal_depth=0),
        maximum_budgets=QueryBudgetsV1(max_results=50, max_traversal_depth=0),
        authority=AUTHORITY,
        pins=(
            ArtifactPin(
                role="claim-type",
                target=claim_type.identity,
                artifact_digest=claim_type_digest(claim_type).tagged,
            ),
        ),
    )


def accept_proposal(
    instance: PlaybillInstance,
    owner: GeneratedKeyMaterial,
    inspection: Any,
    *,
    sequence: int,
) -> None:
    """Approve and activate one generic (non-Claim) proposal inspection."""

    base = instance.accepted_coordinate()
    candidate = inspection.proposal.candidate
    assert candidate is not None
    evaluated_oid = inspection.proposal.evaluation.evaluated_tree_oid
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
