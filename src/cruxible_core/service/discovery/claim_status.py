"""Each accepted Claim's resolution status, derived once per slot and remembered.

A Claim's status says how resolution placed it in its slot: ``accepted`` (it
answers the slot), ``conflicted`` (resolution left its contenders unresolved),
``overturned`` (an accepted rival won), ``refused`` (its own ClaimType's
admission failed) or ``retired``. ``read_flags``, ``next``, the floor, orient's
status counts, projection sync and activation all read statuses here, from one
memoized derivation over the slots they touch.
"""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claim_types import claim_type_path
from cruxible_client.contracts.claim_verdicts import ClaimVerdictResultAny
from cruxible_client.contracts.claims import (
    ClaimArtifactAny,
    SubjectClaimObject,
    claim_path,
)
from cruxible_core.claims.claim_slots import ClaimSlotClassification, classify_claim_slot
from cruxible_core.derived.memo import memo_clear, memo_get, memo_put
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import AcceptedCoordinate
from cruxible_core.service.claims.claims import (
    PlaybillClaimGroupResolution,
    _resolve_coordinate,
    resolve_playbill_claim_group,
)
from cruxible_core.service.claims.verdict_memo import (
    MEMO_CAPACITY,
    claim_set_digest,
    interval_holds,
    invariance_interval,
    memo_key,
    verdict_input_fingerprint,
)
from cruxible_core.service.evidence.evidence import (
    BodyFingerprints,
    ClaimVerdictReadContext,
    VerdictReads,
    body_fingerprints_hold,
)

#: How resolution placed one accepted Claim in its slot, or ``retired``.
ClaimStatus = Literal["accepted", "conflicted", "overturned", "refused", "retired"]


# Bounded, per-process, and keyed on every input the derivation reads. See
# `playbill_verdict_memo` for why each part of the key is there.
# Each entry also keeps the file identity of every body-store object its
# verdicts read (``VerdictReads.body_identities``) and is served only while all
# of them hold: the shard fingerprint in the key sees arrivals and removals, not
# a body rewritten in place.
_RESOLUTION_MEMO: (
    "OrderedDict[tuple[str, str, str, str], "
    "tuple[dict[str, ClaimStatus], dict[str, ClaimVerdictResultAny], "
    "tuple[datetime | None, datetime | None], BodyFingerprints]]"
) = OrderedDict()


# Per-slot answers carried across coordinates. Each entry keeps the exact read
# set its verdicts consulted and the values those reads returned; it is served
# at another coordinate only after every read is re-read and matches, and only
# inside its time-invariance interval. Bounded; nothing depends on it.
_SLOT_MEMO_CAPACITY = 16384
_SLOT_MEMO: "OrderedDict[tuple[str, str, bytes], _RememberedSlot]" = OrderedDict()


@dataclass(frozen=True)
class _RememberedSlot:
    members: tuple[str, ...]
    reads: VerdictReads
    observed: dict[tuple[str, ...], object]
    interval: tuple[datetime | None, datetime | None]
    statuses: dict[str, ClaimStatus]
    verdicts: dict[str, ClaimVerdictResultAny]


def reset_claim_resolution_memo(*, slots: bool = True) -> None:
    """Forget remembered derivations; activation keeps the validated slot answers."""

    memo_clear(_RESOLUTION_MEMO)
    if slots:
        memo_clear(_SLOT_MEMO)


def _resolution_key(claim: ClaimArtifactAny) -> bytes:
    return canonical_bytes(
        {
            "predicate": claim.statement.predicate,
            "subject": claim.statement.subject.model_dump(mode="json"),
        }
    )


def _resolution_memo_key(
    instance: PlaybillInstance,
    *,
    identities: tuple[str, ...],
    at: AcceptedCoordinate,
    input_fingerprint: str | None,
) -> tuple[str, str, str, str]:
    return memo_key(
        instance_root=str(instance.root),
        coordinate_digest=canonical_bytes(at.model_dump(mode="json")).hex(),
        claim_set_digest=claim_set_digest(identities),
        input_fingerprint=input_fingerprint or "",
    )


def remembered_resolution_statuses(
    instance: PlaybillInstance,
    *,
    identities: tuple[str, ...],
    at: AcceptedCoordinate,
    evaluation_time: datetime,
) -> dict[str, ClaimStatus] | None:
    """A still-valid remembered answer for exactly these Claims, without reading them."""

    input_fingerprint = verdict_input_fingerprint(instance)
    if input_fingerprint is None:
        return None
    remembered = memo_get(
        _RESOLUTION_MEMO,
        _resolution_memo_key(
            instance, identities=identities, at=at, input_fingerprint=input_fingerprint
        ),
    )
    if (
        remembered is None
        or not interval_holds(remembered[2], evaluation_time=evaluation_time)
        or not body_fingerprints_hold(instance, remembered[3])
    ):
        return None
    return dict(remembered[0])


def claim_resolution_statuses(
    instance: PlaybillInstance,
    *,
    claims: tuple[ClaimArtifactAny, ...],
    at: AcceptedCoordinate,
    evaluation_time: datetime,
    verdicts_by_identity: MutableMapping[str, ClaimVerdictResultAny] | None = None,
    read_context: ClaimVerdictReadContext | None = None,
    time_boundaries: set[datetime] | None = None,
) -> dict[str, ClaimStatus]:
    """Derive each Claim's resolution status at one accepted coordinate.

    ``verdicts_by_identity`` lets one request share Claim verdicts with another
    fold over the same coordinate and evaluation time; it must never outlive
    that pair.

    The whole derivation is memoized per process on the instance, the accepted
    coordinate, the exact Claim set, and CAS shard metadata for live replay
    availability, and served only while every body-store object its verdicts
    read keeps its file identity. Pending door attestations are not an input.
    The evaluation instant is NOT in the key: every real surface stamps a fresh
    `utc_now()`, so a wall-clock key could never be hit twice. A verdict is a
    step function of time whose only breakpoints are the instants it compares
    against, so the entry carries the
    interval over which its answer holds and is served for any instant inside
    it, with each remembered verdict re-stamped with the instant it is served
    at. An `orient` is a READ, and this read used to cross the client's own
    default timeout at a few hundred Claims; a second read of the same state now
    evaluates no verdicts at all. Nothing depends on the memo: it is
    per-process, cold after a restart, and bounded.
    """

    input_fingerprint = verdict_input_fingerprint(instance)
    key = _resolution_memo_key(
        instance,
        identities=tuple(claim.identity.qualified for claim in claims),
        at=at,
        input_fingerprint=input_fingerprint,
    )
    remembered = None if input_fingerprint is None else memo_get(_RESOLUTION_MEMO, key)
    if remembered is not None:
        memoized_statuses, memoized_verdicts, interval, bodies = remembered
        if interval_holds(interval, evaluation_time=evaluation_time) and body_fingerprints_hold(
            instance, bodies, store=read_context.body_store() if read_context else None
        ):
            if time_boundaries is not None:
                time_boundaries.update(bound for bound in interval if bound is not None)
            if verdicts_by_identity is not None:
                verdicts_by_identity.update(
                    {
                        identity: verdict.model_copy(update={"evaluation_time": evaluation_time})
                        for identity, verdict in memoized_verdicts.items()
                    }
                )
            return dict(memoized_statuses)

    # A caller that arrives with verdicts already in hand did not have them
    # derived here, so the boundaries this fold can see are not all of them and
    # the interval would be wider than the truth. Such a fold answers from the
    # verdicts it was given and remembers nothing.
    remember = input_fingerprint is not None and not verdicts_by_identity
    boundaries: set[datetime] = set()
    verdicts: MutableMapping[str, ClaimVerdictResultAny] = (
        {} if verdicts_by_identity is None else verdicts_by_identity
    )
    live_groups: dict[bytes, list[ClaimArtifactAny]] = defaultdict(list)
    statuses: dict[str, ClaimStatus] = {}
    for claim in claims:
        if claim.lifecycle.state == "retired":
            statuses[claim.identity.name] = "retired"
        else:
            live_groups[_resolution_key(claim)].append(claim)

    coordinate = _resolve_coordinate(instance, at)
    read_context = read_context or ClaimVerdictReadContext(instance, coordinate)
    root = str(instance.root)
    compiler = coordinate.compiler.rule_digest

    # Slots answered at an earlier coordinate are reused when every read their
    # verdicts made still returns the same value here: one batched re-read of
    # all their inputs instead of re-deriving them.
    pending = dict(live_groups)
    # Every body-store object the verdicts' replay availability read, reused or
    # derived, with the identity each read used: the entry's fingerprints.
    bodies_read = VerdictReads()
    if remember:
        candidates: dict[bytes, _RememberedSlot] = {}
        for slot_key, group in live_groups.items():
            entry = memo_get(_SLOT_MEMO, (root, compiler, slot_key))
            if (
                entry is not None
                and entry.members == _slot_members(group)
                and interval_holds(entry.interval, evaluation_time=evaluation_time)
            ):
                candidates[slot_key] = entry
        if candidates:
            union = VerdictReads()
            for entry in candidates.values():
                union.update(entry.reads)
            # A reused slot rests on the Captures just re-read to validate it,
            # with the identities that re-read used.
            validated = VerdictReads()
            current = read_context.snapshot(union, bodies=validated)
            bodies_read.update(validated)
            for slot_key, entry in candidates.items():
                if all(current.get(read) == value for read, value in entry.observed.items()):
                    statuses.update(entry.statuses)
                    verdicts.update(
                        {
                            identity: verdict.model_copy(
                                update={"evaluation_time": evaluation_time}
                            )
                            for identity, verdict in entry.verdicts.items()
                        }
                    )
                    boundaries.update(bound for bound in entry.interval if bound is not None)
                    del pending[slot_key]

    # One batched read of every Claim still to derive and the artifacts its
    # verdict reads (ClaimType and referents) instead of one read per verdict.
    live = tuple(claim for group in pending.values() for claim in group)
    read_context.prefetch(
        tuple(
            path
            for claim in live
            for path in (
                claim_path(claim.identity.name),
                claim_type_path(claim.statement.predicate),
                claim.statement.subject.artifact_path,
                *(
                    (claim.statement.object.address.artifact_path,)
                    if isinstance(claim.statement.object, SubjectClaimObject)
                    else ()
                ),
            )
        )
    )
    # Register the whole batch before any verdict, so batch-wide reads (such as
    # attestation selection) cover every Claim at once, not one Claim per miss.
    for claim in live:
        read_context.claim(claim.identity.qualified)
    read_context.prefetch_law_evidence(tuple(claim_path(claim.identity.name) for claim in live))
    derived: dict[bytes, tuple[VerdictReads, set[datetime], dict[str, ClaimStatus]]] = {}
    for slot_key, group in pending.items():
        first = group[0]
        group_boundaries: set[datetime] = set()
        reads = read_context.record() if remember else None
        try:
            resolution = resolve_playbill_claim_group(
                instance,
                subject=first.statement.subject,
                predicate=first.statement.predicate,
                coordinate=coordinate,
                evaluated_at=evaluation_time,
                claims=tuple(group),
                verdicts_by_identity=verdicts,
                time_boundaries=group_boundaries
                if remember or time_boundaries is not None
                else None,
                read_context=read_context,
            )
        finally:
            read_context.stop()
        boundaries.update(group_boundaries)
        groups_by_qualifier: dict[str | None, list[ClaimArtifactAny]] = defaultdict(list)
        for claim in group:
            groups_by_qualifier[claim.statement.qualifier].append(claim)
        slots = {
            claim.identity.name: classification
            for members in groups_by_qualifier.values()
            for classification in (classify_claim_slot(members),)
            for claim in members
        }
        group_statuses: dict[str, ClaimStatus] = {}
        _apply_resolution_statuses(resolution, group_statuses, slots=slots)
        statuses.update(group_statuses)
        if reads is not None and reads.inconsistent:
            # Inconsistent observations: nothing from this derivation is remembered.
            remember = False
        if reads is not None and not reads.inconsistent:
            derived[slot_key] = (reads, group_boundaries, group_statuses)
            bodies_read.note_bodies(reads.body_identities if reads.bodies_complete else None)
    if derived:
        union = VerdictReads()
        for reads, _bounds, _statuses in derived.values():
            union.update(reads)
        observed = read_context.snapshot(union)
        for slot_key, (reads, group_boundaries, group_statuses) in derived.items():
            group = live_groups[slot_key]
            remembered_slot = _RememberedSlot(
                members=_slot_members(group),
                reads=reads,
                observed={
                    **{read: observed.get(read) for read in (*reads.keys(), ("compiler",))},
                    # Availability as the verdicts used it, not as re-read now.
                    **{
                        ("capture", digest): used
                        for digest, used in reads.used_availability.items()
                    },
                },
                interval=invariance_interval(group_boundaries, evaluation_time=evaluation_time),
                statuses=dict(group_statuses),
                verdicts={
                    claim.identity.qualified: verdicts[claim.identity.qualified]
                    for claim in group
                    if claim.identity.qualified in verdicts
                },
            )
            memo_put(
                _SLOT_MEMO,
                (root, compiler, slot_key),
                remembered_slot,
                capacity=_SLOT_MEMO_CAPACITY,
            )
    fingerprints = tuple(sorted(bodies_read.body_identities.items()))
    # Only a derivation whose every body read has the identity it used, and
    # whose bodies still have those identities now, is remembered: anything
    # that moved between a read and this insert forgoes the memo.
    if (
        remember
        and bodies_read.bodies_complete
        and body_fingerprints_hold(instance, fingerprints, store=read_context.body_store())
    ):
        memo_put(
            _RESOLUTION_MEMO,
            key,
            (
                dict(statuses),
                dict(verdicts),
                invariance_interval(boundaries, evaluation_time=evaluation_time),
                fingerprints,
            ),
            capacity=MEMO_CAPACITY,
        )
    if time_boundaries is not None:
        time_boundaries.update(boundaries)
    return statuses


def _slot_members(group: list[ClaimArtifactAny]) -> tuple[str, ...]:
    return tuple(sorted(claim.identity.qualified for claim in group))


def _apply_resolution_statuses(
    resolution: PlaybillClaimGroupResolution,
    statuses: dict[str, ClaimStatus],
    *,
    slots: Mapping[str, ClaimSlotClassification],
) -> None:
    selected = {item.removeprefix("Claim:") for item in resolution.selected_claim_identities}
    for claim, verdict in zip(resolution.claims, resolution.verdicts, strict=True):
        if verdict.verdict not in {"supported", "uncovered"}:
            status: ClaimStatus = "refused"
        elif resolution.status == "unresolved":
            status = (
                "conflicted"
                if slots[claim.identity.name].resolution == "unresolved"
                else "accepted"
            )
        elif claim.identity.name in selected:
            status = "accepted"
        elif resolution.cardinality == "many":
            # A many-cardinality slot selects every eligible contender, so a
            # Claim that is not selected lost to nothing: it failed its own
            # ClaimType's admission, and "overturned" would name a rival that
            # does not exist.
            status = "refused"
        else:
            status = "overturned"
        statuses[claim.identity.name] = status


__all__ = [
    "ClaimStatus",
    "claim_resolution_statuses",
    "remembered_resolution_statuses",
    "reset_claim_resolution_memo",
]
