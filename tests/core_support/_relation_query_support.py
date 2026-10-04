"""Accepted relation Claims for the ``query`` reverse-follow tests.

On top of ``seed_claims`` (work items ``wi-42`` and ``wi-43``), this accepts a
fixture equivalent of the project instance's "which batches deliver this
roadmap item" shape:

- ``project.batch.delivers`` (many, ``project.batch`` -> ``project.work_item``):
  ``b-1`` and ``b-2`` deliver ``wi-42``; ``b-1`` also delivers ``wi-43``;
- ``project.batch.state`` (one, enum): ``b-1`` open, ``b-2`` closed, ``b-3`` open
  (``b-3`` delivers nothing);
- ``project.decision.governs`` (many, ``project.decision`` -> ``project.work_item``):
  ``d-1`` governs ``wi-43``;
- ``project.work_item.parent`` (one, ``project.work_item`` -> itself): ``wi-43``'s
  parent is ``wi-42``.

Every Claim travels the sanctioned authoring path and is accepted.
"""

from __future__ import annotations

from typing import Any

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.claim_types import (
    ClaimType,
    claim_type_digest,
    claim_type_path,
    render_claim_type,
)
from cruxible_client.contracts.claims import (
    ClaimStatement,
    LiteralClaimObject,
    SubjectClaimObject,
)
from cruxible_client.contracts.policies import ClaimResolutionPolicy
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import SubjectShell, render_subject, subject_path
from cruxible_core.governance.keys import GeneratedKeyMaterial
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.proposals.settlement import ChangeActorBinding
from cruxible_core.runtime.instance import PlaybillInstance
from tests.core_support._claim_authoring_support import (
    DirectClaimAuthoringV1,
    service_propose_playbill_claim,
)
from tests.core_support._knowledge_loop_support import PREDICATE, SUBJECT_KIND, activate
from tests.core_support._support import client_material
from tests.test_ledger.test_activation import _sign

BATCH_KIND = "project.batch"
DECISION_KIND = "project.decision"
DELIVERS = "project.batch.delivers"
BATCH_STATE = "project.batch.state"
GOVERNS = "project.decision.governs"
PARENT = "project.work_item.parent"
_TIMESTAMP = "2026-08-16T20:30:00.000000Z"


def _relation(template: ClaimType, predicate: str, *, source: str, many: bool) -> ClaimType:
    cardinality = "many" if many else "one"
    return template.model_copy(
        update={
            "identity": ArtifactIdentity(kind="ClaimType", name=predicate),
            "predicate": predicate,
            "allowed_subject_kinds": (source,),
            "object_kind": "subject",
            "literal_schema": None,
            "allowed_object_subject_kinds": (SUBJECT_KIND,),
            "cardinality": cardinality,
            "resolution_policy": ClaimResolutionPolicy(
                cardinality=cardinality,
                eligible_verdicts=("supported",),
                selector="all" if many else "only_contender",
            ),
        }
    )


def _claim_types(template: ClaimType) -> tuple[ClaimType, ...]:
    state = template.model_copy(
        update={
            "identity": ArtifactIdentity(kind="ClaimType", name=BATCH_STATE),
            "predicate": BATCH_STATE,
            "allowed_subject_kinds": (BATCH_KIND,),
            "literal_schema": {"enum": ["closed", "open"], "type": "string"},
        }
    )
    return (
        _relation(template, DELIVERS, source=BATCH_KIND, many=True),
        state,
        _relation(template, GOVERNS, source=DECISION_KIND, many=True),
        _relation(template, PARENT, source=SUBJECT_KIND, many=False),
    )


def _shell(kind: str, subject_id: str) -> SubjectShell:
    return SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name=f"{kind}/{subject_id}"),
        subject_kind=kind,
        subject_id=subject_id,
    )


def _accept_vocabulary(instance: PlaybillInstance, claim_types: tuple[ClaimType, ...]) -> None:
    base = instance.accepted_coordinate()
    tree = instance.tree_at(base.git_oid)
    for claim_type in claim_types:
        tree[claim_type_path(claim_type.predicate)] = render_claim_type(claim_type)
    for kind, subject_id in (
        (BATCH_KIND, "b-1"),
        (BATCH_KIND, "b-2"),
        (BATCH_KIND, "b-3"),
        (DECISION_KIND, "d-1"),
    ):
        tree[subject_path(kind, subject_id)] = render_subject(_shell(kind, subject_id))
    proposed = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/relation-vocabulary",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=tree,
        timestamp=_TIMESTAMP,
    )
    assert proposed.candidate is not None, proposed.evaluation
    assert proposed.evaluation.evaluated_tree_oid is not None
    bundle = instance.prepare_generation(
        base=base,
        candidate_tree=instance.proposal_tree(proposed.evaluation.evaluated_tree_oid),
        candidate=proposed.candidate,
        approvals=(
            _sign(
                client_material(instance.root.parent, instance),
                proposed.candidate.candidate_digest,
                base.semantic_root,
            ),
        ),
        actor_binding=ChangeActorBinding(actor_id="owner"),
        proposal_actor_id="owner",
        sequence=len(instance.accepted_history()),
    )
    publisher = instance.activation_publisher()
    projection = publisher.prebuild(bundle, base=base)
    assert publisher.activate(bundle, projection, base=base).status == "accepted"
    instance.refresh()


def _claim(
    claim_type: ClaimType, subject: tuple[str, str], value: tuple[str, str] | str
) -> DirectClaimAuthoringV1:
    kind, subject_id = subject
    obj: Any
    if isinstance(value, tuple):
        obj = SubjectClaimObject(address=SemanticAddress.whole_artifact(subject_path(*value)))
    else:
        obj = LiteralClaimObject(value=value)
    return DirectClaimAuthoringV1(
        statement=ClaimStatement(
            subject=SemanticAddress.whole_artifact(subject_path(kind, subject_id)),
            claim_type=claim_type.identity,
            claim_type_digest=claim_type_digest(claim_type).tagged,
            predicate=claim_type.predicate,
            object=obj,
            role="observation",
        ),
        rationale=f"Reviewed: {kind}/{subject_id} {claim_type.predicate} {value}.",
    )


def seed_relations(instance: PlaybillInstance, owner: GeneratedKeyMaterial) -> None:
    """Accept the relation vocabulary, Subjects and Claims into a seeded instance."""

    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        template = projection.typed.source(f"ClaimType:{PREDICATE}")
    assert isinstance(template, ClaimType)
    delivers, state, governs, parent = _claim_types(template)
    _accept_vocabulary(instance, (delivers, state, governs, parent))
    work = SUBJECT_KIND
    claims = (
        _claim(delivers, (BATCH_KIND, "b-1"), (work, "wi-42")),
        _claim(delivers, (BATCH_KIND, "b-2"), (work, "wi-42")),
        _claim(delivers, (BATCH_KIND, "b-1"), (work, "wi-43")),
        _claim(state, (BATCH_KIND, "b-1"), "open"),
        _claim(state, (BATCH_KIND, "b-2"), "closed"),
        _claim(state, (BATCH_KIND, "b-3"), "open"),
        _claim(governs, (DECISION_KIND, "d-1"), (work, "wi-43")),
        _claim(parent, (work, "wi-43"), (work, "wi-42")),
    )
    for index, authoring in enumerate(claims):
        activate(
            instance,
            owner,
            service_propose_playbill_claim(
                instance,
                authoring=authoring,
                actor_id="owner",
                proposal_name=f"relation-{index}",
                timestamp=f"2026-08-16T20:3{index}:00.000000Z",
            ),
        )


__all__ = [
    "BATCH_KIND",
    "BATCH_STATE",
    "DECISION_KIND",
    "DELIVERS",
    "GOVERNS",
    "PARENT",
    "seed_relations",
]
