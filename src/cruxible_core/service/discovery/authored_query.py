"""Candidate-vocabulary resolution for authored QueryDefinitions."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from functools import lru_cache
from typing import cast

from cruxible_client.contracts.claim_types import claim_type_digest, parse_claim_type
from cruxible_client.contracts.compact_query import QueryFilterOperator
from cruxible_client.contracts.query.grammar import QueryValueType
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.discovery.query_vocabulary import (
    PredicateInfo,
    QueryVocabulary,
    check_operator,
    check_value,
    query_refusal,
    value_type_of,
)
from cruxible_core.service.read_refusals import ReadRefusalError, nearest

_ENGINE_TYPES: dict[str, QueryValueType] = {
    "string": "string",
    "enum": "string",
    "integer": "integer",
    "decimal": "decimal",
    "boolean": "boolean",
    "timestamp": "timestamp",
    "date": "string",
    "subject": "subject_reference",
}


class AuthoredQueryResolver:
    def __init__(self, vocabulary: QueryVocabulary):
        self.vocabulary = vocabulary

    def require_kind(self, kind: str, *, path: str) -> str:
        return self.vocabulary.require_kind(kind, field_path=path)

    def field(self, kinds: tuple[str, ...], name: str, *, path: str) -> tuple[str, QueryValueType]:
        info = self.vocabulary.resolve_field(kinds, name, field_path=path)
        if isinstance(info, str) or info.value_type not in _ENGINE_TYPES:
            raise query_refusal(
                "cruxible.query.operator_not_applicable",
                "field is not a stored scalar value",
                field_path=path,
                repair="choose a scalar predicate",
            )
        return info.predicate, _ENGINE_TYPES[info.value_type]

    def reverse_source(
        self, bindings: Mapping[str, tuple[str, ...]], name: str, *, path: str
    ) -> tuple[str, str, tuple[str, ...]]:
        found = [
            (binding, info)
            for binding, kinds in bindings.items()
            for kind in kinds
            for info in self.vocabulary.incoming(kind)
            if info.predicate == name or info.predicate.endswith("." + name)
        ]
        unique = {(binding, info.predicate): info for binding, info in found}
        if len(unique) != 1:
            raise query_refusal(
                "cruxible.query.ambiguous_field" if unique else "cruxible.query.unknown_field",
                f"{name!r} must identify one incoming relation from an earlier binding",
                nearest=nearest(
                    name,
                    [
                        info.predicate
                        for kinds in bindings.values()
                        for kind in kinds
                        for info in self.vocabulary.incoming(kind)
                    ],
                ),
                field_path=path,
                repair="state <source_binding.full_predicate explicitly",
            )
        (binding, predicate), info = next(iter(unique.items()))
        return binding, predicate, info.subject_kinds

    def relation(
        self, kinds: tuple[str, ...], name: str, reverse: bool, *, path: str
    ) -> tuple[str, tuple[str, ...]]:
        if reverse:
            found = {
                info.predicate: info
                for kind in kinds
                for info in self.vocabulary.resolve_incoming(kind, name)
            }
            if len(found) != 1:
                # Resolve spelling first to preserve unknown-field suggestions.
                if name not in self.vocabulary.predicates and not found:
                    self.vocabulary.resolve_field(kinds, name, field_path=path)
                raise query_refusal(
                    "cruxible.query.ambiguous_field"
                    if found
                    else "cruxible.query.follow_not_incoming",
                    f"{name!r} must name one incoming relation",
                    nearest=tuple(found),
                    field_path=path,
                    repair="name an incoming relation in full",
                )
            info = next(iter(found.values()))
            return info.predicate, info.subject_kinds
        try:
            outgoing = self.vocabulary.resolve_field(kinds, name, field_path=path)
        except ReadRefusalError:
            incoming = {
                info.predicate
                for kind in kinds
                for info in self.vocabulary.resolve_incoming(kind, name)
            }
            if not incoming:
                raise
            raise query_refusal(
                "cruxible.query.follow_not_relation",
                f"{name!r} is incoming; use a reverse hop",
                field_path=path,
                repair="prefix the hop with <",
            ) from None
        if isinstance(outgoing, str) or outgoing.value_type != "subject":
            raise query_refusal(
                "cruxible.query.follow_not_relation",
                f"{name!r} is not a Subject-valued predicate",
                field_path=path,
                repair="choose a relation predicate",
            )
        return outgoing.predicate, outgoing.object_kinds

    def operator(self, predicate: str, operator: str, *, path: str) -> None:
        check_operator(
            self.vocabulary.predicates[predicate], operator, field_path=path, label=predicate
        )

    def literal(self, predicate: str, operator: str, value: object, *, path: str) -> object:
        info = self.vocabulary.predicates[predicate]
        self.operator(predicate, operator, path=path)
        if (
            info.value_type == "subject"
            and isinstance(value, str)
            and "/" not in value
            and len(info.object_kinds) == 1
        ):
            value = f"{info.object_kinds[0]}/{value}"
        return check_value(
            info, cast(QueryFilterOperator, operator), value, field_path=path, label=predicate
        )


@lru_cache(maxsize=2048)
def _predicate_info(path: str, content: bytes) -> PredicateInfo | None:
    claim_type = parse_claim_type(content, path=path)
    if claim_type.lifecycle.state != "live":
        return None
    value_type, members = value_type_of(claim_type)
    return PredicateInfo(
        predicate=claim_type.predicate,
        claim_type=claim_type,
        claim_type_digest=claim_type_digest(claim_type).tagged,
        value_type=value_type,
        members=members,
        cardinality=claim_type.cardinality,
        subject_kinds=claim_type.allowed_subject_kinds,
        object_kinds=claim_type.allowed_object_subject_kinds,
    )


def candidate_query_vocabulary(
    tree: Mapping[str, bytes], *, paths: Iterable[str] | None = None
) -> QueryVocabulary:
    """Build the candidate vocabulary once per authored definition, never on reads/ticks.

    Candidate trees include unaccepted ClaimTypes, so an accepted-state index cannot
    supply this view. Work grows with definition artifacts, not Claims or Captures.
    """
    predicates = {}
    kinds: set[str] = set()
    for path in tree if paths is None else paths:
        if not path.startswith("claim-types/") or not path.endswith(".json"):
            continue
        content = tree.get(path)
        if content is None:
            continue
        info = _predicate_info(path, content)
        if info is None:
            continue
        predicates[info.predicate] = info
        kinds.update(info.subject_kinds)
        kinds.update(info.object_kinds)
    return QueryVocabulary(predicates=predicates, kinds=tuple(sorted(kinds)))


def candidate_vocabulary_paths(
    instance: PlaybillInstance, base: AcceptedProjectionCoordinate, tree: Mapping[str, bytes]
) -> tuple[str, ...]:
    """Index paths plus candidate edits; never enumerate accepted Claims or Captures."""
    from cruxible_core.derived.derived_state import root_and_edits

    with instance.bind_accepted_projection(base) as projection:
        paths = {row.path for row in projection.typed.envelopes(kind="claim-type")}
    rooted = root_and_edits(tree)
    # The served authoring path always supplies a derived tree; unindexed callers
    # are explicit cold/offline inputs and enumerate their keys once.
    changes = tree if rooted is None else rooted[1]
    paths.update(path for path in changes if path.startswith("claim-types/"))
    return tuple(sorted(paths))
