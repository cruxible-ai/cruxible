"""Deterministic search/list/orient over accepted Claims and Procedures."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

from cruxible_client.contracts.canonical import canonical_bytes
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
    PlaybillClaimQueryResult,
    PlaybillClaimQueryResultV2,
    _claim_from_view,
    service_list_playbill_claims,
    service_query_playbill_claims,
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
) -> dict[str, SearchStatus]:
    """Derive each Claim's resolution status at one accepted coordinate."""

    live_groups: dict[bytes, list[ClaimArtifactAny]] = defaultdict(list)
    statuses: dict[str, SearchStatus] = {}
    for claim in claims:
        if claim.lifecycle.state == "retired":
            statuses[claim.identity.name] = "retired"
        else:
            live_groups[_resolution_key(claim)].append(claim)

    for group in live_groups.values():
        first = group[0]
        result = service_query_playbill_claims(
            instance,
            subject=first.statement.subject,
            predicate=first.statement.predicate,
            at=at,
            evaluation_time=evaluation_time,
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
        _apply_resolution_statuses(result, statuses, slots=slots)
    return statuses


def _apply_resolution_statuses(
    result: PlaybillClaimQueryResult | PlaybillClaimQueryResultV2,
    statuses: dict[str, SearchStatus],
    *,
    slots: Mapping[str, ClaimSlotClassification],
) -> None:
    selected = {item.removeprefix("Claim:") for item in result.selected_claim_identities}
    for view, verdict in zip(result.claims, result.verdicts, strict=True):
        claim = _claim_from_view(view)
        if verdict.verdict not in {"supported", "uncovered"}:
            status: SearchStatus = "refused"
        elif result.status == "unresolved":
            status = (
                "conflicted"
                if slots[claim.identity.name].resolution == "unresolved"
                else "accepted"
            )
        elif claim.identity.name in selected:
            status = "accepted"
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
        if not path.startswith("procedures/") or not path.endswith(".yaml"):
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
