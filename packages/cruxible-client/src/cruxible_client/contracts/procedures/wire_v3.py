"""The compact wire codec of the one Procedure envelope.

Only declared model fields are compacted. JSON templates and schema JSON values
are opaque data: nulls, defaults, ids and tags inside them retain their meaning.
"""

from __future__ import annotations

from typing import Any, get_args

from pydantic import BaseModel
from pydantic_core import PydanticUndefined

from cruxible_client.contracts.procedures.contract_schema import PropertySchema
from cruxible_client.contracts.procedures.models import (
    PredicateOperand,
    ProcedureDefinition,
    ProcedureNode,
    RepeatBodyNode,
)

_NODE_MODELS = (*get_args(get_args(ProcedureNode)[0]), RepeatBodyNode)


def compact_procedure(value: Any) -> Any:
    from cruxible_client.contracts.query.definitions import (
        QueryDefinition,
        query_definition_wire,
    )

    if isinstance(value, QueryDefinition):
        return query_definition_wire(value)
    if isinstance(value, PredicateOperand) and value.kind == "literal":
        return value.value
    if isinstance(value, BaseModel):
        dumped = value.model_dump(mode="json", by_alias=True)
        result: dict[str, Any] = {}
        for name, field in type(value).model_fields.items():
            key = field.alias or name
            if key not in dumped:
                continue
            member = getattr(value, name)
            # Only transform specs discriminate a union by tag. Named
            # references and slot bindings have no historical pin-tag branch.
            if name == "tag" and not value.__class__.__name__.startswith("Transform"):
                continue
            default = field.get_default(call_default_factory=True)
            if (
                name not in {"kind", "tag", "artifact_format"}
                and not (isinstance(value, PropertySchema) and name == "type")
                and default is not PydanticUndefined
                and member == default
            ):
                continue
            result[key] = (
                compact_procedure(member) if isinstance(member, (BaseModel, tuple)) else dumped[key]
            )
            if isinstance(member, dict) and any(
                isinstance(item, BaseModel) for item in member.values()
            ):
                result[key] = {k: compact_procedure(v) for k, v in member.items()}
        if isinstance(value, _NODE_MODELS) and "node_id" in result:
            result["id"] = result.pop("node_id")
            if result.get("as") == result["id"]:
                result.pop("as")
        if isinstance(value, ProcedureDefinition):
            from cruxible_client.contracts.procedures.graph import procedure_guard_halt_targets

            guard_halts = procedure_guard_halt_targets(value)
            for index, node in enumerate(result.get("nodes", [])):
                if index + 1 < len(value.nodes):
                    following = value.nodes[index + 1].node_id
                    if node.get("next") == following and following not in guard_halts:
                        node.pop("next")
                    if node.get("on_true") == following:
                        node.pop("on_true")
        return result
    if isinstance(value, tuple):
        return [compact_procedure(item) for item in value]
    return value


def expand_procedure(document: dict[str, Any]) -> dict[str, Any]:
    """Expand only grammar positions before the ordinary typed validator."""
    document = dict(document)
    definition = dict(document["definition"])
    definition["nodes"] = [_expand_node(node) for node in definition["nodes"]]
    document["definition"] = definition
    return document


def _expand_node(node: dict[str, Any]) -> dict[str, Any]:
    node = dict(node)
    if "id" in node:
        node["node_id"] = node.pop("id")
    if node["kind"] in {
        "call",
        "read",
        "extract",
        "transform",
        "project",
        "repeat",
        "invoke",
        "select",
    }:
        node.setdefault("as", node["node_id"])
    if "predicate" in node:
        node["predicate"] = _expand_predicate(node["predicate"])
    if "until" in node:
        node["until"] = _expand_predicate(node["until"])
    if node["kind"] == "repeat":
        body = []
        for raw in node["body"]:
            item = dict(raw)
            if "id" in item:
                item["node_id"] = item.pop("id")
            item.setdefault("as", item["node_id"])
            body.append(item)
        node["body"] = body
    return node


def _expand_predicate(predicate: dict[str, Any]) -> dict[str, Any]:
    predicate = dict(predicate)
    for key in ("left", "right"):
        if key in predicate and not isinstance(predicate[key], dict):
            predicate[key] = {"kind": "literal", "value": predicate[key]}
    for key in ("all_of", "any_of"):
        if key in predicate:
            predicate[key] = [_expand_predicate(child) for child in predicate[key]]
    if "not_of" in predicate:
        predicate["not_of"] = _expand_predicate(predicate["not_of"])
    return predicate
