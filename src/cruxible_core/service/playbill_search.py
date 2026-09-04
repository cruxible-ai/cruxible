"""Deterministic search/list/orient over accepted Claims and Procedures."""

from __future__ import annotations

from collections import Counter, OrderedDict, defaultdict
from collections.abc import Mapping, MutableMapping
from datetime import datetime
from typing import Protocol

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claim_verdicts import ClaimVerdictResultAny
from cruxible_client.contracts.claims import (
    ClaimArtifactAny,
    claim_path,
)
from cruxible_client.contracts.discovery import DiscoveryMatchBasisV1
from cruxible_client.contracts.errors import PlaybillError, ProposalIntegrityError
from cruxible_client.contracts.procedures.artifacts import parse_procedure
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_core.playbill.claim_slots import ClaimSlotClassification, classify_claim_slot
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.memo import memo_get, memo_put
from cruxible_core.playbill.query.semantic_discovery import (
    MATCH_BASIS_PRIORITY,
    discovery_tokens,
)
from cruxible_core.playbill.search import (
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
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.service.playbill_claims import (
    PlaybillClaimGroupResolution,
    _claim_from_view,
    _resolve_coordinate,
    resolve_playbill_claim_group,
    service_list_playbill_claims,
)
from cruxible_core.service.playbill_verdict_memo import (
    MEMO_CAPACITY,
    claim_set_digest,
    memo_key,
    verdict_input_fingerprint,
)


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
    "tuple[dict[str, SearchStatus], dict[str, ClaimVerdictResultAny]]]"
) = OrderedDict()


def reset_claim_resolution_memo() -> None:
    """Forget every remembered derivation; activation and tests call this."""

    _RESOLUTION_MEMO.clear()


def _resolution_key(claim: ClaimArtifactAny) -> bytes:
    return canonical_bytes(
        {
            "predicate": claim.statement.predicate,
            "subject": claim.statement.subject.model_dump(mode="json"),
        }
    )


def claim_resolution_statuses(
    instance: PlaybillInstance,
    *,
    claims: tuple[ClaimArtifactAny, ...],
    at: PlaybillAcceptedCoordinate,
    evaluation_time: datetime,
    verdicts_by_identity: MutableMapping[str, ClaimVerdictResultAny] | None = None,
) -> dict[str, SearchStatus]:
    """Derive each Claim's resolution status at one accepted coordinate.

    ``verdicts_by_identity`` lets one request share Claim verdicts with another
    fold over the same coordinate and evaluation time; it must never outlive
    that pair.

    The whole derivation is memoized per process on the coordinate, the
    evaluation instant, the exact Claim set, and a fingerprint of the two stores
    a verdict reads besides the accepted tree. An `orient` is a READ, and this
    read used to cross the client's own default timeout at a few hundred Claims;
    a second read of the same state now evaluates no verdicts at all. Nothing
    depends on the memo: it is per-process, cold after a restart, and bounded.
    """

    key = memo_key(
        coordinate_digest=canonical_bytes(at.model_dump(mode="json")).hex(),
        evaluation_time=evaluation_time.isoformat(),
        claim_set_digest=claim_set_digest(tuple(claim.identity.qualified for claim in claims)),
        input_fingerprint=verdict_input_fingerprint(instance),
    )
    remembered = memo_get(_RESOLUTION_MEMO, key)
    if remembered is not None:
        memoized_statuses, memoized_verdicts = remembered
        if verdicts_by_identity is not None:
            verdicts_by_identity.update(memoized_verdicts)
        return dict(memoized_statuses)

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
    for group in live_groups.values():
        first = group[0]
        resolution = resolve_playbill_claim_group(
            instance,
            subject=first.statement.subject,
            predicate=first.statement.predicate,
            coordinate=coordinate,
            evaluated_at=evaluation_time,
            claims=tuple(group),
            verdicts_by_identity=verdicts,
        )
        groups_by_qualifier: dict[str | None, list[ClaimArtifactAny]] = defaultdict(list)
        for claim in group:
            groups_by_qualifier[claim.statement.qualifier].append(claim)
        slots = {
            claim.identity.name: classification
            for members in groups_by_qualifier.values()
            for classification in (classify_claim_slot(members),)
            for claim in members
        }
        _apply_resolution_statuses(resolution, statuses, slots=slots)
    memo_put(
        _RESOLUTION_MEMO,
        key,
        (dict(statuses), dict(verdicts)),
        capacity=MEMO_CAPACITY,
    )
    return statuses


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
    listed = service_list_playbill_claims(
        instance,
        at=_accepted_coordinate(request),
        include_retired=True,
    )
    claims = tuple(_claim_from_view(view) for view in listed.claims)
    statuses = claim_resolution_statuses(
        instance,
        claims=claims,
        at=_accepted_coordinate(request),
        evaluation_time=request.evaluation_time,
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


def _procedure_rows(
    tree: Mapping[str, bytes],
    *,
    request: PlaybillSearchRequestV1,
) -> tuple[PlaybillSearchRowV1, ...]:
    if "procedure" not in request.kinds:
        return ()
    rows: list[PlaybillSearchRowV1] = []
    for path in sorted(tree, key=lambda item: item.encode("utf-8")):
        if not path.startswith("procedures/") or not path.endswith(".json"):
            continue
        procedure = parse_procedure(tree[path], path=path)
        rows.append(
            PlaybillSearchRowV1(
                kind="procedure",
                identity=procedure.identity.name,
                address=SemanticAddress.whole_artifact(path),
                status="accepted" if procedure.lifecycle.state == "live" else "retired",
                title=procedure.identity.name,
                summary=(
                    "directly_runnable" if procedure.directly_runnable else "binding_required"
                ),
            )
        )
    return tuple(rows)


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
    tree = instance.tree_at(coordinate.git_oid)
    rows = (*_claim_rows(instance, request=request), *_procedure_rows(tree, request=request))
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
