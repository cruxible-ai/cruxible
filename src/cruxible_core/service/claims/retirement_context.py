"""What a Claim shares with retired Claims, as review context for its explanation.

Retiring a Claim does not retire the evidence it cited. A live Claim that
shares a capture, an exact external source, or a same-version cited span with
a retired one is not broken by that; it is what a reviewer wants to see beside
the Claim. A real dependency on a retired Claim is a queue row of its own
(`claim_dependency_stale`) and is not repeated here.

The relations come from the bound citation projection alone, so an
explanation is a function of its accepted coordinate and reads no workspace.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from cruxible_client.contracts.canonical import Sha256Value
from cruxible_client.contracts.errors import PlaybillError, ProposalIntegrityError
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance

_WITNESS_LIMIT = 8
#: Accepted-state relation kinds, strongest first.
_ACCEPTED_RELATION_ORDER = ("capture", "exact_external", "same_version_span")

RetiredRelationKind = Literal["capture", "exact_external", "same_version_span"]


class _StrictRetirementContextModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClaimRetiredRelationV1(_StrictRetirementContextModel):
    """One piece of evidence or one cited span this Claim shares with retired Claims."""

    tag: Literal["playbill-claim-retired-relation-v1"] = "playbill-claim-retired-relation-v1"
    relation_kind: RetiredRelationKind
    live_citation_id: str
    live_capture_digest: str
    #: The accepted citation group both sides belong to.
    relation_key: str
    retired_claim_count: int = Field(ge=1)
    retired_claim_witnesses: tuple[str, ...]
    retired_citation_count: int = Field(ge=1)
    retired_citation_witnesses: tuple[str, ...]

    @model_validator(mode="after")
    def _shape(self) -> "ClaimRetiredRelationV1":
        Sha256Value.from_tagged(self.live_capture_digest)
        return self


class ClaimRetirementContextV1(_StrictRetirementContextModel):
    tag: Literal["playbill-claim-retirement-context-v1"] = "playbill-claim-retirement-context-v1"
    shared_with_retired: tuple[ClaimRetiredRelationV1, ...] = Field(min_length=1)


def _bounded(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(set(values), key=lambda value: value.encode("utf-8"))[:_WITNESS_LIMIT])


def _digest_value(value: object) -> str | None:
    if isinstance(value, Mapping) and isinstance(value.get("$digest"), str):
        value = value["$digest"]
    if not isinstance(value, str):
        return None
    try:
        Sha256Value.from_tagged(value)
    except ValueError:
        return None
    return value


def _count(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("retired relation count is not an integer")
    return value


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise ValueError("retired relation witnesses are not strings")
    return tuple(cast(Iterable[str], value))


def _accepted_relation(value: object) -> ClaimRetiredRelationV1:
    if not isinstance(value, Mapping):
        raise ValueError("retired conflict has an invalid value")
    capture = _digest_value(value.get("live_capture_digest"))
    if capture is None:
        raise ValueError("retired conflict has no live capture digest")
    return ClaimRetiredRelationV1.model_validate(
        {
            "relation_kind": value.get("relation_kind"),
            "live_citation_id": value.get("live_citation_id"),
            "live_capture_digest": capture,
            "relation_key": value.get("relation_key"),
            "retired_claim_count": _count(value.get("retired_claim_count")),
            "retired_claim_witnesses": _strings(value.get("retired_claim_witnesses")),
            "retired_citation_count": _count(value.get("retired_citation_count")),
            "retired_citation_witnesses": _strings(value.get("retired_citation_witnesses")),
        }
    )


def claim_retirement_context(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    claim_identity: str,
) -> ClaimRetirementContextV1 | None:
    """Read one Claim's retirement relations; None when it shares nothing with a retired one."""

    try:
        with instance.bind_accepted_projection(coordinate) as projection:
            accepted = [
                _accepted_relation(fact.value)
                for fact in projection.citations.conflicts(claim_identities=(claim_identity,))
                if isinstance(fact.value, Mapping)
                and fact.value.get("live_claim_identity") == claim_identity
            ]
    except (PlaybillError, ValueError, ValidationError) as exc:
        raise ProposalIntegrityError(
            "playbill.claim.retirement_context_invalid: citation relation projection is invalid"
        ) from exc
    if not accepted:
        return None
    return ClaimRetirementContextV1(
        shared_with_retired=tuple(
            sorted(
                accepted,
                key=lambda item: (
                    _ACCEPTED_RELATION_ORDER.index(item.relation_kind),
                    item.relation_key.encode("utf-8"),
                ),
            )
        )
    )


__all__ = [
    "ClaimRetiredRelationV1",
    "ClaimRetirementContextV1",
    "claim_retirement_context",
]
