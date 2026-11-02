"""Acceptance-time typing of the core Claims terminal, over its pinned vocabulary."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.claim_types import ClaimType
from cruxible_client.contracts.procedures.claims import ClaimsNode
from cruxible_client.contracts.procedures.models import (
    CallNode,
    ExtractNode,
    ProcedureDefinition,
    ProjectNode,
    ReadNode,
    TransformNode,
)
from cruxible_client.contracts.procedures.nodes import (
    TransformAggregateSpec,
    TransformDedupeSpec,
    TransformDeriveSpec,
    TransformFilterSpec,
    TransformShapeSpec,
)
from cruxible_client.contracts.shapes import (
    BoolType,
    EnumType,
    FieldInvalid,
    FieldResultType,
    FieldType,
    IntType,
    KeyPartType,
    ListType,
    NestedType,
    RecordType,
    ResultType,
    StringType,
    SubjectType,
    assignable,
    check_value,
    claim_has_line_break,
    parse_type,
)
from cruxible_client.contracts.subject_kinds import key_predicate


class ClaimsTypingRefused(ValueError):
    code = "cruxible.procedure.claims_type_invalid"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


def absence_enumeration(node: ClaimsNode, aliases: Mapping[str | None, object]) -> ExtractNode:
    """The enumeration absence judges: the extract at the root of ``for_each``'s chain.

    It is never authored. ``for_each`` must reach the extract's output directly
    or through filter/dedupe only, so presence templates see the records the
    source lists. Walk this Procedure's referenced chain only; no accepted
    population reads. Unknown references, subpaths and cycles fail closed.
    """
    reference = node.for_each
    seen: set[str] = set()
    while isinstance(reference, str):
        parts = reference.split(".")
        if len(parts) < 2 or parts[0] != "$steps" or parts[1] in seen:
            break
        seen.add(parts[1])
        producer = aliases.get(parts[1])
        if isinstance(producer, CallNode):
            raise ClaimsTypingRefused(
                "claims_expect_provider_enumeration_unsupported: expect over a provider "
                "call's records is not supported yet (roadmap item "
                "provider-declared-enumeration); extract the source's Capture instead",
                code="cruxible.procedure.claims_expect_provider_enumeration_unsupported",
            )
        if len(parts) > 3 or (len(parts) == 3 and parts[2] != "records"):
            break
        if isinstance(producer, ExtractNode):
            return producer
        if not isinstance(producer, TransformNode) or not isinstance(
            producer.spec, TransformFilterSpec | TransformDedupeSpec
        ):
            break
        reference = producer.spec.input
    raise ClaimsTypingRefused(
        "claims_enumeration_invalid: expect requires for_each to use an extract's "
        "records directly or through filter/dedupe only",
        code="cruxible.procedure.claims_enumeration_invalid",
    )


def validate_claims_templates(
    definition: ProcedureDefinition, lookup: Callable[[ArtifactIdentity], Any]
) -> None:
    """Only this Procedure's nodes and referenced definitions; no population reads."""
    if not any(isinstance(node, ClaimsNode) for node in definition.nodes):
        return
    aliases = {getattr(node, "as_", None): node for node in definition.nodes}

    def source(kind: str, name: str) -> Any:
        found = lookup(ArtifactIdentity(kind=kind, name=name))
        if found is None or getattr(getattr(found, "lifecycle", None), "state", "live") != "live":
            raise ClaimsTypingRefused(f"{kind}:{name} must be live")
        return found

    def contract(ref: Any) -> Any:
        return source("Contract", f"{ref.name}/v{ref.version}")

    def normalized(t: FieldType) -> FieldType:
        from cruxible_client.contracts.shapes import EnumOfType

        if isinstance(t, KeyPartType):
            kind = source("SubjectKind", t.subject_kind)
            predicate = key_predicate(kind, t.part)
            if predicate is None:
                raise ClaimsTypingRefused("unknown Subject key part")
            ct = source("ClaimType", predicate)
            return cast(FieldType, ct.value)
        if isinstance(t, EnumOfType):
            ct = source("ClaimType", t.claim_type)
            return cast(FieldType, ct.value)
        return t

    def field_type(
        value: object, each: object, trail: frozenset[str] = frozenset()
    ) -> FieldType | None:
        if not isinstance(value, str) or not value.startswith(
            ("$item", "$steps.", "$input", "$params.")
        ):
            return None
        if value.startswith("$item"):
            if not isinstance(each, str) or not each.startswith("$steps."):
                return None
            alias = each.split(".")[1]
            path = value.split(".")[1:]
        elif value.startswith("$steps."):
            _, alias, *path = value.split(".")
        else:
            root, *path = value.split(".")
            ref = definition.contract_in if root == "$input" else definition.parameter_contract
            if ref is None:
                return None
            fields = source("Contract", ref.target.name).fields
            return descend(fields[path[0]].type, path[1:]) if path and path[0] in fields else None
        if alias in trail:
            return None
        producer = aliases.get(alias)
        if isinstance(producer, ExtractNode):
            if path and path[0] == "fields":
                fields = contract(producer.row).fields
                return (
                    descend(fields[path[1]].type, path[2:])
                    if len(path) > 1 and path[1] in fields
                    else None
                )
            if path and path[0] == "key" and len(path) == 2:
                rule = producer.key.get(path[1])
                return None if rule is None or rule.type is None else parse_type(rule.type)
            if path and path[0] == "header" and len(path) == 2:
                rule = producer.header.get(path[1])
                if rule is None:
                    return None
                return parse_type(
                    "date" if rule.project == "utc_date" else rule.type or rule.parse or "string"
                )
        if isinstance(producer, TransformNode):
            spec = producer.spec
            name = path[-1] if path else ""
            if isinstance(spec, TransformShapeSpec):
                if path and path[0] == "key":
                    return field_type(value, spec.input, trail | {alias})
                shaped = spec.fields.get(name)
                if shaped is not None:
                    if shaped.as_ is not None:
                        return parse_type(shaped.as_)
                    if shaped.from_ is None:
                        return literal_type(shaped.value)
                    return field_type(shaped.from_, spec.input, trail | {alias})
            if isinstance(spec, TransformFilterSpec | TransformDedupeSpec):
                return (
                    field_type(value, spec.input, trail | {alias})
                    if value.startswith("$item")
                    else None
                )
            if isinstance(spec, TransformAggregateSpec):
                return IntType() if spec.op == "count" else None
            if isinstance(spec, TransformDeriveSpec):
                choices = [case.value for case in spec.cases] + [spec.else_]
                if all(isinstance(v, str) for v in choices):
                    return EnumType(
                        members=tuple(sorted({v for v in choices if isinstance(v, str)}))
                    )
        if isinstance(producer, ProjectNode):
            fields = producer.literal if producer.literal is not None else producer.fields
            if value.startswith("$item") and isinstance(each, str):
                for part in each.split(".")[2:]:
                    fields = fields.get(part) if isinstance(fields, dict) else None
                if isinstance(fields, list) and fields:
                    inferred = []
                    for row in fields:
                        selected = row
                        for part in path:
                            selected = selected.get(part) if isinstance(selected, dict) else None
                        inferred.append(
                            field_type(selected, each, trail | {alias}) or literal_type(selected)
                        )
                    return inferred[0] if all(t == inferred[0] for t in inferred) else None
            if isinstance(fields, dict) and path and path[0] in fields:
                nested = fields[path[0]]
                return field_type(nested, each, trail | {alias}) or literal_type(nested)
        if isinstance(producer, CallNode):
            fields = contract(definition.slots[producer.slot].out).fields
            if value.startswith("$item") and isinstance(each, str):
                collection = each.split(".")[2:]
                if not collection or collection[0] not in fields:
                    return None
                item = descend(fields[collection[0]].type, collection[1:])
                if isinstance(item, ListType):
                    item = item.item
                if isinstance(item, RecordType | ResultType):
                    row_fields = contract(item.contract).fields
                    name = (
                        path[1]
                        if path and path[0] == "fields" and len(path) > 1
                        else path[0]
                        if path
                        else ""
                    )
                    return row_fields[name].type if name in row_fields else None
                return None if item is None else descend(item, path)
            return descend(fields[path[0]].type, path[1:]) if path and path[0] in fields else None
        if isinstance(producer, ReadNode) and producer.claim_type is not None:
            ct = source("ClaimType", producer.claim_type.target.name)
            return ct.value if path == ["value"] else None
        if isinstance(producer, ReadNode) and producer.query is not None:
            query = source("QueryDefinition", producer.query.target.name)
            fields = contract(query.row_contract).fields
            if path and path[0] == "rows":
                path = path[1:]
            return descend(fields[path[0]].type, path[1:]) if path and path[0] in fields else None
        return None

    def descend(t: FieldType, path: list[str]) -> FieldType | None:
        while path:
            if isinstance(t, FieldResultType):
                t = t.value
            elif isinstance(t, ListType):
                member, *rest = path
                if member == "[*]":
                    item = descend(t.item, rest)
                    return None if item is None else ListType(item=item, max_items=t.max_items)
                if not member.isdigit():
                    raise ClaimsTypingRefused(
                        "a list reference requires [*] or an explicit position"
                    )
                t, path = t.item, rest
            elif isinstance(t, NestedType):
                fields = contract(t.contract).fields
                if path[0] not in fields:
                    return None
                t, path = fields[path[0]].type, path[1:]
            else:
                return None
        return t

    def literal_type(value: object) -> FieldType | None:
        if isinstance(value, str) and not value.startswith("$"):
            return EnumType(members=(value,)) if value else StringType()
        if isinstance(value, bool):
            return BoolType()
        if isinstance(value, int):
            return IntType(min=value, max=value)
        return None

    def check(value: object, expected: FieldType, each: object, label: str) -> None:
        if isinstance(value, str) and value.startswith(("$item", "$steps.", "$input", "$params.")):
            actual = field_type(value, each)
            if actual is None:
                raise ClaimsTypingRefused(
                    f"{label}: value type is unknown; declare an explicit typed shape cast"
                )
            if not assignable(normalized(actual), normalized(expected)):
                raise ClaimsTypingRefused(f"{label}: value is not assignable to ClaimType.value")
            return
        if isinstance(expected, SubjectType) and isinstance(value, dict):
            if value.get("kind") not in expected.subject_kinds:
                raise ClaimsTypingRefused(f"{label}: Subject kind does not match")
            return
        if claim_has_line_break(value):
            raise ClaimsTypingRefused(f"{label}: multiline_string; cite a Document section")
        if isinstance(check_value(value, expected, at=label), FieldInvalid):
            raise ClaimsTypingRefused(f"{label}: literal does not fit ClaimType.value")

    for node in definition.nodes:
        if not isinstance(node, ClaimsNode):
            continue
        if node.expect is not None:
            absence_enumeration(node, aliases)
        kind = source("SubjectKind", node.subject.kind)
        parts = [key_predicate(kind, name) for name in node.subject.key]
        if None in parts or set(parts) != set(kind.keys) or len(parts) != len(kind.keys):
            raise ClaimsTypingRefused(
                "subject.key must name every SubjectKind key part exactly once"
            )
        for name, value in node.subject.key.items():
            predicate = key_predicate(kind, name)
            assert predicate is not None
            ct = source("ClaimType", predicate)
            check(value, ct.value, node.for_each, f"subject.key.{name}")
        for mapping in node.claims:
            ct = source("ClaimType", mapping.claim_type.target.name)
            if not isinstance(ct, ClaimType):
                raise ClaimsTypingRefused("claims requires a ClaimType declaring value")
            if node.subject.kind not in ct.allowed_subject_kinds:
                raise ClaimsTypingRefused("ClaimType does not permit this SubjectKind")
            if mapping.when_absent == "inverse" and ct.inverse is None:
                raise ClaimsTypingRefused(
                    "claims_inverse_missing: when_absent inverse requires "
                    "a declared ClaimType inverse"
                )
            if mapping.when_absent == "inverse":
                from cruxible_core.procedures.execution import _mentions_item_reference

                if _mentions_item_reference(mapping.value):
                    raise ClaimsTypingRefused(
                        "claims_inverse_item_value: inverse units require "
                        "an item-independent value template",
                        code="cruxible.procedure.claims_inverse_item_value",
                    )
            role = mapping.role or ct.default_role
            if role not in ct.permitted_roles:
                raise ClaimsTypingRefused("claims_role_forbidden: state a permitted role")
            if mapping.replaces is not None and not set(mapping.replaces).issubset(
                ct.permitted_roles
            ):
                raise ClaimsTypingRefused(
                    "claims_replaces_forbidden: replaces names a role the ClaimType does not permit"
                )
            ct.precedence.replacements(role, mapping.replaces)
            check(mapping.value, ct.value, node.for_each, mapping.claim_type.target.name)
            for name, template in (
                ("qualifier", mapping.qualifier),
                ("effective_from", mapping.effective_from),
                ("effective_until", mapping.effective_until),
            ):
                if template is not None:
                    check(
                        template,
                        parse_type("string" if name == "qualifier" else "datetime"),
                        node.for_each,
                        name,
                    )


def tree_claims_lookup(tree: Any) -> Callable[[ArtifactIdentity], Any]:
    """Resolve each pinned vocabulary dependency by path, never enumerate state."""
    from functools import cache

    from cruxible_client.contracts.claim_types import claim_type_path, parse_claim_type
    from cruxible_client.contracts.query.definitions import (
        parse_query_definition,
        query_definition_path,
    )
    from cruxible_client.contracts.shapes import contract_path_for_identity, parse_contract
    from cruxible_client.contracts.subject_kinds import parse_subject_kind, subject_kind_path

    paths = {
        "ClaimType": (claim_type_path, parse_claim_type),
        "SubjectKind": (subject_kind_path, parse_subject_kind),
        "QueryDefinition": (query_definition_path, parse_query_definition),
        "Contract": (contract_path_for_identity, parse_contract),
    }

    @cache
    def lookup(identity: ArtifactIdentity) -> Any:
        path_fn, parse = paths[identity.kind]
        path = path_fn(identity.name)
        content = tree.get(path)
        return None if content is None else parse(content, path=path)

    return lookup
