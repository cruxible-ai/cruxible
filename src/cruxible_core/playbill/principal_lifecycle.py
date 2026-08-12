"""Governed client-principal registration, rotation, revocation, and recovery."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from cruxible_core.playbill.candidates import (
    CandidateMemberEvidence,
    CandidateRecord,
    SemanticCandidate,
    candidate_digest,
)
from cruxible_core.playbill.canonical import (
    file_digest,
    manifest_root,
    semantic_diff,
    semantic_projection,
)
from cruxible_core.playbill.errors import PrincipalIntegrityError
from cruxible_core.playbill.governance import ApprovalRequirement, MutationDisposition
from cruxible_core.playbill.laws import PRINCIPAL_LIFECYCLE_ACCEPTANCE_LAW
from cruxible_core.playbill.principals import (
    PrincipalRegistrySnapshot,
    parse_principal_record,
    principal_registry_from_tree,
)
from cruxible_core.playbill.projection import AcceptedProjectionCoordinate
from cruxible_core.playbill.types import PrincipalRecord, PrincipalRole

PrincipalLifecycleAction = Literal["register", "rotate", "revoke", "recover"]


@dataclass(frozen=True)
class PrincipalLifecycleEvaluation:
    candidate: CandidateRecord | None
    error_code: str | None = None
    error_message: str | None = None


def _refused(code: str, message: str) -> PrincipalLifecycleEvaluation:
    return PrincipalLifecycleEvaluation(None, code, message)


def _actor(
    principals: PrincipalRegistrySnapshot,
    actor_id: str,
) -> PrincipalRecord | None:
    try:
        return principals.require_active(actor_id)
    except PrincipalIntegrityError:
        return None


def _approval_role(principal: PrincipalRecord) -> PrincipalRole:
    for role in ("owner", "reviewer", "recovery"):
        if role in principal.authority_roles:
            return role
    raise PrincipalIntegrityError("daemon authority cannot authorize client-principal lifecycle")


def _classify(
    previous: PrincipalRecord | None,
    proposed: PrincipalRecord,
    *,
    actor: PrincipalRecord,
) -> tuple[PrincipalLifecycleAction, MutationDisposition] | None:
    if previous is None:
        if proposed.status != "active" or "daemon" in proposed.authority_roles:
            return None
        if "owner" not in actor.authority_roles:
            return None
        return "register", "replacement"
    if previous.principal_id == "daemon" or proposed.principal_id == "daemon":
        return None
    if previous.authority_roles != proposed.authority_roles:
        return None
    if previous.status == "active" and proposed.status == "active":
        if previous.public_key == proposed.public_key:
            return None
        if actor.principal_id == previous.principal_id:
            return "rotate", "hand-authored-successor"
        if "recovery" in actor.authority_roles:
            return "recover", "hand-authored-successor"
        return None
    if previous.status == "active" and proposed.status == "revoked":
        if previous.public_key != proposed.public_key:
            return None
        if "owner" in actor.authority_roles or "recovery" in actor.authority_roles:
            return "revoke", "invalidation"
        return None
    if previous.status == "revoked" and proposed.status == "active":
        if previous.public_key == proposed.public_key:
            return None
        if "recovery" in actor.authority_roles:
            return "recover", "hand-authored-successor"
    return None


def evaluate_principal_lifecycle(
    *,
    current_tree: Mapping[str, bytes],
    proposed_tree: Mapping[str, bytes],
    current: AcceptedProjectionCoordinate,
    path: str,
    actor_id: str | None,
    timestamp: str,
) -> PrincipalLifecycleEvaluation:
    """Evaluate one control-plane principal mutation under parent-root key state."""

    if actor_id is None:
        return _refused(
            "playbill.principal.actor_required",
            "Principal lifecycle evaluation requires an authenticated actor.",
        )
    content = proposed_tree.get(path)
    if content is None:
        return _refused(
            "playbill.principal.removal_unsupported",
            "Principal records are revoked, never removed from accepted state.",
        )
    try:
        proposed = parse_principal_record(content, path=path)
        principals = principal_registry_from_tree(
            current_tree,
            semantic_root=current.semantic_root,
        )
        previous_content = current_tree.get(path)
        previous = (
            parse_principal_record(previous_content, path=path)
            if previous_content is not None
            else None
        )
    except PrincipalIntegrityError as exc:
        return _refused("playbill.principal.format_invalid", str(exc))
    actor = _actor(principals, actor_id)
    if actor is None or "daemon" in actor.authority_roles:
        return _refused(
            "playbill.principal.actor_unauthorized",
            "Principal lifecycle actor is absent, revoked, or daemon-only at the parent root.",
        )
    classified = _classify(previous, proposed, actor=actor)
    if classified is None:
        return _refused(
            "playbill.principal.transition_unauthorized",
            "Principal transition is outside registration, self-rotation, "
            "revocation, or recovery policy.",
        )
    action, disposition = classified
    try:
        proposed_registry = principal_registry_from_tree(
            proposed_tree,
            semantic_root=current.semantic_root,
        )
    except PrincipalIntegrityError as exc:
        return _refused("playbill.principal.registry_invalid", str(exc))
    active_roles = {
        role
        for principal in proposed_registry.principals
        if principal.status == "active"
        for role in principal.authority_roles
    }
    if "owner" not in active_roles:
        return _refused(
            "playbill.principal.last_owner",
            "A principal transition must retain at least one active owner.",
        )
    recovery_was_configured = any(
        "recovery" in principal.authority_roles for principal in principals.principals
    )
    if recovery_was_configured and "recovery" not in active_roles:
        return _refused(
            "playbill.principal.last_recovery",
            "A recovery-configured instance must retain an active recovery principal.",
        )

    diff_digest, scope = semantic_diff(current_tree, proposed_tree)
    semantic_candidate = SemanticCandidate(
        parent_semantic_root=current.semantic_root,
        candidate_manifest_root=manifest_root(semantic_projection(proposed_tree)).tagged,
        semantic_diff_digest=diff_digest.tagged,
        scope=scope,
        timestamp=timestamp,
    )
    law = PRINCIPAL_LIFECYCLE_ACCEPTANCE_LAW
    record = CandidateRecord(
        candidate=semantic_candidate,
        candidate_digest=candidate_digest(semantic_candidate).tagged,
        required_tier="admin",
        approval_requirements=(ApprovalRequirement(role=_approval_role(actor)),),
        activation_policy="snapshot",
        closure_paths=scope,
        members=(
            CandidateMemberEvidence(
                path=path,
                artifact_kind=law.artifact_kind,
                artifact_digest=file_digest(content).tagged,
                disposition=disposition,
                law_identifier=law.coordinate.identifier,
                governance_operation=action,
            ),
        ),
        law_digests={law.coordinate.identifier: law.coordinate.digest},
        compiler_digest=current.compiler.rule_digest,
    )
    return PrincipalLifecycleEvaluation(record)


__all__ = [
    "PrincipalLifecycleAction",
    "PrincipalLifecycleEvaluation",
    "evaluate_principal_lifecycle",
]
