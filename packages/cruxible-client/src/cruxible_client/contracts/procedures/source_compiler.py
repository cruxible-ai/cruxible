"""Compile a closed Python AST into graph nodes without executing authored code.

There is deliberately no eval, exec, import resolution, callable invocation, or
fallback interpreter. Host discovery supplies explicit retained schema bindings.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Literal, NoReturn, cast

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.procedures.contract_schema import (
    ContractSchema,
    PropertySchema,
    PropertyType,
)
from cruxible_client.contracts.procedures.models import (
    PredicateOperandV1,
    ProcedureBudgetV3,
    ProcedureDefinitionV6,
    ProcedureHardCapsV3,
)
from cruxible_client.contracts.procedures.source_program import (
    ProcedureSourceV1,
    SourceClaimType,
    SourceContract,
    SourceDiagnostic,
    SourceMapEntry,
    SourceProviderBinding,
    SourceQueryBinding,
    SourceSpan,
)
from cruxible_client.contracts.procedures.source_views import query_view_schema, read_object_schema
from cruxible_client.contracts.records import RecordConstructor, record_field_names

ValueType = ContractSchema | PropertySchema


@dataclass(frozen=True)
class Value:
    wire: Any
    type: ValueType
    literal: bool = False
    phase: Literal["input", "state", "runtime"] = "runtime"


@dataclass(frozen=True)
class Observation(Value):
    """A Source output whose evidence provenance is owned by the runtime."""


@dataclass(frozen=True)
class Subject:
    kind: str
    identity: Value


@dataclass(frozen=True)
class ClaimSelection:
    subject: Subject
    type: SourceClaimType


@dataclass(frozen=True)
class ClaimValue(Value):
    """A selected Claim, preserving its complete exact admitted basis."""


@dataclass(frozen=True)
class ClaimCandidate:
    wire: dict[str, Any]


@dataclass(frozen=True)
class Constructor:
    schema: ContractSchema
    query: SourceQueryBinding | None = None


@dataclass(frozen=True)
class Namespace:
    name: str


Expression = (
    Subject
    | ClaimCandidate
    | ClaimSelection
    | SourceClaimType
    | Value
    | Constructor
    | Namespace
    | SourceProviderBinding
    | SourceQueryBinding
)
Edge = tuple[dict[str, Any], str]


class SourceCompileError(ValueError):
    def __init__(self, diagnostic: SourceDiagnostic):
        self.diagnostic = diagnostic
        super().__init__(f"{diagnostic.span.filename}:{diagnostic.span.line}: {diagnostic.message}")


@dataclass(frozen=True)
class CompiledSource:
    definition: ProcedureDefinitionV6
    contracts: tuple[SourceContract, ...]
    source_map: tuple[SourceMapEntry, ...]


def _json_type(schema: ValueType) -> dict[str, Any]:
    if isinstance(schema, ContractSchema):
        return {
            "type": "object",
            "properties": {k: _json_type(v) for k, v in schema.fields.items()},
            "required": [k for k, v in schema.fields.items() if not v.optional],
            "additionalProperties": schema.allow_extra,
        }
    if schema.json_schema is not None:
        return schema.json_schema
    if schema.type == "json":
        return {}
    if schema.type == "list":
        return {
            "type": "array",
            "items": _json_type(ContractSchema(fields=schema.item_fields or {})),
        }
    result: dict[str, Any] = {
        "type": {"int": "integer", "bool": "boolean", "datetime": "string", "date": "string"}.get(
            schema.type, schema.type
        )
    }
    if schema.enum is not None:
        result["enum"] = schema.enum
    return result


def _field_type(schema: ValueType) -> PropertySchema:
    return (
        schema
        if isinstance(schema, PropertySchema)
        else PropertySchema(type="json", json_schema=_json_type(schema))
    )


def _same_base_type(actual: ValueType, expected: ValueType) -> bool:
    a, b = _json_type(actual), _json_type(expected)
    # Open canonical JSON deliberately makes no stronger field promises.
    return not b or a.get("type") == b.get("type")


def _assignable(actual: ValueType, expected: ValueType) -> bool:
    """A source field may widen its declared range, never silently narrow it."""

    def admits(a: dict[str, Any], b: dict[str, Any]) -> bool:
        if not b:
            return True
        if a.get("type") != b.get("type"):
            return False
        if "enum" in b:
            if "enum" not in a or not {canonical_bytes(v) for v in a["enum"]}.issubset(
                {canonical_bytes(v) for v in b["enum"]}
            ):
                return False
        if b.get("type") == "object":
            ap, bp = a.get("properties", {}), b.get("properties", {})
            if not set(b.get("required", [])).issubset(a.get("required", [])):
                return False
            if not b.get("additionalProperties", True) and (
                a.get("additionalProperties", True) or set(ap) - set(bp)
            ):
                return False
            if any(k in ap and not admits(ap[k], field) for k, field in bp.items()):
                return False
        if b.get("type") == "array" and not admits(a.get("items", {}), b.get("items", {})):
            return False
        # Constraint implication is intentionally bounded. Different patterns
        # or ranges need an explicit operation with its own checked contract.
        ignored = {
            "type",
            "properties",
            "required",
            "additionalProperties",
            "items",
            "enum",
            "description",
            "title",
        }
        return all(key in ignored or a.get(key) == value for key, value in b.items())

    return admits(_json_type(actual), _json_type(expected))


class _Compiler:
    def __init__(self, program: ProcedureSourceV1, input: SourceContract, output: SourceContract):
        self.program = program
        self.input, self.output = input, output
        self.nodes: list[dict[str, Any]] = []
        self.maps: list[SourceMapEntry] = []
        self.contracts: dict[str, SourceContract] = {}
        self.tail: list[Edge] = []
        self.environment: dict[str, Expression] = {}
        self.counter = 0
        self.returns: list[str] = []
        self.assignment_alias: str | None = None
        self.used_contracts: set[str] = set()
        self.used_bindings: set[str] = set()
        self.used_types: set[str] = set()
        self.used_kinds: set[str] = set()
        self.contract(input)
        self.contract(output)

    def span(self, node: ast.AST) -> SourceSpan:
        return SourceSpan(
            filename=self.program.filename,
            line=self.program.first_line + getattr(node, "lineno", 1) - 1,
            column=getattr(node, "col_offset", 0),
            end_line=self.program.first_line + (getattr(node, "end_lineno", 1) or 1) - 1,
            end_column=getattr(node, "end_col_offset", 0) or 0,
        )

    def fail(
        self,
        node: ast.AST,
        message: str,
        code: str = "unsupported_construct",
        hint: str | None = None,
    ) -> NoReturn:
        raise SourceCompileError(
            SourceDiagnostic(
                code="playbill.source." + code, message=message, span=self.span(node), hint=hint
            )
        )

    def contract(self, source: SourceContract) -> dict[str, Any]:
        from cruxible_client.contracts.procedures.artifacts import (
            ProcedureOwnedContractV1,
            procedure_owned_contract_digest,
        )

        RecordConstructor(source.schema_)
        previous = self.contracts.get(source.name)
        if previous is not None and previous != source:
            raise ValueError(f"conflicting carried Contract {source.name!r}")
        self.contracts[source.name] = source
        owned = ProcedureOwnedContractV1(
            identity=ArtifactIdentity(kind="Contract", name=source.name), schema=source.schema_
        )
        return ArtifactPin(
            role="contract-out",
            target=owned.identity,
            artifact_digest=procedure_owned_contract_digest(owned).tagged,
        ).model_dump(mode="json")

    def anonymous(self, schema: ContractSchema) -> dict[str, Any]:
        name = "source." + sha256(canonical_bytes(schema.model_dump(mode="json"))).hexdigest()[:24]
        return self.contract(SourceContract(name=name, schema=schema))

    def append(self, kind: str, at: ast.AST, **fields: Any) -> dict[str, Any]:
        self.counter += 1
        node_id = f"{kind}_{self.counter}"
        node = {"kind": kind, "node_id": node_id, **fields}
        if "as" in node:
            node["as"] = (
                self.assignment_alias
                if kind in {"source", "call", "state_tap", "state_claim"} and self.assignment_alias
                else node_id
            )
        if "as" in node and any(previous.get("as") == node["as"] for previous in self.nodes):
            node["as"] = node_id
        for previous, label in self.tail:
            previous[label] = node_id
        self.nodes.append(node)
        self.maps.append(SourceMapEntry(node_id=node_id, span=self.span(at)))
        self.tail = (
            []
            if kind in {"return", "emit_capture", "propose_change_set", "halt"}
            else [(node, "next")]
        )
        return node

    def value(self, node: ast.AST, *, expected: ValueType | None = None) -> Value:
        expression = self.expr(node, expected=expected)
        if not isinstance(expression, Value):
            self.fail(node, "Expected a value, not a binding or constructor", "value_required")
        return expression

    def wire(self, value: Value, at: ast.AST) -> Any:
        if value.literal and isinstance(value.wire, str) and value.wire.startswith("$"):
            schema = ContractSchema(fields={"value": _field_type(value.type)})
            node = self.append(
                "constant",
                at,
                fields={"value": value.wire},
                contract_out=self.anonymous(schema),
                **{"as": True},
            )
            return f"$steps.{node['as']}.value"
        return value.wire

    def record(self, constructor: Constructor, node: ast.Call) -> Value:
        if node.args or any(k.arg is None for k in node.keywords):
            self.fail(node, "Records require explicit named fields", "record_arguments")
        aliases = record_field_names(constructor.schema)
        supplied: dict[str, Value] = {}
        for keyword in node.keywords:
            wire_name = aliases.get(keyword.arg or "", keyword.arg or "")
            if wire_name in supplied or wire_name not in constructor.schema.fields:
                self.fail(
                    keyword, f"Unknown or duplicate record field {keyword.arg!r}", "record_field"
                )
            field = constructor.schema.fields[wire_name]
            value = self.value(keyword.value, expected=field)
            if not _same_base_type(value.type, field) or (
                not value.literal and not _assignable(value.type, field)
            ):
                self.fail(
                    keyword.value,
                    f"Field {wire_name!r} does not match its declared type",
                    "contract_mismatch",
                )
            if value.literal:
                try:
                    RecordConstructor(ContractSchema(fields={wire_name: field}))(
                        **{wire_name: value.wire}
                    )
                except ValueError as exc:
                    self.fail(keyword.value, str(exc), "contract_value_invalid")
            supplied[wire_name] = value
        if constructor.query is not None:
            from cruxible_client.contracts.query.values import coerce_query_value

            for declaration in constructor.query.definition.parameters:
                parameter = supplied.get(declaration.name)
                if parameter is None:
                    continue
                allowed = {
                    "boolean": {"boolean"},
                    "integer": {"integer"},
                    "decimal": {"integer", "string"},
                }.get(declaration.value_type, {"string"})
                valid = (
                    coerce_query_value(parameter.wire, declaration.value_type).ok
                    if parameter.literal
                    else _json_type(parameter.type).get("type") in allowed
                )
                if not valid:
                    self.fail(
                        node,
                        f"Query parameter {declaration.name!r} requires {declaration.value_type}",
                        "query_parameter_type",
                    )
        for name, field in constructor.schema.fields.items():
            if name in supplied:
                continue
            if field.default is not None:
                supplied[name] = Value(field.default, field, literal=True, phase="input")
            elif not field.optional:
                self.fail(node, f"Missing required field {name!r}", "record_field")
        phase: Literal["input", "state", "runtime"] = (
            "runtime"
            if any(v.phase == "runtime" for v in supplied.values())
            else "state"
            if any(v.phase == "state" for v in supplied.values())
            else "input"
        )
        return Value(
            {k: self.wire(v, node) for k, v in supplied.items()}, constructor.schema, phase=phase
        )

    def expr(self, node: ast.AST, *, expected: ValueType | None = None) -> Expression:
        selected: Expression
        if isinstance(node, ast.Constant):
            item = node.value
            if item is None:
                return Value(
                    None, PropertySchema(type="json", json_schema={"type": "null"}), True, "input"
                )
            if type(item) not in {str, int, bool}:
                self.fail(
                    node, "Only canonical string, integer, boolean and null literals are supported"
                )
            kind = {str: "string", int: "int", bool: "bool"}[type(item)]
            return Value(
                item, PropertySchema(type=cast(PropertyType, kind), enum=[item]), True, "input"
            )
        if isinstance(node, ast.Name):
            if node.id in self.environment:
                return self.environment[node.id]
            if node.id in self.program.contracts:
                self.used_contracts.add(node.id)
                return Namespace("contract:" + node.id)
            self.fail(
                node, f"Name {node.id!r} is not defined on every incoming path", "name_unavailable"
            )
        if isinstance(node, ast.Attribute):
            owner = self.expr(node.value)
            if isinstance(owner, Subject):
                matches = [
                    t
                    for t in self.program.claim_types.values()
                    if owner.kind in t.structure.allowed_subject_kinds
                    and t.structure.predicate.rsplit(".", 1)[-1] == node.attr
                ]
                if len(matches) != 1:
                    self.fail(
                        node,
                        f"Field {node.attr!r} is absent or ambiguous for {owner.kind!r}",
                        "unknown_field",
                    )
                self.used_types.add(matches[0].structure.predicate)
                return ClaimSelection(owner, matches[0])
            if isinstance(owner, SourceClaimType):
                members = (owner.structure.literal_schema or {}).get("enum", [])
                if (
                    node.attr == "value"
                    and (owner.structure.literal_schema or {}).get("type") == "object"
                ):
                    return Constructor(
                        RecordConstructor.from_json_schema(
                            owner.structure.literal_schema or {}
                        ).schema
                    )
                if isinstance(members, list) and node.attr in members:
                    return Value(
                        node.attr,
                        PropertySchema(type="json", json_schema=owner.structure.literal_schema),
                        True,
                        "input",
                    )
                self.fail(node, f"No declared ClaimType member {node.attr!r}", "unknown_field")
            if isinstance(owner, Namespace):
                if owner.name == "world" or owner.name.startswith("world:"):
                    prefix = "" if owner.name == "world" else owner.name[6:] + "."
                    path = prefix + node.attr
                    if path in self.program.claim_types:
                        self.used_types.add(path)
                        return self.program.claim_types[path]
                    if path in self.program.subject_kinds or any(
                        k.startswith(path + ".")
                        for k in (*self.program.subject_kinds, *self.program.claim_types)
                    ):
                        return Namespace("world:" + path)
                    self.fail(node, f"No accepted ontology member {path!r}", "unknown_field")
                if owner.name == "bindings":
                    self.used_bindings.add(node.attr)
                    binding = self.program.bindings.get(node.attr)
                    if not isinstance(binding, SourceProviderBinding | SourceQueryBinding):
                        self.fail(
                            node,
                            f"Binding {node.attr!r} is missing or unsupported",
                            "binding_required",
                        )
                    return binding
                if owner.name.startswith("contract:") and node.attr == "value":
                    return Constructor(self.program.contracts[owner.name[9:]].schema_)
                self.fail(node, f"Unknown source member {node.attr!r}", "unknown_field")
            if isinstance(owner, SourceProviderBinding) and node.attr == "input":
                return Constructor(owner.operation.input)
            if isinstance(owner, SourceQueryBinding) and node.attr == "parameters":
                from cruxible_client.contracts.query.parameters import QueryParameters

                return Constructor(QueryParameters(owner.definition).schema, query=owner)
            if isinstance(owner, Constructor):
                field = owner.schema.fields.get(
                    record_field_names(owner.schema).get(node.attr, node.attr)
                )
                if field is not None and field.item_fields is not None:
                    return Constructor(ContractSchema(fields=field.item_fields))
                if field is not None and field.json_schema is not None:
                    try:
                        return Constructor(
                            RecordConstructor.from_json_schema(field.json_schema).schema
                        )
                    except ValueError as exc:
                        self.fail(node, str(exc), "unknown_field")
                self.fail(node, f"No declared nested constructor {node.attr!r}", "unknown_field")
            if isinstance(owner, Value) and isinstance(owner.type, ContractSchema):
                field = owner.type.fields.get(
                    record_field_names(owner.type).get(node.attr, node.attr)
                )
                if field is None:
                    self.fail(node, f"No declared field {node.attr!r}", "unknown_field")
                wire_name = record_field_names(owner.type).get(node.attr, node.attr)
                if isinstance(owner.wire, dict) and wire_name not in owner.wire:
                    self.fail(
                        node, f"Optional field {wire_name!r} was not supplied", "field_unavailable"
                    )
                wire = (
                    owner.wire.get(wire_name)
                    if isinstance(owner.wire, dict)
                    else owner.wire + "." + wire_name
                )
                field_type: ValueType = field
                if field.json_schema and field.json_schema.get("properties") is not None:
                    field_type = read_object_schema(field.json_schema)
                return Value(wire, field_type, phase=owner.phase)
            self.fail(node, f"Field {node.attr!r} has no declared record owner", "unknown_field")
        if isinstance(node, ast.Subscript):
            owner = self.expr(node.value)
            if (
                isinstance(owner, Namespace)
                and owner.name.startswith("world:")
                and owner.name[6:] in self.program.subject_kinds
            ):
                identity = self.value(node.slice)
                if _json_type(identity.type).get("type") != "string" or identity.phase == "runtime":
                    self.fail(
                        node.slice,
                        "A Subject selector must be a string bound before execution",
                        "late_state_selector",
                    )
                self.used_kinds.add(owner.name[6:])
                return Subject(owner.name[6:], identity)
            if isinstance(owner, Subject):
                predicate = self.value(node.slice)
                if not predicate.literal or predicate.wire not in self.program.claim_types:
                    self.fail(node.slice, "Select a declared predicate name", "unknown_field")
                selected = self.program.claim_types[predicate.wire]
                self.used_types.add(predicate.wire)
                if owner.kind not in selected.structure.allowed_subject_kinds:
                    self.fail(
                        node, "Predicate does not admit this Subject kind", "contract_mismatch"
                    )
                return ClaimSelection(owner, selected)
            self.fail(node, "Subscripts select named Subjects or declared predicates")
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute) and node.func.attr in {"one", "all"}:
                selected = self.expr(node.func.value)
                if not isinstance(selected, ClaimSelection) or node.args or node.keywords:
                    self.fail(node, "one/all select a typed Claim field without arguments")
                return self.claim_read(selected, node, node.func.attr)
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr in {"claim_type", "kind"}
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "world"
            ):
                if len(node.args) != 1 or node.keywords:
                    self.fail(node, "Ontology lookup needs one literal name")
                selected = self.value(node.args[0])
                if not selected.literal or not isinstance(selected.wire, str):
                    self.fail(node, "Ontology names are resolved at compile time")
                if node.func.attr == "kind" and selected.wire in self.program.subject_kinds:
                    return Namespace("world:" + selected.wire)
                if node.func.attr == "claim_type" and selected.wire in self.program.claim_types:
                    self.used_types.add(selected.wire)
                    return self.program.claim_types[selected.wire]
                self.fail(
                    node, "No accepted ontology definition matches this name", "unknown_field"
                )
            if isinstance(node.func, ast.Name) and node.func.id in {"call", "source", "query"}:
                return self.operation(node)
            if isinstance(node.func, ast.Name) and node.func.id == "claim_candidate":
                return self.candidate(node)
            target = self.expr(node.func)
            if isinstance(target, Constructor):
                return self.record(target, node)
            self.fail(node, "Arbitrary calls are not part of the Procedure source language")
        if (
            isinstance(node, ast.Dict)
            and isinstance(expected, PropertySchema)
            and expected.type == "json"
            and not (expected.json_schema or {}).get("properties")
        ):
            values: dict[str, Any] = {}
            for key, element in zip(node.keys, node.values, strict=True):
                if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                    self.fail(node, "Open JSON keys must be literal strings")
                values[key.value] = self.wire(
                    self.value(element, expected=PropertySchema(type="json")), element
                )
            return Value(values, expected)
        if (
            isinstance(node, ast.List)
            and isinstance(expected, PropertySchema)
            and expected.type == "json"
        ):
            return Value(
                [
                    self.wire(self.value(v, expected=PropertySchema(type="json")), v)
                    for v in node.elts
                ],
                expected,
            )
        self.fail(
            node,
            f"Python {type(node).__name__} is not supported by this source version",
            hint="Use typed records, explicit bindings, if/else and Procedure operations.",
        )

    def operation(self, node: ast.Call) -> Expression:
        name = node.func.id  # type: ignore[attr-defined]
        if len(node.args) != 1 or any(k.arg is None for k in node.keywords):
            self.fail(node, f"{name} requires one explicit binding and named arguments")
        binding = self.expr(node.args[0])
        kwargs = {k.arg: k.value for k in node.keywords}
        if name == "query":
            if not isinstance(binding, SourceQueryBinding) or set(kwargs) != {"parameters"}:
                self.fail(
                    node,
                    "query requires an accepted query binding and typed parameters",
                    "binding_kind",
                )
            from cruxible_client.contracts.query.parameters import QueryParameters

            value = self.value(kwargs["parameters"])
            if value.type != QueryParameters(binding.definition).schema:
                self.fail(node, "Use this query's typed parameter constructor", "contract_mismatch")
            if value.phase == "runtime":
                self.fail(
                    node,
                    "Admitted query parameters cannot depend on provider or runtime outputs",
                    "late_state_selector",
                )
            query_pin = ArtifactPin(
                role="query",
                target=ArtifactIdentity(
                    kind="QueryDefinition", name=binding.name.removeprefix("QueryDefinition:")
                ),
                artifact_digest=binding.version,
            )
            emitted = self.append(
                "state_tap",
                node,
                query=query_pin.model_dump(mode="json"),
                parameters=self.wire(value, node),
                **{"as": True},
            )
            return Value("$steps." + emitted["as"], query_view_schema(), phase="state")
        if not isinstance(binding, SourceProviderBinding):
            self.fail(node.args[0], "Expected an accepted provider binding", "binding_kind")
        argument = "request" if name == "source" else "input"
        if argument not in kwargs:
            self.fail(node, f"{name} requires {argument}")
        value = self.value(kwargs[argument])
        if not isinstance(value.type, ContractSchema) or value.type != binding.operation.input:
            self.fail(
                kwargs[argument],
                "Use the selected provider's typed input constructor",
                "contract_mismatch",
            )

        def pin(role: str, kind: str, target: str, digest: str) -> dict[str, Any]:
            return ArtifactPin(
                role=role,
                target=ArtifactIdentity(kind=kind, name=target.removeprefix(kind + ":")),
                artifact_digest=digest,
            ).model_dump(mode="json")

        fields = dict(
            provider=pin("provider", "Provider", binding.provider, binding.provider_version),
            interface=pin(
                "provider-interface",
                "ProviderInterface",
                binding.interface,
                binding.interface_version,
            ),
            interface_digest=binding.interface_digest,
            implementation_digest=binding.implementation_digest,
        )
        if name == "source":
            if set(kwargs) != {"request", "capture_contract"}:
                self.fail(node, "source requires request and capture_contract")
            if binding.operation.material is None:
                self.fail(
                    node,
                    "This acquisition interface does not declare its captured material schema",
                    "material_contract_required",
                    "Publish the provider material schema before using typed Source fields.",
                )
            capture_pin = self.capture_pin(kwargs["capture_contract"])
            emitted = self.append(
                "source",
                node,
                **fields,
                request=self.wire(value, node),
                capture_contract=capture_pin,
                **{"as": True},
            )
            return Observation("$steps." + emitted["as"], binding.operation.material)
        if set(kwargs) != {"input"}:
            self.fail(node, "call accepts only its typed input in this form")
        if not isinstance(binding.operation.output, ContractSchema):
            self.fail(node, "An acquisition interface must be used through source", "binding_kind")
        input_pin = self.anonymous(binding.operation.input)
        input_pin["role"] = "contract-in"
        output_pin = self.anonymous(binding.operation.output)
        emitted = self.append(
            "call",
            node,
            **fields,
            input=self.wire(value, node),
            contract_in=input_pin,
            contract_out=output_pin,
            effect_policy=None,
            **{"as": True},
        )
        return Value("$steps." + emitted["as"], binding.operation.output)

    def claim_read(self, selected: ClaimSelection, node: ast.Call, cardinality: str) -> Value:
        shape = selected.type.structure
        if shape.object_kind != "literal":
            self.fail(
                node, "This source version requires a literal Claim field", "claim_value_kind"
            )
        schema = ContractSchema(
            fields={
                "value": PropertySchema(type="json", json_schema=shape.literal_schema),
                "verdict": PropertySchema(type="string"),
                "currency": PropertySchema(type="string"),
                "identity": PropertySchema(type="string"),
                "artifact_digest": PropertySchema(type="string"),
                "statement_digest": PropertySchema(type="string"),
                "path": PropertySchema(type="string"),
                "claim": PropertySchema(type="json"),
            }
        )
        pin = ArtifactPin(
            role="claim-type",
            target=ArtifactIdentity(kind="ClaimType", name=shape.predicate),
            artifact_digest=selected.type.version,
        )
        emitted = self.append(
            "state_claim",
            node,
            claim_type=pin.model_dump(mode="json"),
            subject_kind=selected.subject.kind,
            subject_id=self.wire(selected.subject.identity, node),
            cardinality=cardinality,
            **{"as": True},
        )
        return ClaimValue(
            "$steps." + emitted["as"],
            schema
            if cardinality == "one"
            else PropertySchema(type="list", item_fields=schema.fields),
            phase="state",
        )

    def candidate(self, node: ast.Call) -> ClaimCandidate:
        fields = {k.arg: k.value for k in node.keywords}
        required = {"subject", "predicate", "value", "role", "rationale"}
        optional = {"supported_by", "copied_from", "self_source", "qualifier", "revises", "basis"}
        if node.args or not required.issubset(fields) or set(fields) - required - optional:
            self.fail(
                node, "claim_candidate needs a typed statement, role, rationale and one source"
            )
        subject, predicate = self.expr(fields["subject"]), self.expr(fields["predicate"])
        if not isinstance(subject, Subject) or not isinstance(predicate, SourceClaimType):
            self.fail(node, "Use an accepted Subject and ClaimType", "contract_mismatch")
        shape = predicate.structure
        if subject.kind not in shape.allowed_subject_kinds:
            self.fail(node, "ClaimType does not admit this Subject kind", "contract_mismatch")
        if shape.object_kind != "literal":
            self.fail(
                node,
                "This source form currently needs a literal-valued ClaimType",
                "contract_mismatch",
            )
        expected = PropertySchema(type="json", json_schema=shape.literal_schema)
        value = self.value(fields["value"], expected=expected)
        if value.literal:
            try:
                RecordConstructor(ContractSchema(fields={"value": expected}))(value=value.wire)
            except ValueError as exc:
                self.fail(fields["value"], str(exc), "contract_value_invalid")
        elif not _assignable(value.type, expected):
            self.fail(
                fields["value"],
                "Value is outside the accepted ClaimType range",
                "contract_mismatch",
            )
        role = self.value(fields["role"])
        if not role.literal or role.wire not in shape.permitted_roles:
            self.fail(fields["role"], "Role is not admitted by this ClaimType", "contract_mismatch")
        rationale = self.value(fields["rationale"])
        if _json_type(rationale.type).get("type") != "string":
            self.fail(fields["rationale"], "Rationale must be text", "contract_mismatch")
        sources = set(fields) & {"supported_by", "copied_from", "self_source"}
        if len(sources) != 1:
            self.fail(
                node,
                "Choose exactly one supported_by, copied_from or self_source",
                "evidence_required",
            )
        source_kind = sources.pop()
        source = self.expr(fields[source_kind])
        if source_kind == "self_source":
            if not isinstance(source, Value) or _json_type(source.type).get("type") != "string":
                self.fail(node, "self_source must be text", "contract_mismatch")
        elif not isinstance(source, Observation):
            self.fail(
                fields[source_kind],
                "Cite a verified observation or child capture handle",
                "evidence_required",
            )
        basis: list[Any] = []
        if "basis" in fields:
            supplied = fields["basis"]
            if not isinstance(supplied, ast.Tuple | ast.List):
                self.fail(supplied, "basis must list exact selected Claims")
            for element in supplied.elts:
                selected = self.expr(element)
                if not isinstance(selected, ClaimValue) or not isinstance(
                    selected.type, ContractSchema
                ):
                    self.fail(
                        element,
                        "basis needs an exact Claim selected with one()",
                        "contract_mismatch",
                    )
                basis.append(self.wire(selected, element))
        if bool(basis) != (role.wire == "derivation"):
            self.fail(
                node,
                "Derivation Claims require basis; other roles cannot claim derivation inputs",
                "contract_mismatch",
            )
        extras: dict[str, Any] = {}
        for name in ("qualifier", "revises"):
            if name in fields:
                extra = self.value(fields[name])
                if _json_type(extra.type).get("type") not in {"string", "null"}:
                    self.fail(fields[name], f"{name} must be text or null", "contract_mismatch")
                extras[name] = self.wire(extra, node)
        return ClaimCandidate(
            dict(
                tag="playbill-source-claim-candidate-v1",
                subject_kind=subject.kind,
                subject_id=self.wire(subject.identity, node),
                predicate=shape.predicate,
                value=self.wire(value, node),
                role=role.wire,
                rationale=self.wire(rationale, node),
                source_kind=source_kind,
                source_value=self.wire(source, node),
                source_alias=None
                if source_kind == "self_source"
                else source.wire.removeprefix("$steps."),
                basis=basis,
                **extras,
            )
        )

    def proposal_terminal(self, call: ast.Call, stmt: ast.Return) -> None:
        fields = {k.arg: k.value for k in call.keywords}
        if call.args or set(fields) != {"candidates", "result"}:
            self.fail(call, "propose_change_set needs candidates and a typed result")
        candidates = fields["candidates"]
        if not isinstance(candidates, ast.Tuple | ast.List) or not candidates.elts:
            self.fail(candidates, "Provide a nonempty list of typed Claim candidates")
        templates = []
        for element in candidates.elts:
            candidate = self.expr(element)
            if not isinstance(candidate, ClaimCandidate):
                self.fail(element, "Use claim_candidate to construct each proposal member")
            templates.append(candidate.wire)
        result = self.value(fields["result"])
        if result.type != self.output.schema_:
            self.fail(
                fields["result"],
                "Return the declared output contract's typed record",
                "return_contract",
            )
        self.append(
            "propose_change_set",
            stmt,
            candidate_templates=templates,
            claim_types=[
                ArtifactPin(
                    role="claim-type",
                    target=ArtifactIdentity(kind="ClaimType", name=name),
                    artifact_digest=self.program.claim_types[name].version,
                ).model_dump(mode="json")
                for name in sorted({t["predicate"] for t in templates})
            ],
            result=self.wire(result, call),
        )

    def capture_pin(self, node: ast.AST) -> dict[str, Any]:
        value = self.value(node)
        if not value.literal or not isinstance(value.wire, str):
            self.fail(node, "Capture contract must be an accepted name", "binding_required")
        name = value.wire.removeprefix("CaptureContract:")
        version = self.program.capture_contracts.get(name)
        if version is None:
            self.fail(
                node,
                f"CaptureContract {name!r} was not resolved at the authoring base",
                "binding_required",
            )
        return ArtifactPin(
            role="capture-contract",
            target=ArtifactIdentity(kind="CaptureContract", name=name),
            artifact_digest=version,
        ).model_dump(mode="json")

    def capture_terminal(self, call: ast.Call, stmt: ast.Return) -> None:
        kwargs = {k.arg: k.value for k in call.keywords}
        if len(call.args) != 1 or set(kwargs) != {"capture_contract", "result"}:
            self.fail(call, "emit_capture requires evidence, capture_contract and typed result")
        evidence = self.expr(call.args[0])
        if not isinstance(evidence, Observation):
            self.fail(
                call.args[0], "emit_capture requires verified Source evidence", "evidence_required"
            )
        value = self.value(kwargs["result"])
        if value.type != self.output.schema_:
            self.fail(
                kwargs["result"],
                "Return the declared output contract's typed record",
                "return_contract",
            )
        self.append(
            "emit_capture",
            stmt,
            input=self.wire(evidence, call),
            capture_contract=self.capture_pin(kwargs["capture_contract"]),
            result=self.wire(value, call),
        )

    def operand(self, value: Value, at: ast.AST) -> dict[str, Any]:
        if value.literal:
            return PredicateOperandV1(kind="literal", value=value.wire).model_dump(mode="json")
        if isinstance(value.wire, str) and value.wire.startswith("$input."):
            first, *path = value.wire[7:].split(".")
            return PredicateOperandV1(kind="input", input_name=first, path=tuple(path)).model_dump(
                mode="json"
            )
        if isinstance(value.wire, str) and value.wire.startswith("$steps."):
            alias, *path = value.wire[7:].split(".")
            return PredicateOperandV1(kind="step", alias=alias, path=tuple(path)).model_dump(
                mode="json"
            )
        self.fail(at, "This value cannot be used as a guard operand", "guard_operand")

    def condition(
        self, node: ast.AST, code: str = "condition", message: str = "Condition refused"
    ) -> tuple[list[Edge], list[Edge]]:
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            yes, no = self.condition(node.operand, code, message)
            return no, yes
        if isinstance(node, ast.BoolOp):
            yes, no = self.condition(node.values[0], code, message)
            for term in node.values[1:]:
                self.tail = yes if isinstance(node.op, ast.And) else no
                next_yes, next_no = self.condition(term, code, message)
                yes, no = (
                    (next_yes, no + next_no)
                    if isinstance(node.op, ast.And)
                    else (yes + next_yes, next_no)
                )
            return yes, no
        if isinstance(node, ast.Compare):
            if len(node.ops) != 1:
                self.fail(node, "Chained comparisons require explicit and conditions")
            operators = {
                ast.Eq: "eq",
                ast.NotEq: "ne",
                ast.Lt: "lt",
                ast.LtE: "lte",
                ast.Gt: "gt",
                ast.GtE: "gte",
            }
            operator = operators.get(type(node.ops[0]))
            if operator is None:
                self.fail(node, "Unsupported comparison operator")
            left, right = self.value(node.left), self.value(node.comparators[0])
            if _json_type(left.type).get("type") not in {
                "string",
                "integer",
                "boolean",
            } or not _same_base_type(left.type, right.type):
                self.fail(node, "Comparison operands have incompatible types", "contract_mismatch")
        else:
            left = self.value(node)
            if _json_type(left.type).get("type") != "boolean":
                self.fail(
                    node, "Conditions require booleans; implicit Python truthiness is unsupported"
                )
            right, operator = Value(True, PropertySchema(type="bool"), True), "eq"
        predicate = {
            "left": self.operand(left, node),
            "operator": operator,
            "right": self.operand(right, node),
        }
        guard = self.append(
            "guard",
            node,
            predicate=predicate,
            on_true=None,
            on_false="$abort",
            refusal_code=code,
            message=message,
        )
        self.tail = []
        return [(guard, "on_true")], [(guard, "on_false")]

    def statements(self, statements: list[ast.stmt]) -> bool:
        terminated = False
        for stmt in statements:
            if terminated:
                self.fail(stmt, "Statement is unreachable after all paths terminate", "unreachable")
            if (
                isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)
            ):
                continue  # retained docstring, never executable
            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
            ):
                if (
                    stmt.targets[0].id
                    in {
                        "request",
                        "world",
                        "bindings",
                        "query",
                        "call",
                        "source",
                        "invoke",
                        "require",
                        "halt",
                        "emit_capture",
                        "claim_candidate",
                        "propose_change_set",
                    }
                    or stmt.targets[0].id in self.program.contracts
                ):
                    self.fail(
                        stmt,
                        "Local assignments cannot shadow Procedure inputs or intrinsics",
                        "reserved_name",
                    )
                self.assignment_alias = stmt.targets[0].id
                self.environment[stmt.targets[0].id] = self.expr(stmt.value)
                self.assignment_alias = None
            elif (
                isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and stmt.value is not None
            ):
                self.fail(stmt, "Local annotations are not interpreted; types come from contracts")
            elif isinstance(stmt, ast.If):
                yes, no = self.condition(stmt.test)
                initial = dict(self.environment)
                self.tail = yes
                left_done = self.statements(stmt.body)
                left_env, left_tail = dict(self.environment), self.tail
                self.environment = dict(initial)
                self.tail = no
                right_done = self.statements(stmt.orelse)
                right_env, right_tail = dict(self.environment), self.tail
                if left_done and right_done:
                    terminated = True
                    continue
                if left_done or right_done:
                    self.environment = right_env if left_done else left_env
                    self.tail = right_tail if left_done else left_tail
                    continue
                common = left_env.keys() & right_env.keys()
                changed = sorted(name for name in common if left_env[name] != right_env[name])
                self.environment = {name: left_env[name] for name in common if name not in changed}
                if not changed:
                    self.tail = left_tail + right_tail
                    continue
                fields: dict[str, PropertySchema] = {}
                for name in changed:
                    left, right = left_env[name], right_env[name]
                    if (
                        not isinstance(left, Value)
                        or not isinstance(right, Value)
                        or not _same_base_type(left.type, right.type)
                        or _field_type(left.type).json_schema != _field_type(right.type).json_schema
                    ):
                        self.fail(
                            stmt,
                            f"Branch values for {name!r} have incompatible types",
                            "branch_type",
                        )
                    field = _field_type(left.type).model_copy(deep=True)
                    other = _field_type(right.type)
                    field.enum = (
                        list({canonical_bytes(v): v for v in field.enum + other.enum}.values())
                        if field.enum and other.enum
                        else None
                    )
                    fields[name] = field
                schema = ContractSchema(fields=fields)
                contract = self.anonymous(schema)
                self.tail = left_tail
                lnode = self.append(
                    "project",
                    stmt,
                    fields={k: self.wire(cast(Value, left_env[k]), stmt) for k in changed},
                    contract_out=contract,
                    **{"as": True},
                )
                ltail = self.tail
                self.tail = right_tail
                rnode = self.append(
                    "project",
                    stmt,
                    fields={k: self.wire(cast(Value, right_env[k]), stmt) for k in changed},
                    contract_out=contract,
                    **{"as": True},
                )
                self.tail += ltail
                joined = self.append(
                    "select",
                    stmt,
                    sources=[lnode["as"], rnode["as"]],
                    contract_out=contract,
                    **{"as": True},
                )
                for name in changed:
                    self.environment[name] = Value(f"$steps.{joined['as']}.{name}", fields[name])
            elif (
                isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Call)
                and isinstance(stmt.value.func, ast.Name)
                and stmt.value.func.id == "require"
            ):
                call = stmt.value
                kwargs = {k.arg: k.value for k in call.keywords}
                if len(call.args) != 1 or set(kwargs) != {"code", "message"}:
                    self.fail(call, "require needs a condition, literal code and message")
                code, message = self.value(kwargs["code"]), self.value(kwargs["message"])
                if (
                    not code.literal
                    or not message.literal
                    or not isinstance(code.wire, str)
                    or not isinstance(message.wire, str)
                ):
                    self.fail(call, "require code and message must be literal strings")
                yes, no = self.condition(call.args[0], code.wire, message.wire)
                for guard, label in no:
                    guard[label] = "$abort"
                self.tail = yes
            elif isinstance(stmt, ast.Return):
                if stmt.value is None:
                    self.fail(stmt, "A successful return must provide the declared output")
                if (
                    isinstance(stmt.value, ast.Call)
                    and isinstance(stmt.value.func, ast.Name)
                    and stmt.value.func.id == "halt"
                ):
                    if len(stmt.value.args) != 1 or stmt.value.keywords:
                        self.fail(stmt.value, "halt requires one literal reason")
                    reason = self.value(stmt.value.args[0])
                    if not reason.literal or not isinstance(reason.wire, str):
                        self.fail(stmt.value, "halt reason must be a literal string")
                    self.append("halt", stmt, reason=reason.wire)
                elif (
                    isinstance(stmt.value, ast.Call)
                    and isinstance(stmt.value.func, ast.Name)
                    and stmt.value.func.id == "emit_capture"
                ):
                    self.capture_terminal(stmt.value, stmt)
                elif (
                    isinstance(stmt.value, ast.Call)
                    and isinstance(stmt.value.func, ast.Name)
                    and stmt.value.func.id == "propose_change_set"
                ):
                    self.proposal_terminal(stmt.value, stmt)
                else:
                    value = self.value(stmt.value)
                    if (
                        not isinstance(value.type, ContractSchema)
                        or value.type != self.output.schema_
                    ):
                        self.fail(
                            stmt.value,
                            "Return the declared output contract's typed record",
                            "return_contract",
                        )
                    emitted = self.append(
                        "return",
                        stmt,
                        fields=self.wire(value, stmt),
                        contract_out=self.contract(self.output),
                        **{"as": True},
                    )
                    self.returns.append(emitted["as"])
                terminated = True
            else:
                self.fail(
                    stmt, f"Python {type(stmt).__name__} is not supported by this source version"
                )
        return terminated


def _compile_source(
    program: ProcedureSourceV1,
    *,
    name: str,
    input: SourceContract,
    output: SourceContract,
    budget: ProcedureBudgetV3,
    hard_caps: ProcedureHardCapsV3,
    terminal_capability: Literal[1, 2, 3] = 1,
    description: str | None = None,
) -> CompiledSource:
    compiler = _Compiler(program, input, output)
    try:
        module = ast.parse(program.text, filename=program.filename)
    except SyntaxError as exc:
        raise SourceCompileError(
            SourceDiagnostic(
                code="playbill.source.invalid_python",
                message=exc.msg,
                span=SourceSpan(
                    filename=program.filename,
                    line=program.first_line + (exc.lineno or 1) - 1,
                    column=max(0, (exc.offset or 1) - 1),
                    end_line=program.first_line + (exc.end_lineno or exc.lineno or 1) - 1,
                    end_column=max(0, (exc.end_offset or exc.offset or 1) - 1),
                ),
            )
        ) from exc
    if len(module.body) != 1 or not isinstance(module.body[0], ast.FunctionDef):
        compiler.fail(module, "Retained source must contain exactly one ordinary function")
    function = module.body[0]
    for call_node in ast.walk(function):
        if isinstance(call_node, ast.Call):
            keyword_names = [keyword.arg for keyword in call_node.keywords]
            if None in keyword_names or len(keyword_names) != len(set(keyword_names)):
                compiler.fail(
                    call_node, "Calls require unique explicit keyword arguments", "call_arguments"
                )
    if function.name != program.function or function.decorator_list:
        compiler.fail(
            function, "Retained function name must match; host decorators are separate metadata"
        )
    args = function.args
    if args.posonlyargs or args.kwonlyargs or args.vararg or args.kwarg or args.defaults:
        compiler.fail(
            function, "Use positional request, world and bindings parameters without defaults"
        )
    names = [arg.arg for arg in args.args]
    if (
        not names
        or names[0] != "request"
        or len(names) != len(set(names))
        or any(n not in {"world", "bindings"} for n in names[1:])
    ):
        compiler.fail(function, "Parameters are request followed by optional world and bindings")
    compiler.environment = {"request": Value("$input", input.schema_, phase="input")}
    compiler.environment.update({n: Namespace(n) for n in names[1:]})
    if not compiler.statements(function.body):
        compiler.fail(function, "Every path must explicitly return or halt", "missing_return")
    root_in = compiler.contract(input)
    root_in["role"] = "contract-in"
    definition = ProcedureDefinitionV6.model_validate(
        dict(
            name=name,
            description=description,
            contract_in=root_in,
            contract_out=compiler.contract(output),
            nodes=tuple(compiler.nodes),
            returns=compiler.returns[0] if compiler.returns else "result",
            budget=budget,
            hard_caps=hard_caps,
            terminal_capability=terminal_capability,
            source=program.model_copy(
                update={
                    "contracts": {
                        k: v for k, v in program.contracts.items() if k in compiler.used_contracts
                    },
                    "bindings": {
                        k: v for k, v in program.bindings.items() if k in compiler.used_bindings
                    },
                    "claim_types": {
                        k: v for k, v in program.claim_types.items() if k in compiler.used_types
                    },
                    "subject_kinds": tuple(
                        k for k in program.subject_kinds if k in compiler.used_kinds
                    ),
                }
            ),
        )
    )
    return CompiledSource(
        definition,
        tuple(compiler.contracts[k] for k in sorted(compiler.contracts)),
        tuple(compiler.maps),
    )


def compile_source(
    program: ProcedureSourceV1,
    *,
    name: str,
    input: SourceContract,
    output: SourceContract,
    budget: ProcedureBudgetV3,
    hard_caps: ProcedureHardCapsV3,
    terminal_capability: Literal[1, 2, 3] = 1,
    description: str | None = None,
) -> CompiledSource:
    """Return a graph or a localized diagnostic, including unsupported schemas."""
    try:
        return _compile_source(
            program,
            name=name,
            input=input,
            output=output,
            budget=budget,
            hard_caps=hard_caps,
            terminal_capability=terminal_capability,
            description=description,
        )
    except SourceCompileError:
        raise
    except (ValueError, PlaybillFormatError) as exc:
        raise SourceCompileError(
            SourceDiagnostic(
                code="playbill.source.contract_or_graph_invalid",
                message=str(exc),
                span=SourceSpan(
                    filename=program.filename,
                    line=program.first_line,
                    column=0,
                    end_line=program.first_line,
                    end_column=0,
                ),
                hint="Check the declared schemas and the graph details in this diagnostic.",
            )
        ) from exc


def verify_source_graph(procedure: Any) -> None:
    """Prove retained source/graph association without running authored Python."""
    from cruxible_client.contracts.errors import ProjectionFormatError
    from cruxible_client.contracts.procedures.artifacts import (
        ProcedureArtifactV2,
        procedure_owned_contract_digest,
    )

    definition = procedure.definition
    if not isinstance(definition, ProcedureDefinitionV6) or definition.source is None:
        return
    if not isinstance(procedure, ProcedureArtifactV2):
        raise ProjectionFormatError("Source Procedures must carry their declared Contracts")

    def root(pin: Any) -> SourceContract:
        if isinstance(pin, ArtifactPin):
            for contract in procedure.owned_contracts:
                if (
                    contract.identity == pin.target
                    and procedure_owned_contract_digest(contract).tagged == pin.artifact_digest
                ):
                    return SourceContract(
                        name=contract.identity.name, schema=contract.contract_schema
                    )
        raise ProjectionFormatError("Source root Contract is not an exact owned Contract")

    try:
        compiled = compile_source(
            definition.source,
            name=definition.name,
            input=root(definition.contract_in),
            output=root(definition.contract_out),
            budget=definition.budget,
            hard_caps=definition.hard_caps,
            terminal_capability=definition.terminal_capability,
            description=definition.description,
        )
    except ValueError as exc:
        raise ProjectionFormatError(f"Retained Procedure source does not compile: {exc}") from exc
    contracts = tuple(
        SourceContract(name=c.identity.name, schema=c.contract_schema)
        for c in procedure.owned_contracts
    )
    if compiled.definition != definition or compiled.contracts != contracts:
        raise ProjectionFormatError("Retained Procedure source and compiled graph disagree")
