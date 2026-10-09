"""The post-hoc duplicates audit (identity-design-1009): possible duplicates of one kind.

Computed on demand at an accepted coordinate, never on the write path, and
bounded: it reads at most ``DUPLICATES_AUDIT_MAX_SCANNED`` Subjects, aliases
and values, and says when it stopped. Groups are the things equal under the
broader comparison (``DUPLICATES_AUDIT_BROAD_FORM`` plus the alias graph) but
distinct under the declared rules; an agent resolves each with an alias and a
retirement, ``semantic.distinct_from`` (which suppresses the pair thereafter),
or a governed canonical-form succession. Core never resolves one itself.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import TypeVar

from cruxible_client.contracts.canonical_forms import (
    CanonicalFormDidNotConverge,
    canonicalize_text,
)
from cruxible_client.contracts.claim_types import effective_canonical_form
from cruxible_client.contracts.duplicates import (
    DUPLICATES_AUDIT_BROAD_FORM,
    DUPLICATES_AUDIT_MAX_SCANNED,
    DuplicateGroup,
    DuplicatesAuditRequest,
    DuplicatesAuditResult,
    DuplicatesAuditScanned,
    DuplicateSubject,
    DuplicateSubjectGroup,
    DuplicateValue,
    DuplicateValueGroup,
)
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.discovery.query_values import read_live_values
from cruxible_core.service.discovery.query_vocabulary import load_query_vocabulary
from cruxible_core.service.list_pages import (
    decode_list_cursor,
    encode_list_cursor,
    list_snapshot,
    page_after_boundary,
)

_LIST = "duplicates audit"
_T = TypeVar("_T")
_DISTINCT_FROM = "semantic.distinct_from"


def broad_key(text: str) -> str:
    """The broader comparison's spelling of one name or value."""

    try:
        return canonicalize_text(text, DUPLICATES_AUDIT_BROAD_FORM)
    except CanonicalFormDidNotConverge:
        return text


def _ref(path: str) -> str:
    return path.removeprefix("subjects/").removesuffix(".json")


def _components(members: Iterable[str], apart: set[frozenset[str]]) -> list[list[str]]:
    """Connected groups of ``members``, every pair joined unless asserted distinct."""

    ordered = sorted(set(members))
    present = set(ordered)
    if not any(pair <= present for pair in apart):
        # Nothing asserted distinct among them: one group, without pairwise work.
        return [ordered] if len(ordered) > 1 else []
    parent = {member: member for member in ordered}

    def find(item: str) -> str:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    for index, first in enumerate(ordered):
        for second in ordered[index + 1 :]:
            if frozenset({first, second}) not in apart:
                parent[find(second)] = find(first)
    groups: dict[str, list[str]] = defaultdict(list)
    for member in ordered:
        groups[find(member)].append(member)
    return [group for group in groups.values() if len(group) > 1]


class _Budget:
    def __init__(self, limit: int) -> None:
        self.remaining = limit
        self.exhausted = False

    def take(self, rows: list[_T]) -> list[_T]:
        if len(rows) > self.remaining:
            self.exhausted = True
            rows = rows[: self.remaining]
        self.remaining -= len(rows)
        return rows


def _audit_groups(
    instance: PlaybillInstance,
    coordinate: AcceptedProjectionCoordinate,
    kind: str,
    *,
    budget: _Budget,
) -> tuple[list[DuplicateGroup], DuplicatesAuditScanned]:
    vocabulary = load_query_vocabulary(instance, coordinate)
    vocabulary.require_kind(kind)
    prefix = f"subjects/{kind}/"
    high = prefix[:-1] + chr(ord("/") + 1)
    with instance.bind_accepted_projection(coordinate) as projection:
        connection = projection.typed.connection
        subject_rows: list[tuple[str, str]] = budget.take(
            list(
                connection.execute(
                    "SELECT path, subject_id FROM subjects WHERE path>=? AND path<? "
                    "AND lifecycle='live' ORDER BY path LIMIT ?",
                    (prefix, high, budget.remaining + 1),
                )
            )
        )
        alias_rows: list[tuple[str, str]] = budget.take(
            list(
                connection.execute(
                    "SELECT target_path, value FROM vocabulary_terms WHERE basis='alias' "
                    "AND target_path>=? AND target_path<? ORDER BY target_path, value LIMIT ?",
                    (prefix, high, budget.remaining + 1),
                )
            )
        )
        apart = {
            frozenset({str(row[0]), str(row[1])})
            for row in connection.execute(
                "SELECT subject_path, object_path FROM claims WHERE predicate=? "
                "AND lifecycle='live' AND object_path IS NOT NULL "
                "AND ((subject_path>=? AND subject_path<?) OR (object_path>=? AND object_path<?))",
                (_DISTINCT_FROM, prefix, high, prefix, high),
            )
        }
    names: dict[str, set[str]] = defaultdict(set)
    for path, subject_id in subject_rows:
        names[str(path)].add(str(subject_id))
    for path, value in alias_rows:
        if str(path) in names:
            names[str(path)].add(str(value))
    by_key: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for path, spelled in names.items():
        for name in spelled:
            by_key[broad_key(name)][path].add(name)
    groups: list[DuplicateGroup] = []
    for key in sorted(by_key):
        holders = by_key[key]
        for component in _components(holders, apart):
            groups.append(
                DuplicateSubjectGroup(
                    key=key,
                    subjects=tuple(
                        DuplicateSubject(subject=_ref(path), names=tuple(sorted(holders[path])))
                        for path in component
                    ),
                )
            )
    literal = [
        info
        for info in vocabulary.predicates_of(kind)
        if info.claim_type.object_kind == "literal"
        and info.value_type in {"string", "enum", "json"}
    ]
    values = (
        read_live_values(
            instance,
            coordinate,
            subject_paths=tuple(sorted(names)),
            predicates=tuple(info.predicate for info in literal),
        )
        if literal and names
        else []
    )
    taken = budget.take([item for item in values if isinstance(item.value, str)])
    by_value: dict[tuple[str, str], list[DuplicateValue]] = defaultdict(list)
    for item in taken:
        value = str(item.value)
        by_value[(item.predicate, broad_key(value))].append(
            DuplicateValue(
                claim=str(item.identity).removeprefix("Claim:"),
                subject=_ref(item.subject_path),
                value=value,
            )
        )
    forms = {info.predicate: effective_canonical_form(info.claim_type) for info in literal}
    for (predicate, key), held in sorted(by_value.items()):
        if len({item.value for item in held}) < 2:
            continue
        groups.append(
            DuplicateValueGroup(
                predicate=predicate,
                key=key,
                canonical_form=forms[predicate],
                values=tuple(sorted(held, key=lambda item: (item.value, item.claim))),
            )
        )
    scanned = DuplicatesAuditScanned(
        subjects=len(subject_rows), aliases=len(alias_rows), values=len(taken)
    )
    return groups, scanned


def _group_key(group: DuplicateGroup) -> tuple[str, ...]:
    if isinstance(group, DuplicateSubjectGroup):
        return ("subjects", group.key, group.subjects[0].subject)
    return ("values", group.predicate, group.key)


def service_duplicates_audit(
    instance: PlaybillInstance,
    *,
    request: DuplicatesAuditRequest | Mapping[str, object],
) -> DuplicatesAuditResult:
    """One page of possible duplicates of one kind; a pure, bounded read."""

    request = (
        request
        if isinstance(request, DuplicatesAuditRequest)
        else DuplicatesAuditRequest.model_validate(request)
    )
    selection = {"kind": request.kind}
    continuation = (
        None
        if request.cursor is None
        else decode_list_cursor(request.cursor, list_name=_LIST, selection=selection)
    )
    coordinate = (
        instance.accepted_coordinate()
        if continuation is None
        else instance.resolve_accepted_coordinate(
            git_oid=str(continuation.coordinate["git_oid"]),
            semantic_root=str(continuation.coordinate["semantic_root"]),
            generation_root=str(continuation.coordinate["generation_root"]),
        )
    )
    budget = _Budget(DUPLICATES_AUDIT_MAX_SCANNED)
    groups, scanned = _audit_groups(instance, coordinate, request.kind, budget=budget)
    keys = [_group_key(group) for group in groups]
    snapshot = list_snapshot([group.model_dump(mode="json") for group in groups])
    page, more = page_after_boundary(
        groups,
        keys=keys,
        snapshot=snapshot,
        continuation=continuation,
        limit=request.limit,
        list_name=_LIST,
    )
    next_cursor = None
    if more and page:
        next_cursor = encode_list_cursor(
            list_name=_LIST,
            coordinate={
                "git_oid": coordinate.git_oid,
                "semantic_root": coordinate.semantic_root,
                "generation_root": coordinate.generation_root,
            },
            selection=selection,
            snapshot=snapshot,
            last_key=_group_key(page[-1]),
        )
    return DuplicatesAuditResult(
        kind=request.kind,
        git_oid=coordinate.git_oid,
        groups=page,
        scanned=scanned,
        budget_exhausted=budget.exhausted,
        truncated=more,
        next_cursor=next_cursor,
    )


__all__ = ["broad_key", "service_duplicates_audit"]
