"""Verdict flags for every read verb, derived once.

``query`` rows and ``get`` cards show a Claim's verdict problems as flags, never
by hiding the Claim. Each flag comes from machinery that already decided it;
nothing here re-adjudicates:

- ``stale``: the Claim's verdict is ``stale`` or ``stale_evidence``;
- ``contested``: the Claim's slot is ``conflicted`` (resolution left its
  contenders unresolved), or the Claim's own verdict is ``unresolved`` (its
  evidence both supports and contradicts it), or a one-cardinality answer shows
  more than one distinct live value;
- ``contradicted``: the Claim's verdict is ``contradicted``;
- ``uncovered``: the Claim's verdict is ``uncovered`` (it is accepted, but no
  admitted evidence backs it at the evaluation time);
- ``unsure_hold``: ``next`` parks one of the Claim's rows under an ``unsure``
  examined attestation right now (``next.claim_unsure_holds``), decided by
  ``next``'s own Claim rows and hold coverage, so a new contender, a revised
  input or newer evidence ends the hold here exactly as it returns the row to
  the queue.

``claim_reads`` is the whole derivation for a set of Claims (each Claim's flags,
verdict and slot status), ``claim_flags`` its flags alone; ``verdict_flags``
is its per-Claim rule for callers that already hold each Claim's verdict and
slot status. Either way the Claims passed must include every live contender of
each slot they touch, so a slot's status and its holds are the full slot's.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, MutableMapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from cruxible_client.contracts.claims import ClaimArtifactAny, claim_path
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import PlaybillAcceptedCoordinate

ReadFlag = Literal["stale", "contested", "contradicted", "uncovered", "unsure_hold"]
FLAG_ORDER: tuple[ReadFlag, ...] = (
    "stale",
    "contested",
    "contradicted",
    "uncovered",
    "unsure_hold",
)


def ordered_flags(flags: Iterable[ReadFlag]) -> list[ReadFlag]:
    """Flags in their one display order, each once."""

    present = set(flags)
    return [flag for flag in FLAG_ORDER if flag in present]


def verdict_flags(
    verdict: str | None,
    status: str | None = None,
    *,
    held: bool = False,
) -> tuple[ReadFlag, ...]:
    """The flags one Claim shows, from its verdict, its slot status and its hold."""

    flags: set[ReadFlag] = set()
    if verdict in {"stale", "stale_evidence"}:
        flags.add("stale")
    if verdict == "unresolved" or status == "conflicted":
        flags.add("contested")
    if verdict == "contradicted":
        flags.add("contradicted")
    if verdict == "uncovered":
        flags.add("uncovered")
    if held:
        flags.add("unsure_hold")
    return tuple(ordered_flags(flags))


def answer_flags(cardinality: str, distinct_values: int) -> tuple[ReadFlag, ...]:
    """A one-cardinality answer that shows several distinct live values is contested."""

    return ("contested",) if cardinality == "one" and distinct_values > 1 else ()


def unsure_holds(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    claims: Iterable[ClaimArtifactAny],
    *,
    statuses: Mapping[str, str] | None,
    evaluation_time: datetime,
) -> frozenset[str]:
    """Claims an ``unsure`` examined attestation holds right now, as ``next`` decides.

    ``statuses`` are the slot resolution statuses by bare Claim id, over every
    live contender of each slot in ``claims``.
    """

    from cruxible_core.service.discovery.next import claim_unsure_holds

    return claim_unsure_holds(
        instance,
        coordinate=coordinate,
        claims=tuple(claims),
        evaluation_time=evaluation_time,
        resolution_statuses=statuses,
    )


@dataclass(frozen=True)
class ClaimRead:
    """One live Claim as a read verb shows it: its flags, its verdict and its slot status."""

    flags: tuple[ReadFlag, ...]
    verdict: str
    status: str


def claim_flags(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    *,
    identities: Iterable[str],
    evaluation_time: datetime,
) -> dict[str, tuple[ReadFlag, ...]]:
    """Flags per qualified Claim identity, from the shared per-slot verdict derivation.

    ``identities`` must hold every live contender of each slot it touches, so
    each slot's resolution is its full resolution.
    """

    return {
        identity: read.flags
        for identity, read in claim_reads(
            instance, coordinate, identities=identities, evaluation_time=evaluation_time
        ).items()
    }


def claim_reads(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    *,
    identities: Iterable[str],
    evaluation_time: datetime,
) -> dict[str, ClaimRead]:
    """Each live Claim's flags, verdict and slot status, by qualified identity.

    ``identities`` must hold every live contender of each slot it touches, so
    each slot's resolution is its full resolution.
    """

    from cruxible_core.service.discovery.claim_status import claim_resolution_statuses
    from cruxible_core.service.evidence.evidence import ClaimVerdictReadContext

    ordered = tuple(sorted(set(identities)))
    if not ordered:
        return {}
    context = ClaimVerdictReadContext(instance, coordinate)
    context.prefetch(tuple(claim_path(identity.removeprefix("Claim:")) for identity in ordered))
    parsed = tuple(context.claim(identity) for identity in ordered)
    verdicts: MutableMapping[str, Any] = {}
    statuses = claim_resolution_statuses(
        instance,
        claims=parsed,
        at=PlaybillAcceptedCoordinate.from_internal(coordinate),
        evaluation_time=evaluation_time,
        verdicts_by_identity=verdicts,
        read_context=context,
    )
    held = unsure_holds(
        instance,
        coordinate,
        parsed,
        statuses=statuses,
        evaluation_time=evaluation_time,
    )
    reads: dict[str, ClaimRead] = {}
    for claim in parsed:
        verdict = getattr(verdicts.get(claim.identity.qualified), "verdict", None)
        status = statuses.get(claim.identity.name)
        reads[claim.identity.qualified] = ClaimRead(
            flags=verdict_flags(verdict, status, held=claim.identity.qualified in held),
            verdict=str(verdict or "unresolved"),
            status=str(status or "accepted"),
        )
    return reads


__all__ = [
    "FLAG_ORDER",
    "ClaimRead",
    "ReadFlag",
    "answer_flags",
    "claim_flags",
    "claim_reads",
    "ordered_flags",
    "unsure_holds",
    "verdict_flags",
]
