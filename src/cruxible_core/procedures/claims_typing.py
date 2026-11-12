"""Acceptance-time typing of the core Claims terminal, over its pinned vocabulary."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, cast

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.claim_types import ClaimType
from cruxible_client.contracts.procedures.claims import ClaimsExpectByEnd, ClaimsNode
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
    DateTimeType,
    DateType,
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
    render_type,
)
from cruxible_client.contracts.subject_kinds import key_predicate


class ClaimsTypingRefused(ValueError):
    """One Claims template refused at acceptance.

    ``node_id`` names the claims step and ``element`` the template inside it
    (``claims[0].effective_from``, ``subject.key.cve_id``) when the refusal is
    about one, so an authoring surface can address the authored step.
    """

    code = "cruxible.procedure.claims_type_invalid"
    node_id: str | None = None
    element: str | None = None

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        node_id: str | None = None,
        element: str | None = None,
    ) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.node_id = node_id
        self.element = element


_END_FORM = (
    "a pair's end {part!r} is named by a nested Subject template "
    "{{kind: {kinds}, key: {{...}}, create: never | if_absent}}"
)


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
                return None if rule is None else rule.field_type()
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
            pin = definition.slots[producer.slot].capability
            fields = contract(source("Capability", f"{pin.name}/v{pin.version}").out).fields
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

    def check(
        value: object, expected: FieldType, each: object, label: str, slot: str, element: str
    ) -> None:
        """Type one template against its slot: ``slot`` names it as the author reads it."""

        def refused(message: str) -> ClaimsTypingRefused:
            return ClaimsTypingRefused(f"{label}: {message}", element=element)

        if isinstance(value, str) and value.startswith(("$item", "$steps.", "$input", "$params.")):
            actual = field_type(value, each)
            if actual is None:
                raise refused("value type is unknown; declare an explicit typed shape cast")
            if not assignable(normalized(actual), normalized(expected)):
                hint = (
                    "; read the date into a datetime field with "
                    "parse: date, project: utc_start_of_day"
                    if isinstance(actual, DateType) and isinstance(expected, DateTimeType)
                    else ""
                )
                raise refused(
                    f"{value} is {render_type(actual)}, which is not assignable to "
                    f"{slot} ({render_type(expected)}){hint}"
                )
            return
        if isinstance(expected, SubjectType) and isinstance(value, dict):
            subject_value(value, expected, each, label, element, refused)
            return
        if claim_has_line_break(value):
            raise refused("multiline_string; cite a Document section")
        if isinstance(check_value(value, expected, at=label), FieldInvalid):
            raise refused(f"literal does not fit {slot} ({render_type(expected)})")

    def subject_value(
        value: dict[str, object],
        expected: SubjectType,
        each: object,
        label: str,
        element: str,
        refused: Callable[[str], ClaimsTypingRefused],
    ) -> None:
        """A Subject named by kind and key, as ``subject`` names one (V1).

        Only ``kind``, ``key`` and ``create`` are spelled; the key names every
        key part of the kind exactly once, each typed against its key
        ClaimType; ``create`` is ``never`` or ``if_absent``. A minted kind has
        no key, so a value never names or creates one of it by key.
        """

        unknown = sorted(set(value) - {"kind", "key", "create"})
        if unknown:
            raise refused(
                "a Subject value spells only kind, key and create; drop " + ", ".join(unknown)
            )
        if value.get("kind") not in expected.subject_kinds:
            raise refused("Subject kind does not match")
        if value.get("create", "never") not in ("never", "if_absent"):
            raise refused("create must be never or if_absent")
        kind = source("SubjectKind", str(value["kind"]))
        if kind.minted:
            raise refused(
                f"{kind.subject_kind} is minted: core names its Subjects, so a value "
                "names one by id, never by key, and cannot create one"
            )
        key = value.get("key")
        if not isinstance(key, dict):
            raise refused(f"key must map each key part of {kind.subject_kind}")
        parts = [key_predicate(kind, str(name)) for name in key]
        if None in parts or len(set(parts)) != len(parts) or set(parts) != set(kind.keys):
            raise refused("key must name every SubjectKind key part exactly once")
        for name, part in key.items():
            predicate = key_predicate(kind, str(name))
            assert predicate is not None
            part_type = source("ClaimType", predicate).value
            if isinstance(part_type, SubjectType) and not isinstance(part, dict):
                raise refused(_END_FORM.format(part=name, kinds=", ".join(part_type.subject_kinds)))
            check(
                part,
                part_type,
                each,
                f"{label} key {name}",
                f"key ClaimType {predicate}",
                f"{element}.key.{name}",
            )

    for node in definition.nodes:
        if not isinstance(node, ClaimsNode):
            continue
        try:
            _validate_claims_node(node, aliases, source, check)
        except ClaimsTypingRefused as exc:
            if exc.node_id is None:
                exc.node_id = node.node_id
            raise


def _validate_claims_node(
    node: ClaimsNode,
    aliases: Mapping[str | None, object],
    source: Callable[[str, str], Any],
    check: Callable[[object, FieldType, object, str, str, str], None],
) -> None:
    """One claims step's subject key and Claim templates, against their slots."""

    if node.expect is not None:
        absence_enumeration(node, aliases)
    kind = source("SubjectKind", node.subject.kind)
    parts = [key_predicate(kind, name) for name in node.subject.key]
    if None in parts or set(parts) != set(kind.keys) or len(parts) != len(kind.keys):
        raise ClaimsTypingRefused("subject.key must name every SubjectKind key part exactly once")
    for name, value in node.subject.key.items():
        predicate = key_predicate(kind, name)
        assert predicate is not None
        ct = source("ClaimType", predicate)
        element = f"subject.key.{name}"
        if isinstance(ct.value, SubjectType) and not isinstance(value, dict):
            # A pair row names each end as a nested Subject template (V1), never by text.
            raise ClaimsTypingRefused(
                f"{element}: "
                + _END_FORM.format(part=name, kinds=", ".join(ct.value.subject_kinds)),
                element=element,
            )
        check(value, ct.value, node.for_each, element, f"key ClaimType {predicate}", element)
    if isinstance(node.expect, ClaimsExpectByEnd):
        by = key_predicate(kind, node.expect.by)
        by_type = None if by is None else source("ClaimType", by).value
        if not isinstance(by_type, SubjectType):
            raise ClaimsTypingRefused(
                f"claims_expect_by_invalid: expect.by {node.expect.by!r} must name a "
                f"Subject-valued key part (an end) of {node.subject.kind}; its key parts are "
                + ", ".join(kind.keys),
                code="cruxible.procedure.claims_expect_by_invalid",
                element="expect.by",
            )
    for position, mapping in enumerate(node.claims):
        at = f"claims[{position}]"
        ct = source("ClaimType", mapping.claim_type.target.name)
        if not isinstance(ct, ClaimType):
            raise ClaimsTypingRefused("claims requires a ClaimType declaring value")
        if node.subject.kind not in ct.allowed_subject_kinds:
            raise ClaimsTypingRefused("ClaimType does not permit this SubjectKind")
        if mapping.when_absent == "inverse" and ct.inverse is None:
            raise ClaimsTypingRefused(
                "claims_inverse_missing: when_absent inverse requires a declared ClaimType inverse"
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
        if mapping.replaces is not None and not set(mapping.replaces).issubset(ct.permitted_roles):
            raise ClaimsTypingRefused(
                "claims_replaces_forbidden: replaces names a role the ClaimType does not permit"
            )
        ct.precedence.replacements(role, mapping.replaces)
        check(
            mapping.value,
            ct.value,
            node.for_each,
            f"{at} {ct.predicate}",
            f"the value of ClaimType {ct.predicate}",
            f"{at}.value",
        )
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
                    f"{at} {ct.predicate} {name}",
                    f"the Claim's {name}",
                    f"{at}.{name}",
                )


def tree_claims_lookup(tree: Any) -> Callable[[ArtifactIdentity], Any]:
    """Resolve each pinned vocabulary dependency by path, never enumerate state."""
    from functools import cache

    from cruxible_client.contracts.capabilities import (
        capability_path_for_identity,
        parse_capability,
    )
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
        "Capability": (capability_path_for_identity, parse_capability),
    }

    @cache
    def lookup(identity: ArtifactIdentity) -> Any:
        path_fn, parse = paths[identity.kind]
        path = path_fn(identity.name)
        content = tree.get(path)
        return None if content is None else parse(content, path=path)

    return lookup
