"""Governed client-principal registration, rotation, revocation, and recovery."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from cruxible_client.contracts.errors import PrincipalIntegrityError
from cruxible_client.contracts.principals import (
    PrincipalRegistrySnapshot,
    parse_principal_record,
    principal_registry_from_tree,
)
from cruxible_client.contracts.types import PrincipalRecord, PrincipalRole
from cruxible_core.playbill.projection import AcceptedProjectionCoordinate

PrincipalLifecycleAction = Literal["register", "rotate", "revoke", "recover"]


@dataclass(frozen=True)
class PrincipalLifecycleEvaluation:
    """One principal transition's law result, in the shape every member law returns.

    This law used to assemble a whole candidate record of its own, which is why
    a single-member principal change had to bypass the evaluator every other
    member kind went through. It now answers the same question the other member
    laws answer -- what happened, what it costs to approve, and what the member's
    digest is -- and the one evaluator assembles the candidate.
    """

    action: PrincipalLifecycleAction | None
    approval_role: PrincipalRole | None = None
    error_code: str | None = None
    error_message: str | None = None


def _refused(code: str, message: str) -> PrincipalLifecycleEvaluation:
    return PrincipalLifecycleEvaluation(None, error_code=code, error_message=message)


def _actor(
    principals: PrincipalRegistrySnapshot,
    actor_id: str,
) -> PrincipalRecord | None:
    try:
        return principals.require_active(actor_id)
    except PrincipalIntegrityError:
        return None


def ordinary_approval_capable(principal: PrincipalRecord) -> bool:
    """Return whether one accepted Principal can satisfy ordinary approval."""

    return (
        principal.status == "active"
        and principal.principal_id != "daemon"
        and principal.authority_roles != ("recovery",)
        and principal.authority_roles != ("daemon",)
    )


def _classify(
    previous: PrincipalRecord | None,
    proposed: PrincipalRecord,
    *,
    actor: PrincipalRecord,
) -> PrincipalLifecycleAction | None:
    if previous is None:
        if proposed.status != "active" or "daemon" in proposed.authority_roles:
            return None
        return "register"
    if previous.principal_id == "daemon" or proposed.principal_id == "daemon":
        return None
    # Authority roles are dormant for admission in this pre-release lineage,
    # but their accepted bytes remain immutable across lifecycle transitions.
    # Changing them requires a future governed policy/ontology succession.
    if previous.authority_roles != proposed.authority_roles:
        return None
    if previous.status == "active" and proposed.status == "active":
        if previous.public_key == proposed.public_key:
            return None
        if actor.principal_id == previous.principal_id:
            return "rotate"
        return "recover"
    if previous.status == "active" and proposed.status == "revoked":
        if previous.public_key != proposed.public_key:
            return None
        return "revoke"
    if previous.status == "revoked" and proposed.status == "active":
        # Revocation permanently kills the accepted key bytes. Recovery must
        # introduce fresh material before the Principal can become active.
        if previous.public_key == proposed.public_key:
            return None
        return "recover"
    return None


def evaluate_principal_lifecycle(
    *,
    candidate_content: bytes,
    parent_content: bytes | None,
    principals: PrincipalRegistrySnapshot,
    candidate_tree: Mapping[str, bytes],
    current: AcceptedProjectionCoordinate,
    path: str,
    actor_id: str | None,
) -> PrincipalLifecycleEvaluation:
    """Evaluate one control-plane principal mutation under parent-root key state."""

    if actor_id is None:
        return _refused(
            "playbill.principal.actor_required",
            "Principal lifecycle evaluation requires an authenticated actor.",
        )
    try:
        proposed = parse_principal_record(candidate_content, path=path)
        previous = (
            parse_principal_record(parent_content, path=path)
            if parent_content is not None
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
    action = _classify(previous, proposed, actor=actor)
    if action is None:
        return _refused(
            "playbill.principal.transition_unauthorized",
            "Principal transition is outside registration, self-rotation, "
            "revocation, or recovery policy.",
        )
    try:
        proposed_registry = principal_registry_from_tree(
            candidate_tree,
            semantic_root=current.semantic_root,
        )
    except PrincipalIntegrityError as exc:
        return _refused("playbill.principal.registry_invalid", str(exc))
    recovery_was_configured = any(
        "recovery" in principal.authority_roles for principal in principals.principals
    )
    if recovery_was_configured and not any(
        principal.status == "active" and "recovery" in principal.authority_roles
        for principal in proposed_registry.principals
    ):
        return _refused(
            "playbill.principal.last_recovery",
            "A recovery-configured instance must retain an active recovery principal.",
        )
    ordinary_approvers = tuple(
        principal
        for principal in proposed_registry.principals
        if ordinary_approval_capable(principal)
    )
    if len(ordinary_approvers) < 2:
        return _refused(
            "playbill.principal.independent_quorum_unconstructible",
            "A principal transition must retain at least two active ordinary-approval-capable "
            "client Principals; recovery and daemon Principals do not count.",
        )
    return PrincipalLifecycleEvaluation(action=action)


__all__ = [
    "PrincipalLifecycleAction",
    "PrincipalLifecycleEvaluation",
    "evaluate_principal_lifecycle",
    "ordinary_approval_capable",
]
