"""Deterministic search/list/orient over accepted Claims and Procedures."""

from __future__ import annotations

from collections import Counter, OrderedDict, defaultdict
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claim_types import claim_type_path
from cruxible_client.contracts.claim_verdicts import ClaimVerdictResultAny
from cruxible_client.contracts.claims import (
    ClaimArtifactAny,
    SubjectClaimObject,
    claim_path,
)
from cruxible_client.contracts.discovery import DiscoveryMatchBasisV1
from cruxible_client.contracts.errors import PlaybillError, ProposalIntegrityError
from cruxible_client.contracts.semantic import SemanticAddress, SemanticSelector
from cruxible_core.claims.claim_slots import ClaimSlotClassification, classify_claim_slot
from cruxible_core.derived.memo import memo_get, memo_put
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.query.search import (
    SEARCH_KINDS,
    PlaybillSearchCountV1,
    PlaybillSearchFollowUpV1,
    PlaybillSearchKindAvailabilityV1,
    PlaybillSearchOrientationV1,
    PlaybillSearchRequestV1,
    PlaybillSearchResultV1,
    PlaybillSearchRowV1,
    SearchKind,
    SearchStatus,
    build_playbill_search_cursor,
    build_playbill_search_result,
    playbill_search_result_bytes,
    playbill_search_selection_basis_digest,
)
from cruxible_core.query.semantic_discovery import (
    MATCH_BASIS_PRIORITY,
    discovery_tokens,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import PlaybillAcceptedCoordinate
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
from cruxible_core.service.evidence.evidence import ClaimVerdictReadContext, VerdictReads


class PlaybillSearchError(PlaybillError):
    """A search cursor, budget, or accepted coordinate cannot be honored."""


class DemandSearchProviderProtocol(Protocol):
    """Closed seam populated only after the accepted demand-policy wire lands."""

    def rows(
        self,
        instance: PlaybillInstance,
        request: PlaybillSearchRequestV1,
    ) -> tuple[PlaybillSearchRowV1, ...]: ...


def _accepted_coordinate(request: PlaybillSearchRequestV1) -> PlaybillAcceptedCoordinate:
    return PlaybillAcceptedCoordinate.model_validate(
        request.accepted_coordinate.model_dump(mode="json")
    )


# Bounded, per-process, and keyed on every input the derivation reads. See
# `playbill_verdict_memo` for why each part of the key is there.
_RESOLUTION_MEMO: (
    "OrderedDict[tuple[str, str, str, str], "
    "tuple[dict[str, SearchStatus], dict[str, ClaimVerdictResultAny], "
    "tuple[datetime | None, datetime | None]]]"
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
    statuses: dict[str, SearchStatus]
    verdicts: dict[str, ClaimVerdictResultAny]


def reset_claim_resolution_memo(*, slots: bool = True) -> None:
    """Forget remembered derivations; activation keeps the validated slot answers."""

    _RESOLUTION_MEMO.clear()
    if slots:
        _SLOT_MEMO.clear()


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
    at: PlaybillAcceptedCoordinate,
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
    at: PlaybillAcceptedCoordinate,
    evaluation_time: datetime,
) -> dict[str, SearchStatus] | None:
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
    if remembered is None or not interval_holds(remembered[2], evaluation_time=evaluation_time):
        return None
    return dict(remembered[0])


def claim_resolution_statuses(
    instance: PlaybillInstance,
    *,
    claims: tuple[ClaimArtifactAny, ...],
    at: PlaybillAcceptedCoordinate,
    evaluation_time: datetime,
    verdicts_by_identity: MutableMapping[str, ClaimVerdictResultAny] | None = None,
    read_context: ClaimVerdictReadContext | None = None,
) -> dict[str, SearchStatus]:
    """Derive each Claim's resolution status at one accepted coordinate.

    ``verdicts_by_identity`` lets one request share Claim verdicts with another
    fold over the same coordinate and evaluation time; it must never outlive
    that pair.

    The whole derivation is memoized per process on the instance, the accepted
    coordinate, the exact Claim set, and CAS shard metadata for live replay
    availability. Pending door attestations are not an input. The evaluation instant is NOT in
    the key: every real surface stamps a fresh `utc_now()`, so a wall-clock key
    could never be hit twice. A verdict is a step function of time whose only
    breakpoints are the instants it compares against, so the entry carries the
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
        memoized_statuses, memoized_verdicts, interval = remembered
        if interval_holds(interval, evaluation_time=evaluation_time):
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
    statuses: dict[str, SearchStatus] = {}
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
    if remember:
        candidates: dict[bytes, _RememberedSlot] = {}
        for slot_key, group in live_groups.items():
            entry = _SLOT_MEMO.get((root, compiler, slot_key))
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
            current = read_context.snapshot(union)
            for slot_key, entry in candidates.items():
                if all(current.get(read) == value for read, value in entry.observed.items()):
                    _SLOT_MEMO.move_to_end((root, compiler, slot_key))
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
    derived: dict[bytes, tuple[VerdictReads, set[datetime], dict[str, SearchStatus]]] = {}
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
                time_boundaries=group_boundaries if remember else None,
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
        group_statuses: dict[str, SearchStatus] = {}
        _apply_resolution_statuses(resolution, group_statuses, slots=slots)
        statuses.update(group_statuses)
        if reads is not None:
            derived[slot_key] = (reads, group_boundaries, group_statuses)
    if derived:
        union = VerdictReads()
        for reads, _bounds, _statuses in derived.values():
            union.update(reads)
        observed = read_context.snapshot(union)
        for slot_key, (reads, group_boundaries, group_statuses) in derived.items():
            group = live_groups[slot_key]
            _SLOT_MEMO[(root, compiler, slot_key)] = _RememberedSlot(
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
            _SLOT_MEMO.move_to_end((root, compiler, slot_key))
        while len(_SLOT_MEMO) > _SLOT_MEMO_CAPACITY:
            _SLOT_MEMO.popitem(last=False)
    if remember:
        memo_put(
            _RESOLUTION_MEMO,
            key,
            (
                dict(statuses),
                dict(verdicts),
                invariance_interval(boundaries, evaluation_time=evaluation_time),
            ),
            capacity=MEMO_CAPACITY,
        )
    return statuses


def _slot_members(group: list[ClaimArtifactAny]) -> tuple[str, ...]:
    return tuple(sorted(claim.identity.qualified for claim in group))


def _apply_resolution_statuses(
    resolution: PlaybillClaimGroupResolution,
    statuses: dict[str, SearchStatus],
    *,
    slots: Mapping[str, ClaimSlotClassification],
) -> None:
    selected = {item.removeprefix("Claim:") for item in resolution.selected_claim_identities}
    for claim, verdict in zip(resolution.claims, resolution.verdicts, strict=True):
        if verdict.verdict not in {"supported", "uncovered"}:
            status: SearchStatus = "refused"
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


def _claim_rows(
    instance: PlaybillInstance,
    *,
    request: PlaybillSearchRequestV1,
) -> tuple[PlaybillSearchRowV1, ...]:
    if "claim" not in request.kinds:
        return ()
    coordinate = _resolve_coordinate(instance, _accepted_coordinate(request))
    if request.mode == "orient" and request.subject is None:
        remembered = _remembered_orientation_rows(instance, coordinate, request=request)
        if remembered is not None:
            return remembered
    read_context = ClaimVerdictReadContext(instance, coordinate)
    # Discovery needs Claim envelopes and current status, not the full fact
    # projection (including provenance/explanation payloads) for every row.
    # Resolution groups are keyed by subject and predicate, so a subject filter
    # keeps every contender group whole: read and evaluate only those groups.
    claims = read_context.claims(subject=request.subject)
    statuses = claim_resolution_statuses(
        instance,
        claims=claims,
        at=_accepted_coordinate(request),
        evaluation_time=request.evaluation_time,
        read_context=read_context,
    )
    rows: list[PlaybillSearchRowV1] = []
    for claim in claims:
        kind: SearchKind = "claim"
        if kind not in request.kinds:
            continue
        rows.append(
            PlaybillSearchRowV1(
                kind=kind,
                identity=claim.identity.name,
                address=SemanticAddress.claim_statement(claim_path(claim.identity.name)),
                status=statuses[claim.identity.name],
                subject=claim.statement.subject,
                predicate=claim.statement.predicate,
                title=claim.statement.predicate,
            )
        )
    return tuple(rows)


def _remembered_orientation_rows(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    *,
    request: PlaybillSearchRequestV1,
) -> tuple[PlaybillSearchRowV1, ...] | None:
    """Orientation only counts rows, so a remembered answer needs no Claim bytes.

    The indexed Claim rows at this exact coordinate name the same Claim set a
    full read would; when their statuses are remembered and still valid, rows
    are built from the index alone. Any miss takes the ordinary read.
    """

    with instance.bind_accepted_projection(coordinate) as projection:
        indexed = projection.typed.connection.execute(
            "SELECT identity,subject_path,subject_selector_scheme,subject_selector_value,"
            "predicate FROM claims ORDER BY identity"
        ).fetchall()
    identities = tuple(row[0] for row in indexed)
    statuses = remembered_resolution_statuses(
        instance,
        identities=identities,
        at=_accepted_coordinate(request),
        evaluation_time=request.evaluation_time,
    )
    if statuses is None or set(statuses) != {i.removeprefix("Claim:") for i in identities}:
        return None
    return tuple(
        PlaybillSearchRowV1(
            kind="claim",
            identity=identity.removeprefix("Claim:"),
            address=SemanticAddress.claim_statement(claim_path(identity.removeprefix("Claim:"))),
            status=statuses[identity.removeprefix("Claim:")],
            subject=SemanticAddress(
                artifact_path=subject_path,
                selector=SemanticSelector(scheme=scheme, value=value),
            ),
            predicate=predicate,
            title=predicate,
        )
        for identity, subject_path, scheme, value, predicate in indexed
    )


def _procedure_rows(
    instance: PlaybillInstance,
    *,
    coordinate: AcceptedProjectionCoordinate,
    request: PlaybillSearchRequestV1,
) -> tuple[PlaybillSearchRowV1, ...]:
    if "procedure" not in request.kinds:
        return ()
    with instance.bind_accepted_projection(coordinate) as projection:
        inventory = projection.typed.procedure_inventory()
    return tuple(
        PlaybillSearchRowV1(
            kind="procedure",
            identity=procedure.identity.removeprefix("Procedure:"),
            address=SemanticAddress.whole_artifact(procedure.path),
            status="accepted" if procedure.lifecycle == "live" else "retired",
            title=procedure.identity.removeprefix("Procedure:"),
            summary=("directly_runnable" if procedure.directly_runnable else "binding_required"),
        )
        for procedure in inventory
    )


def _match_basis(row: PlaybillSearchRowV1, query: str) -> tuple[DiscoveryMatchBasisV1, ...]:
    bases: list[DiscoveryMatchBasisV1] = []
    exact_terms = {
        row.identity.casefold(),
        row.address.artifact_path.casefold(),
        f"{row.kind}:{row.identity}".casefold(),
    }
    if query in exact_terms:
        bases.append(DiscoveryMatchBasisV1(basis="exact_address", matched_text=query))
    query_tokens = set(discovery_tokens(query))
    searchable = " ".join(
        item
        for item in (
            row.identity,
            row.title,
            row.summary,
            row.predicate,
            None if row.subject is None else row.subject.artifact_path,
        )
        if item is not None
    )
    if query_tokens and query_tokens.issubset(set(discovery_tokens(searchable))):
        bases.append(DiscoveryMatchBasisV1(basis="lexical", matched_text=query))
    return tuple(
        sorted(
            set(bases),
            key=lambda item: (
                MATCH_BASIS_PRIORITY[item.basis],
                (item.matched_text or "").encode("utf-8"),
            ),
        )
    )


def _row_priority(row: PlaybillSearchRowV1, mode: str) -> int:
    if mode != "search":
        return 0
    return min(MATCH_BASIS_PRIORITY[item.basis] for item in row.match_basis)


def _filtered_rows(
    rows: tuple[PlaybillSearchRowV1, ...],
    *,
    request: PlaybillSearchRequestV1,
) -> tuple[PlaybillSearchRowV1, ...]:
    selected: list[PlaybillSearchRowV1] = []
    for row in rows:
        if request.subject is not None and row.subject != request.subject:
            continue
        if request.statuses and row.status not in request.statuses:
            continue
        if request.mode == "search":
            assert request.query is not None
            bases = _match_basis(row, request.query)
            if not bases:
                continue
            row = row.model_copy(update={"match_basis": bases})
        selected.append(row)
    return tuple(
        sorted(
            selected,
            key=lambda item: (
                _row_priority(item, request.mode),
                item.kind.encode("utf-8"),
                item.identity.encode("utf-8"),
            ),
        )
    )


def _availability(
    demand_provider: DemandSearchProviderProtocol | None,
) -> tuple[PlaybillSearchKindAvailabilityV1, ...]:
    return tuple(
        PlaybillSearchKindAvailabilityV1(
            kind=kind,
            availability=(
                "not_installed" if kind == "demand" and demand_provider is None else "installed"
            ),
        )
        for kind in SEARCH_KINDS
    )


def _orientation(
    instance: PlaybillInstance,
    *,
    request: PlaybillSearchRequestV1,
    rows: tuple[PlaybillSearchRowV1, ...],
    demand_provider: DemandSearchProviderProtocol | None,
) -> PlaybillSearchOrientationV1:
    kinds = Counter(row.kind for row in rows)
    statuses = Counter(row.status for row in rows)
    availability = _availability(demand_provider)
    generation = next(
        item.sequence
        for item in instance.accepted_history()
        if item.oid == request.accepted_coordinate.git_oid
    )
    return PlaybillSearchOrientationV1(
        coordinate=request.accepted_coordinate,
        generation=generation,
        counts_by_kind=tuple(
            PlaybillSearchCountV1(key=kind, count=kinds.get(kind, 0)) for kind in request.kinds
        ),
        counts_by_status=tuple(
            PlaybillSearchCountV1(key=status, count=statuses[status])
            for status in sorted(statuses, key=lambda item: item.encode("utf-8"))
        ),
        conflicted_count=sum(row.status == "conflicted" for row in rows),
        decommissioned=instance.is_decommissioned,
        mirror_url=instance.ledger_mirror_url(),
        available_kinds=tuple(
            item.kind for item in availability if item.availability == "installed"
        ),
        kind_availability=availability,
        truncated=False,
        follow_ups=(
            PlaybillSearchFollowUpV1(
                mode="list",
                kinds=request.kinds,
                statuses=request.statuses,
                subject=request.subject,
            ),
            PlaybillSearchFollowUpV1(
                mode="search",
                kinds=request.kinds,
                statuses=request.statuses,
                subject=request.subject,
            ),
        ),
    )


def _page_result(
    *,
    request: PlaybillSearchRequestV1,
    rows: tuple[PlaybillSearchRowV1, ...],
    selection_basis_digest: str,
) -> PlaybillSearchResultV1:
    start = 0
    if request.cursor is not None:
        cursor = request.cursor
        if (
            cursor.selection_basis_digest != selection_basis_digest
            or cursor.coordinate != request.accepted_coordinate
            or cursor.budgets != request.budgets
        ):
            raise PlaybillSearchError("search cursor belongs to a different request coordinate")
        cursor_key = (
            cursor.last_match_priority,
            cursor.last_kind.encode("utf-8"),
            cursor.last_identity.encode("utf-8"),
        )
        keys = tuple(
            (
                _row_priority(row, request.mode),
                row.kind.encode("utf-8"),
                row.identity.encode("utf-8"),
            )
            for row in rows
        )
        if cursor_key not in keys:
            raise PlaybillSearchError("search cursor boundary is absent from its bound result")
        start = keys.index(cursor_key) + 1

    page = list(rows[start : start + request.budgets.max_rows])
    while True:
        more = start + len(page) < len(rows)
        next_cursor = (
            None
            if not more or not page
            else build_playbill_search_cursor(
                selection_basis_digest=selection_basis_digest,
                coordinate=request.accepted_coordinate,
                last_match_priority=_row_priority(page[-1], request.mode),
                last_kind=page[-1].kind,
                last_identity=page[-1].identity,
                budgets=request.budgets,
            )
        )
        result = build_playbill_search_result(
            mode=request.mode,
            coordinate=request.accepted_coordinate,
            evaluation_time=request.evaluation_time,
            rows=tuple(page),
            orientation=None,
            selection_basis_digest=selection_basis_digest,
            next_cursor=next_cursor,
            truncated=more,
        )
        if len(playbill_search_result_bytes(result)) <= request.budgets.max_result_bytes:
            return result
        if not page:
            raise PlaybillSearchError("search result byte budget cannot fit its envelope")
        page.pop()


def service_search_playbill(
    instance: PlaybillInstance,
    *,
    request: PlaybillSearchRequestV1,
    demand_provider: DemandSearchProviderProtocol | None = None,
) -> PlaybillSearchResultV1:
    """Return one byte-deterministic discovery answer without writing daemon state."""

    try:
        coordinate = instance.resolve_accepted_coordinate(
            git_oid=request.accepted_coordinate.git_oid,
            semantic_root=request.accepted_coordinate.semantic_root,
            generation_root=request.accepted_coordinate.generation_root,
            compiler_digest=request.accepted_coordinate.compiler_digest,
        )
    except PlaybillError:
        raise
    except Exception as exc:  # pragma: no cover - backend normalization boundary
        raise ProposalIntegrityError("search requires a verified accepted coordinate") from exc
    rows = (
        *_claim_rows(instance, request=request),
        *_procedure_rows(instance, coordinate=coordinate, request=request),
    )
    if demand_provider is not None and "demand" in request.kinds:
        demand_rows = demand_provider.rows(instance, request)
        if any(row.kind != "demand" for row in demand_rows):
            raise PlaybillSearchError("demand provider returned a non-demand row")
        rows = (*rows, *demand_rows)
    filtered = _filtered_rows(rows, request=request)
    selection_basis_digest = playbill_search_selection_basis_digest(request)
    if request.mode == "orient":
        orientation = _orientation(
            instance,
            request=request,
            rows=filtered,
            demand_provider=demand_provider,
        )
        result = build_playbill_search_result(
            mode="orient",
            coordinate=request.accepted_coordinate,
            evaluation_time=request.evaluation_time,
            rows=(),
            orientation=orientation,
            selection_basis_digest=selection_basis_digest,
            next_cursor=None,
            truncated=False,
        )
        if len(playbill_search_result_bytes(result)) > request.budgets.max_result_bytes:
            raise PlaybillSearchError("orientation byte budget cannot fit its complete summary")
        return result
    return _page_result(
        request=request,
        rows=filtered,
        selection_basis_digest=selection_basis_digest,
    )


__all__ = [
    "DemandSearchProviderProtocol",
    "PlaybillSearchError",
    "claim_resolution_statuses",
    "service_search_playbill",
]
