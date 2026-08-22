"""One structural Claim-slot classifier shared by projections and Brief health."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

from cruxible_core.playbill.canonical import canonical_bytes
from cruxible_core.playbill.claims import ClaimArtifactAny

ClaimSlotResolution = Literal["empty", "single", "unresolved"]
ClaimSlotMemberState = Literal["accepted_current", "conflicted", "absent"]


@dataclass(frozen=True)
class ClaimSlotClassification:
    """Coordinate-pure resolution of accepted live Claims by semantic value."""

    resolution: ClaimSlotResolution
    claim_count: int
    contender_count: int
    selected_claim_identities: tuple[str, ...]
    contender_claim_identities: tuple[str, ...]
    single_object: object | None


def classify_claim_slot(claims: Iterable[ClaimArtifactAny]) -> ClaimSlotClassification:
    """Classify one already-partitioned ``(subject,predicate,qualifier)`` slot.

    Multiple Claims making the same statement support one contender.  This is
    structural accepted-state resolution; time-relative verdicts stay on their
    existing single evaluation path.
    """

    live = tuple(claim for claim in claims if claim.lifecycle.state == "live")
    grouped: dict[bytes, tuple[object, list[str]]] = {}
    for claim in live:
        payload: object = claim.statement.object.model_dump(mode="json")
        key = canonical_bytes(payload)
        if key not in grouped:
            grouped[key] = (payload, [])
        grouped[key][1].append(claim.identity.qualified)

    identities = tuple(
        sorted(
            (identity for _payload, values in grouped.values() for identity in values),
            key=lambda item: item.encode("utf-8"),
        )
    )
    if not grouped:
        return ClaimSlotClassification("empty", 0, 0, (), (), None)
    if len(grouped) == 1:
        payload, selected = next(iter(grouped.values()))
        selected_identities = tuple(sorted(selected, key=lambda item: item.encode("utf-8")))
        return ClaimSlotClassification(
            "single",
            len(live),
            1,
            selected_identities,
            selected_identities,
            payload,
        )
    return ClaimSlotClassification(
        "unresolved",
        len(live),
        len(grouped),
        (),
        identities,
        None,
    )


def classify_claim_slot_member(
    slot: ClaimSlotClassification,
    claim_identity: str,
) -> ClaimSlotMemberState:
    """Return one member's state from the exact structural slot answer."""

    if slot.resolution == "single" and claim_identity in slot.selected_claim_identities:
        return "accepted_current"
    if slot.resolution == "unresolved" and claim_identity in slot.contender_claim_identities:
        return "conflicted"
    return "absent"


__all__ = [
    "ClaimSlotClassification",
    "ClaimSlotMemberState",
    "ClaimSlotResolution",
    "classify_claim_slot",
    "classify_claim_slot_member",
]
