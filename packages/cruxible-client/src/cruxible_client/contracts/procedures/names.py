"""Lower a resolved authoring graph to its governed, name-referenced form.

This only handles the artifact being authored. It never rewrites accepted
artifacts, and it never transforms evidence or run admissions.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cruxible_client.contracts.procedures.artifacts import (
    BlueprintArtifact,
    NamedBlueprintArtifact,
    NamedProcedureArtifact,
    ProcedureArtifact,
)
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
from cruxible_client.contracts.procedures.named_models import NamedProcedureDefinition


def named_graph(value: Mapping[str, Any]) -> dict[str, Any]:
    """Drop resolved digests and field-redundant roles from one authored graph."""

    def walk(item: Any) -> Any:
        if isinstance(item, dict):
            if "target" in item and "artifact_digest" in item:
                return {"target": item["target"]}
            return {
                key: walk(member)
                for key, member in item.items()
                if key not in {"interface_digest", "implementation_digest"}
            }
        if isinstance(item, list | tuple):
            return [walk(member) for member in item]
        return item

    result = walk(dict(value))
    result["graph_format"] = 7
    for slot in result.get("pin_slots", ()):
        slot["tag"] = "cruxible-procedure-slot-v2"
        slot.pop("pin_role", None)
        slot.pop("artifact_kind", None)
        matches = [
            node
            for node in result["nodes"]
            if node.get("provider", {}).get("slot_name") == slot["slot_name"]
        ]
        matches += [
            body
            for node in result["nodes"]
            for body in node.get("body", ())
            if body.get("provider", {}).get("slot_name") == slot["slot_name"]
        ]
        if "interface" not in slot and matches:
            slot["interface"] = matches[0]["interface"]
    for node in result["nodes"]:
        if node["kind"] == "call":
            node.pop("contract_in", None)
            node.pop("contract_out", None)
    return result


def name_procedure(
    value: ProcedureArtifact | BlueprintArtifact,
) -> ProcedureArtifact | BlueprintArtifact:
    data = value.model_dump(mode="json", by_alias=True)
    data.pop("pins", None)
    definition = NamedProcedureDefinition.model_validate(named_graph(data["definition"]))
    data["definition"] = definition
    data["definition_digest"] = compute_procedure_definition_digest(definition).tagged
    blueprint = isinstance(value, BlueprintArtifact)
    data["artifact_format"] = "cruxible-blueprint-v2" if blueprint else "cruxible-procedure-v3"
    if data.get("blueprint"):
        origin = data["blueprint"]
        data["blueprint"] = {
            "tag": "cruxible-blueprint-origin-v2",
            "blueprint": {"target": origin["blueprint"]["target"]},
            "bindings": [
                {
                    "slot_name": binding["slot_name"],
                    "provider": {"target": binding["artifact_pin"]["target"]},
                }
                for binding in origin["bindings"]
            ],
        }
    return (NamedBlueprintArtifact if blueprint else NamedProcedureArtifact).model_validate(data)
