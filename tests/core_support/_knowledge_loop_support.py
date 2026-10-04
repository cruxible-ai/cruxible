"""Shared seeding helpers for the PC-G-S1a knowledge-loop service tests.

Every helper drives the public service surfaces and the accepted activation
path, so the state these tests read is accepted state rather than a fixture.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.captures import (
    DirectForeignSourceSelection,
    capture_contract_digest,
    foreign_source_capture_contract,
)
from cruxible_client.contracts.claim_types import ClaimType, claim_type_digest
from cruxible_client.contracts.claims import ClaimStatement, LiteralClaimObject
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicyV1,
    ClaimEvidenceAdmissionRuleV1,
)
from cruxible_client.contracts.query.definitions import QueryDefinition, QueryEvaluationPolicy
from cruxible_client.contracts.query.grammar import (
    QueryBudgets,
    QueryClaimValueRef,
    QueryEntry,
    QueryProjection,
    QueryProjectionField,
    QuerySubjectFieldRef,
)
from cruxible_client.contracts.semantic import ContentSpan, SemanticAddress
from cruxible_client.contracts.subjects import SubjectShell
from cruxible_core.governance.keys import GeneratedKeyMaterial
from cruxible_core.proposals.settlement import ChangeActorBinding
from cruxible_core.runtime.instance import PlaybillInstance
from tests.core_support._claim_authoring_support import (
    DirectClaimAuthoringV1,
    service_propose_playbill_claim,
)
from tests.core_support._support import (
    TemplateWorld,
    build_inputs,
    client_material,
    initialize_fresh,
    initialize_local,
    template_world,
)
from tests.core_support._world_templates import TEMPLATES
from tests.test_authoring.test_authoring_preflight import _seed_claim_surface
from tests.test_claims.test_claims import _claim_type
from tests.test_ledger.test_activation import _sign

TIMESTAMP = "2026-08-16T20:00:00.000000Z"
EVALUATION_TIME = "2026-08-16T21:00:00+00:00"
SUBJECT_KIND = "project.work_item"
PREDICATE = "project.work_item.status"
QUERY_NAME = "project.work_items"


def subject_shell(subject_id: str) -> SubjectShell:
    """Return one identity-only Subject shell of the work-item kind."""

    return SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name=f"{SUBJECT_KIND}/{subject_id}"),
        subject_kind=SUBJECT_KIND,
        subject_id=subject_id,
    )


def subject_address(subject_id: str) -> SemanticAddress:
    """Return the whole-artifact address of one work-item Subject."""

    return SemanticAddress.whole_artifact(f"subjects/{SUBJECT_KIND}/{subject_id}.json")


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
        approvals=(
            _sign(
                client_material(instance.root.parent, instance),
                candidate.candidate_digest,
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


def seed_claims(
    tmp_path: Path, *, claim_type_override: ClaimType | None = None
) -> tuple[PlaybillInstance, GeneratedKeyMaterial]:
    """Return an instance holding two accepted work-item status Claims.

    Without an override the world is a copy of a session template (see
    `tests/core_support/_world_templates.py`).
    """

    if claim_type_override is None:
        template = TEMPLATES.template(_seeded_shape(), _seeded_template)
        if template is not None:
            opened = template_world(template, tmp_path, warm=True)
            if opened is not None:
                return opened
    instance, owner = initialize_local(tmp_path)
    seed_claims_into(instance, owner, claim_type_override=claim_type_override)
    return instance, owner


def _seeded_shape() -> tuple[object, ...]:
    """The seeded world's key: the build inputs plus every constant seeding reads."""

    return (
        "seeded",
        build_inputs(),
        TIMESTAMP,
        SUBJECT_KIND,
        PREDICATE,
        claim_type_digest(_claim_type()).tagged,
    )


def _seeded_template(root: Path) -> TemplateWorld:
    instance, owner = initialize_fresh(root)
    seed_claims_into(instance, owner)
    return TemplateWorld.capture(instance, owner)


def seed_claims_into(
    instance: PlaybillInstance,
    owner: GeneratedKeyMaterial,
    *,
    claim_type_override: ClaimType | None = None,
) -> None:
    """Accept the Claim surface and two work-item status Claims into ``instance``."""

    source_id = "fixture.work-items"
    _seed_claim_surface(
        instance,
        owner,
        contract=foreign_source_capture_contract(source_id),
        claim_type_override=claim_type_override,
    )
    first_body = instance.body_store().store(b"status: ready")
    first = service_propose_playbill_claim(
        instance,
        authoring=authoring("wi-42", "ready", with_claim_type=False).model_copy(
            update={
                "source_selection": DirectForeignSourceSelection(
                    logical_source_identity=source_id,
                    span=ContentSpan(
                        content_digest=first_body.digest,
                        start_byte=0,
                        end_byte=len(b"status: ready"),
                    ),
                )
            }
        ),
        actor_id="owner",
        proposal_name="seed-first",
        timestamp=TIMESTAMP,
    )
    activate(instance, owner, first)
    second_body = instance.body_store().store(b"status: blocked")
    second = service_propose_playbill_claim(
        instance,
        authoring=authoring("wi-43", "blocked", with_claim_type=False).model_copy(
            update={
                "source_selection": DirectForeignSourceSelection(
                    logical_source_identity=source_id,
                    span=ContentSpan(
                        content_digest=second_body.digest,
                        start_byte=0,
                        end_byte=len(b"status: blocked"),
                    ),
                )
            }
        ),
        actor_id="owner",
        proposal_name="seed-second",
        timestamp=TIMESTAMP,
    )
    activate(instance, owner, second)


def work_item_query(
    name: str = QUERY_NAME,
    *,
    claim_type: ClaimType | None = None,
) -> QueryDefinition:
    """Return one many-cardinality Subject read over every accepted work item."""

    if claim_type is None:
        contract = foreign_source_capture_contract("fixture.work-items")
        claim_type = _claim_type().model_copy(
            update={
                "evidence_admission_policy": ClaimEvidenceAdmissionPolicyV1(
                    rules=(
                        ClaimEvidenceAdmissionRuleV1(
                            rule_id="coordinator-source",
                            claim_roles=("normative", "observation"),
                            capture_contract_digests=(capture_contract_digest(contract).tagged,),
                            evidence_kinds=("self_asserted",),
                            admission="direct",
                            subject_binding="exact_claim_subject",
                        ),
                    )
                )
            }
        )
    return QueryDefinition(
        identity=ArtifactIdentity(kind="QueryDefinition", name=name),
        entry=QueryEntry(binding="item", subject_kinds=(SUBJECT_KIND,)),
        result_binding="item",
        result_shape="subject",
        result_cardinality="many",
        dedupe="subject",
        projection=QueryProjection(
            fields=(
                QueryProjectionField(
                    name="item_id",
                    value=QuerySubjectFieldRef(binding="item", field="subject_id"),
                ),
                QueryProjectionField(
                    name="status",
                    value=QueryClaimValueRef(binding="item", predicate=PREDICATE),
                ),
            )
        ),
        evaluation_policy=QueryEvaluationPolicy(
            visible_verdicts=("supported",),
            visible_currency=("current",),
            conflict_behavior="surface_conflicts",
        ),
        default_budgets=QueryBudgets(max_results=10, max_traversal_depth=0),
        maximum_budgets=QueryBudgets(max_results=50, max_traversal_depth=0),
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
) -> None:
    """Approve and activate one generic (non-Claim) proposal inspection."""

    base = instance.accepted_coordinate()
    candidate = inspection.proposal.candidate
    assert candidate is not None, inspection.proposal.evaluation.diagnostics
    evaluated_oid = inspection.proposal.evaluation.evaluated_tree_oid
    assert evaluated_oid is not None
    bundle = instance.prepare_generation(
        base=base,
        candidate_tree=instance.proposal_tree(evaluated_oid),
        candidate=candidate,
        approvals=(
            _sign(
                client_material(instance.root.parent, instance),
                candidate.candidate_digest,
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
