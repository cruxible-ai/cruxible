"""Pair Subjects accepted through the ordinary proposal and activation path.

Before authoring and the write verbs create pairs (slice S2), a world writes
them raw: the ends and the pair are keyed first holders (a raw proposal may
create those), each endpoint Claim names its end as a Subject object, and each
Claim cites a coordinator self-source Capture, as an authored Claim does.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin, ArtifactRef
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    DirectClaimSource,
    build_coordinator_self_source_capture,
    builtin_contract_for_write,
    capture_contract_digest,
    capture_contract_path,
    render_capture_contract,
)
from cruxible_client.contracts.claim_types import (
    ClaimType,
    claim_type_digest,
    claim_type_path,
    render_claim_type,
)
from cruxible_client.contracts.claims import (
    ClaimArtifact,
    ClaimBacking,
    ClaimReferentContext,
    ClaimStatement,
    SubjectClaimObject,
    build_claim_citation,
    claim_path,
    claim_statement_address,
    render_claim,
)
from cruxible_client.contracts.policies import (
    CAPTURE_CONTRACT_REF_ROLE,
    ClaimEvidenceAdmissionPolicy,
    ClaimEvidenceAdmissionRule,
)
from cruxible_client.contracts.semantic import ContentSpan, SemanticAddress, SourceMapping
from cruxible_client.contracts.subject_kinds import render_subject_kind, subject_kind_path
from cruxible_client.contracts.subjects import (
    SubjectShell,
    parse_subject,
    render_subject,
    subject_digest,
)
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
from tests.core_support._pair_support import (
    COMPONENT,
    E_COMPONENT,
    E_VULN,
    EXPOSURE,
    KINDS,
    PURL,
    SCOPE,
    VID,
    VULN,
    literal,
    path_of,
    shell,
    to,
)
from tests.core_support._support import initialize_local
from tests.support.claim_types import claim_type as build_claim_type

SELF_SOURCE = builtin_contract_for_write(COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT, None)
OBSERVED_AT = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
_ROLES = ("observation",)


def _self_evidence() -> ClaimEvidenceAdmissionPolicy:
    return ClaimEvidenceAdmissionPolicy(
        rules=(
            ClaimEvidenceAdmissionRule(
                rule_id="self",
                claim_roles=_ROLES,
                capture_contracts=(
                    ArtifactRef(role=CAPTURE_CONTRACT_REF_ROLE, target=SELF_SOURCE.identity),
                ),
                admission="direct",
                subject_binding="exact_claim_subject",
            ),
        )
    )


def _type(predicate: str, value: Any = None) -> ClaimType:
    return build_claim_type(
        predicate,
        value_type={"type": "string"} if value is None else value,
        permitted_roles=_ROLES,
        evidence_admission_policy=_self_evidence(),
        revision_evidence="replace",
    )


REAL_TYPES = {
    item.predicate: item
    for item in (
        _type(PURL),
        _type(VID),
        _type(E_COMPONENT, (COMPONENT,)),
        _type(E_VULN, (VULN,)),
        _type(SCOPE),
    )
}


def real_claim(
    instance: PlaybillInstance,
    tree: dict[str, bytes],
    number: int,
    predicate: str,
    obj: Any,
    *,
    subject: str,
) -> tuple[str, bytes]:
    """One Claim citing its own self-source Capture, about a Subject in ``tree``."""

    claim_type = REAL_TYPES[predicate]
    claim_id = f"CLM-{number:032x}"
    path = claim_path(claim_id)
    shell_artifact: SubjectShell = parse_subject(tree[subject], path=subject)
    capture = build_coordinator_self_source_capture(
        store=instance.body_store(),
        accepted_contract=SELF_SOURCE,
        actor_id="owner",
        claim_id=claim_id,
        body=canonical_bytes(
            DirectClaimSource(
                authored_by="owner",
                claim_id=claim_id,
                value=obj.model_dump(mode="json"),
                rationale="Stated about a pair Subject.",
            ).model_dump(mode="json")
        ),
        observed_at=OBSERVED_AT,
        accepted_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
    )
    length = capture.envelope.commitment.byte_length
    assert length is not None
    object_shell: SubjectShell | None = None
    if isinstance(obj, SubjectClaimObject):
        at = obj.address.artifact_path
        object_shell = parse_subject(tree[at], path=at)
    pins = [
        ArtifactPin(
            role="capture-contract",
            target=SELF_SOURCE.identity,
            artifact_digest=capture_contract_digest(SELF_SOURCE).tagged,
        ),
        ArtifactPin(
            role="claim-type",
            target=claim_type.identity,
            artifact_digest=claim_type_digest(claim_type).tagged,
        ),
        ArtifactPin(
            role="subject",
            target=shell_artifact.identity,
            artifact_digest=subject_digest(shell_artifact).tagged,
        ),
    ]
    if object_shell is not None:
        pins.append(
            ArtifactPin(
                role="object-subject",
                target=object_shell.identity,
                artifact_digest=subject_digest(object_shell).tagged,
            )
        )
    artifact = ClaimArtifact(
        identity=ArtifactIdentity(kind="Claim", name=claim_id),
        statement=ClaimStatement(
            subject=SemanticAddress.whole_artifact(subject),
            claim_type=claim_type.identity,
            claim_type_digest=claim_type_digest(claim_type).tagged,
            predicate=predicate,
            object=obj,
            role="observation",
        ),
        backing=ClaimBacking(
            referent_context=ClaimReferentContext(
                subject_content_digest=subject_digest(shell_artifact).tagged,
                object_content_digest=(
                    None if object_shell is None else subject_digest(object_shell).tagged
                ),
                observed_at=OBSERVED_AT,
            ),
            capture_digests=(capture.capture_digest,),
            citations=(
                build_claim_citation(
                    ArtifactIdentity(kind="Claim", name=claim_id),
                    capture_digest=capture.capture_digest,
                    role="evidence",
                    origin="self_source",
                ),
            ),
            source_mappings=(
                SourceMapping(
                    subject=claim_statement_address(path),
                    spans=(
                        ContentSpan(
                            content_digest=capture.commitment_digest,
                            start_byte=0,
                            end_byte=length,
                        ),
                    ),
                ),
            ),
        ),
        pins=tuple(
            sorted(pins, key=lambda item: (item.role.encode(), item.target.qualified.encode()))
        ),
    )
    return path, render_claim(artifact)


def accept_raw(instance: PlaybillInstance, updates: dict[str, bytes], *, name: str) -> Any:
    """Submit ``updates`` over the head as a raw proposal; activate it when admitted."""

    base = instance.accepted_coordinate()
    proposed = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/owner/{name}", proposed_base_oid=base.git_oid
        ),
        candidate_tree={**instance.tree_at(base.git_oid), **updates},
        timestamp="2026-11-06T12:00:00.000000Z",
    )
    if proposed.candidate is None:
        return proposed
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=proposed.admission.proposal_id, activated_by="owner"
    )
    assert receipt.status == "accepted", receipt
    return proposed


def pair_instance(tmp_path: Path, *, pairs: int = 2) -> tuple[PlaybillInstance, dict[str, str]]:
    """An instance holding ``pairs`` exposures of one component, each with a live scope."""

    instance, _owner = initialize_local(tmp_path)
    vocabulary: dict[str, bytes] = {
        capture_contract_path(SELF_SOURCE.identity.name): render_capture_contract(SELF_SOURCE)
    }
    for item in REAL_TYPES.values():
        vocabulary[claim_type_path(item.predicate)] = render_claim_type(item)
    for kind in (COMPONENT, VULN, EXPOSURE):
        vocabulary[subject_kind_path(kind)] = render_subject_kind(KINDS[kind])
    accepted = accept_raw(instance, vocabulary, name="pair-vocabulary")
    assert accepted.candidate is not None, accepted.evaluation.diagnostics
    number = 1
    changes: dict[str, bytes] = {}
    tree = dict(instance.tree_at(instance.accepted_coordinate().git_oid))

    def subject(kind: str, subject_id: str) -> str:
        at = path_of(kind, subject_id)
        changes[at] = tree[at] = render_subject(shell(kind, subject_id))
        return at

    def add(predicate: str, obj: Any, at: str) -> str:
        nonlocal number
        path, content = real_claim(instance, tree, number, predicate, obj, subject=at)
        number += 1
        changes[path] = tree[path] = content
        return path

    component = subject(COMPONENT, "c1")
    add(PURL, literal("c1"), component)
    facts: dict[str, str] = {}
    for index in range(pairs):
        vuln = subject(VULN, f"v{index}")
        add(VID, literal(f"v{index}"), vuln)
        pair = subject(EXPOSURE, f"c1--v{index}")
        add(E_COMPONENT, to(component), pair)
        add(E_VULN, to(vuln), pair)
        facts[pair] = add(SCOPE, literal("required"), pair)
    accepted = accept_raw(instance, changes, name="pair-world")
    assert accepted.candidate is not None, accepted.evaluation.diagnostics
    return instance, facts


__all__ = ["REAL_TYPES", "accept_raw", "pair_instance", "real_claim"]
