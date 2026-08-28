"""Test-only Claim seeding through the sanctioned AuthoringIntent coordinator.

The pre-0.4 direct Claim proposal surface was retired in PC-DEL3.  These small
adapters let semantic/read tests keep concise fixture declarations while every
candidate they create now travels through the current coordinator and Claim-v2
lowering path.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.authoring.models import (
    AuthoringClaimStatementV1,
    AuthoringExistingClaimDispositionV1,
    ClaimAuthoringPayloadV1,
    ClaimAuthoringPayloadV2,
    ClaimDependencyDraftsV1,
    SelfSourceBodyV1,
    WorkingAnchorWindowV1,
    WorkingDigestCoordinateV1,
    WorkingSelectionObservationV1,
)
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    DirectClaimSelectionV1,
    DirectForeignSourceSelectionV1,
    capture_contract_digest,
    capture_contract_path,
    foreign_source_capture_contract,
)
from cruxible_client.contracts.claim_types import ClaimType, claim_type_digest
from cruxible_client.contracts.claims import (
    ClaimArtifactV2,
    ClaimStatement,
    LiteralClaimObject,
    claim_artifact_digest,
    claim_path,
    claim_statement_digest,
    new_claim_id,
    parse_claim,
)
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicyV1,
    ClaimEvidenceAdmissionRuleV1,
)
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import SubjectShell
from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.playbill.service.documents import (
    PlaybillAcceptedCoordinate,
    PlaybillProposalInspection,
    service_inspect_playbill_proposal,
)
from cruxible_core.playbill.settlement import ChangeActorBinding
from tests.test_playbill._support import client_material
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_claims import _claim_type, _subject

TIMESTAMP = "2026-08-16T20:00:00.000000Z"
STATUS_CLAIM_ID = "CLM-" + "1" * 32
SUMMARY_CLAIM_ID = "CLM-" + "2" * 32


class _StrictFixtureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExistingStatementHandoffV1(_StrictFixtureModel):
    statement_digest: str
    disposition: Literal["not_tested", "support", "contradict", "unsure"]


class DirectClaimAuthoringV1(_StrictFixtureModel):
    """Old fixture spelling, lowered only through AuthoringIntent."""

    statement: ClaimStatement
    rationale: str
    claim_id: str | None = None
    predecessor_artifact_digest: str | None = None
    retire: bool = False
    materialize_source: bool = True
    source_selection: DirectClaimSelectionV1 | None = None
    subject_shell: SubjectShell | None = None
    claim_type_artifact: ClaimType | None = None
    dependency_subject_shells: tuple[SubjectShell, ...] = ()
    dependency_claim_types: tuple[ClaimType, ...] = ()
    existing_statement_handoffs: tuple[ExistingStatementHandoffV1, ...] = ()


@dataclass(frozen=True)
class AuthoredClaimV1:
    claim_identity: str
    claim_path: str
    statement_digest: str
    artifact_digest: str
    capture_digest: str
    capture_digests: tuple[str, ...]
    observed_at: object
    existing_statements: tuple[object, ...] = ()
    handoffs: tuple[ExistingStatementHandoffV1, ...] = ()
    warnings: tuple[object, ...] = ()


@dataclass(frozen=True)
class DirectClaimProposalV1(AuthoredClaimV1):
    proposal: PlaybillProposalInspection | None = None


def _dispositions(
    instance: PlaybillInstance,
    authoring: DirectClaimAuthoringV1,
) -> tuple[AuthoringExistingClaimDispositionV1, ...]:
    by_statement: dict[str, str] = {}
    same_slot: dict[
        str,
        Literal["not_tested", "support", "contradict", "unsure"],
    ] = {}
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    for path, content in tree.items():
        if not path.startswith("claims/"):
            continue
        claim = parse_claim(content, path=path)
        by_statement[claim_statement_digest(claim.statement).tagged] = claim.identity.name
        if (
            claim.lifecycle.state == "live"
            and claim.statement.subject == authoring.statement.subject
            and claim.statement.predicate == authoring.statement.predicate
        ):
            same_slot[claim.identity.name] = "not_tested"
    for item in authoring.existing_statement_handoffs:
        same_slot[by_statement[item.statement_digest]] = item.disposition
    return tuple(
        sorted(
            (
                AuthoringExistingClaimDispositionV1(
                    claim_id=claim_id,
                    disposition=disposition,
                )
                for claim_id, disposition in same_slot.items()
            ),
            key=lambda item: item.claim_id.encode("ascii"),
        )
    )


def _payload(
    instance: PlaybillInstance,
    authoring: DirectClaimAuthoringV1,
) -> ClaimAuthoringPayloadV1 | ClaimAuthoringPayloadV2:
    if authoring.retire:
        raise ValueError("retirement fixtures must use playbill.claim.retire")
    if authoring.dependency_subject_shells or authoring.dependency_claim_types:
        raise ValueError("one-Claim dependency drafts accept only the Claim's direct dependencies")
    statement = authoring.statement
    source = authoring.source_selection
    citation_role: Literal["evidence"] | None = None
    if isinstance(source, DirectForeignSourceSelectionV1):
        content = instance.body_store().read(
            source.span.content_digest,
            access=BodyAccessContext(principal_id="claim-fixture", can_read_body=True),
        )
        selected = content[source.span.start_byte : source.span.end_byte]
        anchor = content.decode("utf-8").strip()
        selected_digest = "sha256:" + hashlib.sha256(selected).hexdigest()
        source_value = WorkingSelectionObservationV1(
            source_id=source.logical_source_identity,
            coordinate=WorkingDigestCoordinateV1(
                source_content_digest="sha256:" + hashlib.sha256(content).hexdigest(),
                source_byte_length=len(content),
            ),
            selected_content_base64=base64.b64encode(selected).decode("ascii"),
            selected_bytes_digest=selected_digest,
            selector=WorkingAnchorWindowV1(
                anchor=anchor,
                start_byte=source.span.start_byte,
                end_byte=source.span.end_byte,
                observed_occurrence_count=1,
            ),
        )
        citation_role = "evidence"
    elif source is not None:
        raise ValueError("CAS-only fixture selections have no sanctioned Flow-A source identity")
    else:
        fixture_source_id = "fixture.work-items"
        fixture_contract = foreign_source_capture_contract(fixture_source_id)
        accepted_tree = instance.tree_at(instance.accepted_coordinate().git_oid)
        if capture_contract_path(fixture_contract.identity.name) in accepted_tree:
            selected = statement.object.model_dump_json().encode("utf-8")
            digest = "sha256:" + hashlib.sha256(selected).hexdigest()
            source_value = WorkingSelectionObservationV1(
                source_id=fixture_source_id,
                coordinate=WorkingDigestCoordinateV1(
                    source_content_digest=digest,
                    source_byte_length=len(selected),
                ),
                selected_content_base64=base64.b64encode(selected).decode("ascii"),
                selected_bytes_digest=digest,
                selector=WorkingAnchorWindowV1(
                    anchor=selected.decode("utf-8"),
                    start_byte=0,
                    end_byte=len(selected),
                    observed_occurrence_count=1,
                ),
            )
            citation_role = "evidence"
        else:
            source_value = SelfSourceBodyV1(
                content_base64=base64.b64encode(
                    statement.object.model_dump_json().encode("utf-8")
                ).decode("ascii")
            )
    base = {
        "statement": AuthoringClaimStatementV1(
            subject=statement.subject,
            predicate=statement.predicate,
            qualifier=statement.qualifier,
            object=statement.object,
            role=statement.role,
            effective_from=statement.effective_from,
            effective_until=statement.effective_until,
        ),
        "rationale": authoring.rationale,
        "source": source_value,
        "citation_role": citation_role,
        "claim_ref": (
            authoring.claim_id
            if authoring.claim_id is not None
            and claim_path(authoring.claim_id)
            in instance.tree_at(instance.accepted_coordinate().git_oid)
            else None
        ),
        "existing_claim_dispositions": _dispositions(instance, authoring),
    }
    if authoring.subject_shell is None and authoring.claim_type_artifact is None:
        return ClaimAuthoringPayloadV1(**base)
    claim_type = authoring.claim_type_artifact
    if claim_type is not None and source is None:
        claim_type = claim_type.model_copy(
            update={
                "evidence_admission_policy": ClaimEvidenceAdmissionPolicyV1(
                    rules=(
                        ClaimEvidenceAdmissionRuleV1(
                            rule_id="coordinator-source",
                            claim_roles=tuple(
                                role
                                for role in claim_type.permitted_roles
                                if role in {"normative", "observation", "derivation"}
                            ),
                            capture_contract_digests=(
                                capture_contract_digest(
                                    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT
                                ).tagged,
                            ),
                            evidence_kinds=("self_asserted",),
                            admission="direct",
                            subject_binding="exact_claim_subject",
                        ),
                    )
                )
            }
        )
    return ClaimAuthoringPayloadV2(
        **base,
        dependency_drafts=ClaimDependencyDraftsV1(
            subject=authoring.subject_shell,
            claim_type=claim_type,
        ),
    )


def service_propose_playbill_claim(
    instance: PlaybillInstance,
    *,
    authoring: DirectClaimAuthoringV1,
    actor_id: str,
    proposal_name: str,
    timestamp: str,
    base: PlaybillAcceptedCoordinate | None = None,
) -> DirectClaimProposalV1:
    del proposal_name
    coordinator = AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentCoordinator.for_instance(instance).store,
        claim_id_factory=lambda: authoring.claim_id or new_claim_id(),
    )
    actor = AuthenticatedActor(actor_id=actor_id)
    created = coordinator.create(
        actor=actor,
        payload=_payload(instance, authoring),
        canonical_timestamp=timestamp,
        base_coordinate=base,
    )
    submitted = coordinator.submit(created.intent.intent_id, actor=actor)
    if submitted.status.proposal_id is None:
        raise AssertionError(submitted.intent.last_preflight)
    inspection = service_inspect_playbill_proposal(
        instance,
        proposal_id=submitted.status.proposal_id,
    )
    evaluated_oid = inspection.proposal.evaluation.evaluated_tree_oid
    if evaluated_oid is None:
        raise AssertionError(inspection.proposal.evaluation.diagnostics)
    identity = submitted.intent.semantic_identity
    path = claim_path(identity)
    claim = parse_claim(instance.proposal_tree(evaluated_oid)[path], path=path)
    if not isinstance(claim, ClaimArtifactV2):
        raise AssertionError("AuthoringIntent fixture did not lower to Claim v2")
    captures = tuple(item.capture_digest for item in claim.backing.citations)
    return DirectClaimProposalV1(
        proposal=inspection,
        claim_identity=claim.identity.qualified,
        claim_path=path,
        statement_digest=claim_statement_digest(claim.statement).tagged,
        artifact_digest=claim_artifact_digest(claim).tagged,
        capture_digest=captures[0],
        capture_digests=captures,
        observed_at=submitted.intent.canonical_timestamp,
        handoffs=authoring.existing_statement_handoffs,
    )


def _authoring() -> DirectClaimAuthoringV1:
    shell = _subject()
    claim_type = _claim_type()
    return DirectClaimAuthoringV1(
        statement=ClaimStatement(
            subject=SemanticAddress.whole_artifact(
                f"subjects/{shell.subject_kind}/{shell.subject_id}.yaml"
            ),
            claim_type=claim_type.identity,
            claim_type_digest=claim_type_digest(claim_type).tagged,
            predicate=claim_type.predicate,
            object=LiteralClaimObject(value="ready"),
            role="observation",
        ),
        rationale="The review inputs are complete.",
        subject_shell=shell,
        claim_type_artifact=claim_type,
    )


def _summary_claim_type() -> ClaimType:
    return _claim_type().model_copy(
        update={
            "identity": ArtifactIdentity(kind="ClaimType", name="project.work_item.summary"),
            "predicate": "project.work_item.summary",
            "literal_schema": {"type": "string"},
        }
    )


def _status_authoring(*, claim_id: str | None = STATUS_CLAIM_ID) -> DirectClaimAuthoringV1:
    return _authoring().model_copy(update={"claim_id": claim_id})


def _summary_authoring(*, claim_id: str | None = SUMMARY_CLAIM_ID) -> DirectClaimAuthoringV1:
    shell = _subject()
    claim_type = _summary_claim_type()
    return DirectClaimAuthoringV1(
        statement=ClaimStatement(
            subject=SemanticAddress.whole_artifact(
                f"subjects/{shell.subject_kind}/{shell.subject_id}.yaml"
            ),
            claim_type=claim_type.identity,
            claim_type_digest=claim_type_digest(claim_type).tagged,
            predicate=claim_type.predicate,
            object=LiteralClaimObject(value="Ship the review surface"),
            role="observation",
        ),
        rationale="The summary is the one line the work item is tracked by.",
        claim_id=claim_id,
        subject_shell=shell,
        claim_type_artifact=claim_type,
    )


def _activate_direct_claim(
    instance: PlaybillInstance,
    _owner: object,
    proposed: object,
) -> None:
    inspection = proposed.proposal
    candidate = inspection.proposal.candidate
    evaluated_oid = inspection.proposal.evaluation.evaluated_tree_oid
    assert candidate is not None and evaluated_oid is not None
    base = instance.accepted_coordinate()
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
