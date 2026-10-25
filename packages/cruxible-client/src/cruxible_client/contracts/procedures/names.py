"""Lower a resolved authoring graph to its governed, name-referenced form.

This only handles the artifact being authored. It never rewrites accepted
artifacts, and it never transforms evidence or run admissions.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from cruxible_client.contracts.artifacts import ArtifactNameRef
from cruxible_client.contracts.procedures.artifacts import (
    BlueprintArtifact,
    NamedBlueprintArtifact,
    NamedProcedureArtifact,
    ProcedureArtifact,
)
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
from cruxible_client.contracts.procedures.models import iter_pin_bindings
from cruxible_client.contracts.procedures.named_models import NamedProcedureDefinition


def named_graph(value: Mapping[str, Any]) -> dict[str, Any]:
    """Change only grammar reference fields; payloads and schemas stay exact."""
    result = deepcopy(dict(value))

    def reference(item: Any) -> Any:
        if isinstance(item, dict) and "target" in item:
            return {"target": item["target"]}
        return item

    def fields(item: dict[str, Any], names: tuple[str, ...]) -> None:
        for name in names:
            if name in item:
                item[name] = reference(item[name])

    fields(result, ("contract_in", "contract_out", "parameter_contract"))
    reference_fields = {
        "state_tap": ("query",),
        "source": ("capture_contract", "provider", "interface"),
        "call": ("provider", "interface", "effect_policy"),
        "exhaust_tap": ("reducer_or_query",),
        "transform": ("contract_in", "contract_out"),
        "project": ("contract_out",),
        "select": ("contract_out",),
        "return": ("contract_out",),
        "constant": ("contract_out",),
        "emit_capture": ("capture_contract",),
        "state_claim": ("claim_type",),
        "invoke": ("procedure",),
    }
    occurrences = []
    for node in result["nodes"]:
        fields(node, reference_fields.get(node["kind"], ()))
        if node["kind"] in {"source", "call"}:
            occurrences.append(node)
        if node["kind"] == "call":
            node.pop("contract_in", None)
            node.pop("contract_out", None)
        if node["kind"] in {"propose_change_set", "settle_change_set"}:
            node["claim_types"] = [reference(item) for item in node.get("claim_types", ())]
        for body in node.get("body", ()):
            fields(body, ("provider", "interface", "contract_in", "contract_out", "effect_policy"))
            if body["operation"] == "call":
                body.pop("contract_in", None)
                body.pop("contract_out", None)
                occurrences.append(body)
    for occurrence in occurrences:
        occurrence.pop("interface_digest", None)
        occurrence.pop("implementation_digest", None)
    for slot in result.get("pin_slots", ()):
        slot["tag"] = "cruxible-procedure-slot-v2"
        slot.pop("pin_role", None)
        slot.pop("artifact_kind", None)
        slot.pop("interface_digest", None)
        matches = [
            occurrence
            for occurrence in occurrences
            if (occurrence.get("provider") or {}).get("slot_name") == slot["slot_name"]
        ]
        if "interface" in slot:
            slot["interface"] = reference(slot["interface"])
        elif matches:
            slot["interface"] = matches[0]["interface"]
    if result.get("source") is not None:
        source = result["source"]
        source.setdefault("syntax_rules", source["rules"])
        source["rules"] = "cruxible.procedure-source.v3"
        for binding in source["bindings"].values():
            if binding.get("effect_class") == "none":
                binding["effect_class"] = "pure"
            for name in (
                "provider_version",
                "interface_version",
                "version",
                "interface_digest",
                "implementation_digest",
            ):
                binding.pop(name, None)
            if binding["kind"] == "query":
                query = binding["definition"]
                query["artifact_format"] = "cruxible-query-definition-v3"
                query.pop("pins", None)
        for claim_type in source["claim_types"].values():
            claim_type.pop("version", None)
        source["capture_contracts"] = sorted(source["capture_contracts"])
    result["graph_format"] = 7
    return result


def name_procedure(
    value: ProcedureArtifact | BlueprintArtifact,
) -> ProcedureArtifact | BlueprintArtifact:
    data = value.model_dump(mode="json", by_alias=True)
    data.pop("pins", None)
    definition = NamedProcedureDefinition.model_validate(named_graph(data["definition"]))
    data["definition"] = definition
    referenced_contracts = {
        binding.target.qualified
        for binding in iter_pin_bindings(definition)
        if isinstance(binding, ArtifactNameRef) and binding.target.kind == "Contract"
    }
    data["owned_contracts"] = [
        contract
        for contract in value.owned_contracts
        if contract.identity.qualified in referenced_contracts
    ]
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
                    "provider": {
                        "target": (
                            binding["artifact_pin"]
                            if "artifact_pin" in binding
                            else binding["provider"]
                        )["target"]
                    },
                }
                for binding in origin["bindings"]
            ],
        }
    return (NamedBlueprintArtifact if blueprint else NamedProcedureArtifact).model_validate(data)
