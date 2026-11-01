"""Acceptance-time typing of the core Claims terminal, over its pinned vocabulary."""

from __future__ import annotations

from collections.abc import Callable
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
    parse_type,
)
from cruxible_client.contracts.subject_kinds import key_predicate


class ClaimsTypingRefused(ValueError):
    code = "cruxible.procedure.claims_type_invalid"


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
            return cast(FieldType, ct.value) if ct.value is not None else parse_type("string")
        if isinstance(t, EnumOfType):
            ct = source("ClaimType", t.claim_type)
            if ct.value is not None:
                return cast(FieldType, ct.value)
            return EnumType(members=tuple(ct.literal_schema["enum"]))
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
                t = t.item
                if path[0].isdigit():
                    path = path[1:]
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
            if value.get("kind") != expected.subject_kind:
                raise ClaimsTypingRefused(f"{label}: Subject kind does not match")
            return
        if isinstance(check_value(value, expected, at=label), FieldInvalid):
            raise ClaimsTypingRefused(f"{label}: literal does not fit ClaimType.value")

    for node in definition.nodes:
        if not isinstance(node, ClaimsNode):
            continue
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
            if ct.value is not None:
                check(value, ct.value, node.for_each, f"subject.key.{name}")
        for mapping in node.claims:
            ct = source("ClaimType", mapping.claim_type.target.name)
            if not isinstance(ct, ClaimType) or ct.value is None:
                raise ClaimsTypingRefused("claims requires a ClaimType declaring value")
            if node.subject.kind not in ct.allowed_subject_kinds:
                raise ClaimsTypingRefused("ClaimType does not permit this SubjectKind")
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
