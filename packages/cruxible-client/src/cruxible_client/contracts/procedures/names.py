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
        "source": ("capture_contract",),
        "call": ("effect_policy",),
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
    for node in result["nodes"]:
        fields(node, reference_fields.get(node["kind"], ()))
        if node["kind"] in {"propose_change_set", "settle_change_set"}:
            node["claim_types"] = [reference(item) for item in node.get("claim_types", ())]
        for body in node.get("body", ()):
            fields(body, ("contract_in", "contract_out", "effect_policy"))
    if result.get("source") is not None:
        source = result["source"]
        source.setdefault("syntax_rules", source["rules"])
        source["rules"] = "cruxible.procedure-source.v3"
        for binding in source["bindings"].values():
            binding.pop("version", None)
            if binding["kind"] == "query":
                query = binding["definition"]
                query["artifact_format"] = "cruxible-query-definition-v3"
                query.pop("pins", None)
        for claim_type in source["claim_types"].values():
            claim_type.pop("version", None)
        source["capture_contracts"] = sorted(source["capture_contracts"])
    result["graph_format"] = 7
    return result


def name_procedure(value: ProcedureArtifact) -> ProcedureArtifact:
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
    data["artifact_format"] = "cruxible-procedure-v3"
    return NamedProcedureArtifact.model_validate(data)
