"""Candidate-vocabulary resolution for authored QueryDefinitions."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from functools import lru_cache
from typing import cast

from cruxible_client.contracts.builtin_claim_types import concrete_subject_kinds
from cruxible_client.contracts.claim_types import claim_type_digest, parse_claim_type
from cruxible_client.contracts.compact_query import QueryFilterOperator
from cruxible_client.contracts.query.grammar import QueryValueType
from cruxible_client.contracts.shapes import subject_value_kinds
from cruxible_client.contracts.subject_kinds import parse_subject_kind
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.indexes.subject_identity import key_normalizer_of, tree_subject_kind_reader
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

    def field(
        self, kinds: tuple[str, ...], name: str, *, path: str, scalar: bool = False
    ) -> tuple[str, QueryValueType]:
        info = self.vocabulary.resolve_field(kinds, name, field_path=path)
        if isinstance(info, str) or (scalar and info.value_type not in _ENGINE_TYPES):
            raise query_refusal(
                "cruxible.query.operator_not_applicable",
                "field is not a stored scalar value",
                field_path=path,
                repair="choose a scalar predicate",
            )
        return info.predicate, _ENGINE_TYPES.get(info.value_type, "string")

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

    def pair(
        self,
        kind: str,
        parts: tuple[tuple[str, tuple[str, ...]], ...],
        fact: str | None,
        *,
        path: str,
    ) -> tuple[tuple[str, ...], str | None]:
        """Resolve a pair hop's parts and fact against the pair kind's key (R8).

        Each part is a Subject-valued key part of ``kind`` whose end kind
        admits the binding's kinds. Without a fact the parts are the whole key;
        with one, exactly one part and a fact that is not a key part.
        """

        def refuse(message: str, repair: str) -> ReadRefusalError:
            return query_refusal(
                "cruxible.query.pair_hop_invalid", message, field_path=path, repair=repair
            )

        keys = self.vocabulary.kind_keys.get(kind)
        if not keys:
            raise refuse(f"{kind!r} is not a keyed Subject kind", "hop to a pair kind")
        predicates: list[str] = []
        for name, kinds in parts:
            info = self.vocabulary.resolve_field((kind,), name, field_path=path)
            if isinstance(info, str) or info.predicate not in keys:
                raise refuse(
                    f"{name!r} is not a key part of {kind}",
                    "name the pair kind's key parts: " + ", ".join(keys),
                )
            if info.value_type != "subject":
                raise refuse(
                    f"key part {info.predicate!r} is a literal, not a Subject end",
                    "bind only Subject-valued key parts",
                )
            if not set(kinds).issubset(info.object_kinds):
                raise refuse(
                    f"key part {info.predicate!r} names a {' or '.join(info.object_kinds)}",
                    "bind the part to a binding of its end kind",
                )
            if info.predicate in predicates:
                raise refuse(f"key part {info.predicate!r} is named twice", "name it once")
            predicates.append(info.predicate)
        if fact is None:
            if set(predicates) != set(keys):
                raise refuse(
                    f"a whole-key hop names every key part of {kind}",
                    "name all of " + ", ".join(keys) + ", or one part with `with <fact>`",
                )
            return tuple(predicates), None
        if len(predicates) != 1:
            raise refuse(
                "a hop `with <fact>` binds exactly one key part",
                "bind one end, or every part without `with`",
            )
        info = self.vocabulary.resolve_field((kind,), fact, field_path=path)
        if isinstance(info, str) or info.predicate in keys:
            raise refuse(f"{fact!r} is not a fact of {kind}", "name a non-key predicate")
        return tuple(predicates), info.predicate

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


@lru_cache(maxsize=4096)
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
        object_kinds=subject_value_kinds(claim_type.value),
    )


@lru_cache(maxsize=4096)
def _declared_kind(path: str, content: bytes) -> tuple[str, tuple[str, ...]] | None:
    value = parse_subject_kind(content, path=path)
    return (value.identity.name, value.keys) if value.lifecycle.state == "live" else None


def candidate_query_vocabulary(
    tree: Mapping[str, bytes], *, paths: Iterable[str] | None = None
) -> QueryVocabulary:
    """Build the candidate vocabulary once per authored definition, never on reads/ticks.

    Candidate trees include unaccepted ClaimTypes, so an accepted-state index cannot
    supply this view. Work grows with definition artifacts, not Claims or Captures.
    """
    predicates = {}
    kinds: set[str] = set()
    kind_keys: dict[str, tuple[str, ...]] = {}
    declared_kinds = tree_subject_kind_reader(tree)
    for path in tree if paths is None else paths:
        if path.startswith("subject-kinds/") and path.endswith(".json"):
            content = tree.get(path)
            declared = None if content is None else _declared_kind(path, content)
            if declared is not None:
                kinds.add(declared[0])
                if declared[1]:
                    kind_keys[declared[0]] = declared[1]
            continue
        if not path.startswith("claim-types/") or not path.endswith(".json"):
            continue
        content = tree.get(path)
        if content is None:
            continue
        info = _predicate_info(path, content)
        if info is None:
            continue
        rule = key_normalizer_of(info.claim_type, declared_kinds)
        if rule is not None:
            # A key predicate's literals take its SubjectKind's normalizer (R12).
            info = replace(info, key_normalizer=rule)
        predicates[info.predicate] = info
        kinds.update(concrete_subject_kinds(info.subject_kinds))
        kinds.update(info.object_kinds)
    return QueryVocabulary(predicates=predicates, kinds=tuple(sorted(kinds)), kind_keys=kind_keys)


def candidate_vocabulary_paths(
    instance: PlaybillInstance, base: AcceptedProjectionCoordinate, tree: Mapping[str, bytes]
) -> tuple[str, ...]:
    """Index paths plus candidate edits; never enumerate accepted Claims or Captures."""
    from cruxible_core.derived.derived_state import root_and_edits

    with instance.bind_accepted_projection(base) as projection:
        paths = {
            row.path
            for kind in ("claim-type", "subject-kind")
            for row in projection.typed.envelopes(kind=kind)
        }
    rooted = root_and_edits(tree)
    # The served authoring path always supplies a derived tree; unindexed callers
    # are explicit cold/offline inputs and enumerate their keys once.
    changes = tree if rooted is None else rooted[1]
    paths.update(path for path in changes if path.startswith(("claim-types/", "subject-kinds/")))
    return tuple(sorted(paths))
