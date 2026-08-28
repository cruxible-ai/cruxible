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
from cruxible_client.contracts.types import PrincipalRecord
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


def _classify(
    previous: PrincipalRecord | None,
    proposed: PrincipalRecord,
    *,
    actor: PrincipalRecord,
) -> PrincipalLifecycleAction | None:
    if previous is None:
        if proposed.status != "active" or proposed.kind == "daemon":
            return None
        return "register"
    if previous.principal_id == "daemon" or proposed.principal_id == "daemon":
        return None
    if previous.kind != proposed.kind:
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
    if actor is None or actor.kind == "daemon":
        return _refused(
            "playbill.principal.actor_unauthorized",
            "Principal lifecycle actor is absent, revoked, or daemon-only at the parent root.",
        )
    action = _classify(previous, proposed, actor=actor)
    if actor.kind == "recovery" and action != "recover":
        action = None
    if action is None:
        return _refused(
            "playbill.principal.transition_unauthorized",
            "Principal transition is outside registration, self-rotation, "
            "revocation, or recovery policy.",
        )
    try:
        principal_registry_from_tree(
            candidate_tree,
            semantic_root=current.semantic_root,
        )
    except PrincipalIntegrityError as exc:
        return _refused("playbill.principal.registry_invalid", str(exc))
    return PrincipalLifecycleEvaluation(action=action)


__all__ = [
    "PrincipalLifecycleAction",
    "PrincipalLifecycleEvaluation",
    "evaluate_principal_lifecycle",
]
