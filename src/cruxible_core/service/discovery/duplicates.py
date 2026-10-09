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

import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping

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
from cruxible_core.service.discovery.query_vocabulary import load_query_vocabulary
from cruxible_core.service.list_pages import (
    decode_list_cursor,
    encode_list_cursor,
    list_snapshot,
    page_after_boundary,
)

_LIST = "duplicates audit"
_DISTINCT_FROM = "semantic.distinct_from"


def broad_key(text: str) -> str:
    """The broader comparison's spelling of one name or value."""

    try:
        return canonicalize_text(text, DUPLICATES_AUDIT_BROAD_FORM)
    except CanonicalFormDidNotConverge:
        return text


def _ref(path: str) -> str:
    return path.removeprefix("subjects/").removesuffix(".json")


def _groups_apart(members: Iterable[str], apart: Mapping[str, set[str]]) -> list[list[str]]:
    """Group the holders of one key so no group holds an asserted-distinct pair.

    The overlap contract: a key's holders are taken in path order, and each joins
    the first group none of whose members it is asserted distinct from, or opens
    a new one. Every holder is in exactly one group, no group ever holds a pair
    asserted ``semantic.distinct_from``, and groups of one are not duplicates. A
    holder distinct from no one costs one probe per group; the work is
    proportional to holders times groups, never to all pairs.
    """

    groups: list[list[str]] = []
    for member in sorted(set(members)):
        far = apart.get(member, set())
        for group in groups:
            if not far or far.isdisjoint(group):
                group.append(member)
                break
        else:
            groups.append([member])
    return [group for group in groups if len(group) > 1]


class _Budget:
    """The rows one audit may read, counted as SQL returns them.

    Each read asks for at most what remains plus one: an overflow sentinel. A
    read that returns the sentinel exhausts the budget; the sentinel is dropped
    from the answer and not counted, so no more than the budget is ever used.
    """

    def __init__(self, limit: int) -> None:
        self.remaining = limit
        self.exhausted = False

    def read(
        self, connection: sqlite3.Connection, sql: str, parameters: tuple[object, ...]
    ) -> list[tuple[object, ...]]:
        if self.exhausted:
            return []
        rows = [
            tuple(row)
            for row in connection.execute(sql + " LIMIT ?", (*parameters, self.remaining + 1))
        ]
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
    literal = [
        info
        for info in vocabulary.predicates_of(kind)
        if info.claim_type.object_kind == "literal"
        and info.value_type in {"string", "enum", "json"}
    ]
    with instance.bind_accepted_projection(coordinate) as projection:
        connection = projection.typed.connection
        subject_rows = budget.read(
            connection,
            "SELECT path, subject_id FROM subjects WHERE path>=? AND path<? "
            "AND lifecycle='live' ORDER BY path",
            (prefix, high),
        )
        alias_rows = budget.read(
            connection,
            "SELECT target_path, value FROM vocabulary_terms WHERE basis='alias' "
            "AND target_path>=? AND target_path<? ORDER BY target_path, value",
            (prefix, high),
        )
        relation_rows = budget.read(
            connection,
            "SELECT subject_path, object_path FROM claims WHERE predicate=? "
            "AND lifecycle='live' AND object_path IS NOT NULL "
            "AND ((subject_path>=? AND subject_path<?) OR (object_path>=? AND object_path<?)) "
            "ORDER BY identity",
            (_DISTINCT_FROM, prefix, high, prefix, high),
        )
        predicates = tuple(info.predicate for info in literal)
        value_rows = (
            budget.read(
                connection,
                "SELECT identity, subject_path, predicate, literal_text FROM claims "
                "WHERE lifecycle='live' AND literal_type='string' AND subject_path>=? "
                "AND subject_path<? AND predicate IN ("
                + ",".join("?" for _ in predicates)
                + ") ORDER BY identity",
                (prefix, high, *predicates),
            )
            if predicates
            else []
        )
    apart: dict[str, set[str]] = defaultdict(set)
    for first, second in relation_rows:
        apart[str(first)].add(str(second))
        apart[str(second)].add(str(first))
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
        for members in _groups_apart(holders, apart):
            groups.append(
                DuplicateSubjectGroup(
                    key=key,
                    subjects=tuple(
                        DuplicateSubject(subject=_ref(path), names=tuple(sorted(holders[path])))
                        for path in members
                    ),
                )
            )
    by_value: dict[tuple[str, str], list[DuplicateValue]] = defaultdict(list)
    for identity, subject, predicate, value in value_rows:
        by_value[(str(predicate), broad_key(str(value)))].append(
            DuplicateValue(
                claim=str(identity).removeprefix("Claim:"),
                subject=_ref(str(subject)),
                value=str(value),
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
        subjects=len(subject_rows),
        aliases=len(alias_rows),
        relations=len(relation_rows),
        values=len(value_rows),
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
