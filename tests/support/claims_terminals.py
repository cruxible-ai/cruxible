"""Typed fixtures for Claim delivery, without the retired command-item wire."""

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactNameRef
from cruxible_client.contracts.procedures.claims import ClaimsNode
from cruxible_client.contracts.procedures.extract import ExtractNode
from cruxible_client.contracts.shapes import contract_digest
from tests.support.procedures import boundary_contract, contract_pin, named, procedure_definition


def delivery_definition(
    procedure,
    *,
    subject_kind,
    subject_id,
    predicates,
    node_id="claims",
    records="/",
    within=(),
    templates=None,
    create=False,
):
    source = procedure.definition.nodes[0]
    row = boundary_contract("security.delivery-row", {"severity": "string{max_len: 256}"})
    out = boundary_contract("security.delivery-output", {})
    mappings = [
        {
            "type": ArtifactNameRef(target=ArtifactIdentity(kind="ClaimType", name=predicate)),
            "value": "$item.fields.severity",
            "role": "observation",
        }
        for predicate in predicates
    ]
    if templates is not None:
        if not isinstance(templates, (list, tuple)):
            raise ValueError("provider-made command items are retired; use a typed claims template")
        mappings = []
        for item in templates:
            if not isinstance(item, dict) or "statement" not in item:
                raise ValueError("Claim commands are retired; use a typed claims template")
            prohibited = {
                "source",
                "citation_role",
                "revises",
                "retires",
                "existing_claim_dispositions",
                "procedure_evidence",
            }
            if any(item.get(key) is not None for key in prohibited):
                raise ValueError("Claim commands are retired; use a typed claims template")
            statement = item["statement"]
            obj = statement["object"]
            if obj["kind"] != "literal":
                raise ValueError("This fixture maps typed literal Claims only")
            value = obj["value"]
            if value == "$steps.result.severity":
                value = "$item.fields.severity"
            mappings.append(
                {
                    "type": ArtifactNameRef(
                        target=ArtifactIdentity(kind="ClaimType", name=statement["predicate"])
                    ),
                    "value": value,
                    "role": statement.get("role", "observation"),
                    "qualifier": statement.get("qualifier"),
                    "effective_from": statement.get("effective_from"),
                    "effective_until": statement.get("effective_until"),
                    "rationale": item["rationale"],
                }
            )
    definition = procedure_definition(
        base=procedure.definition,
        contract_out=named(contract_pin("contract-out", out)),
        returns=None,
        nodes=(
            source.model_copy(update={"next": "entries"}),
            ExtractNode(
                node_id="entries",
                capture=f"$steps.{source.as_}.capture",
                records=records,
                row=row.ref.model_copy(update={"digest": contract_digest(row).tagged}),
                key={"slug": {"at": "id", "type": f"key_part<{subject_kind}:slug>"}}
                if records != "/"
                else {},
                fields={"severity": {"at": "severity"}},
                as_="entries",
                next=node_id,
            ),
            ClaimsNode(
                node_id=node_id,
                for_each="$steps.entries.records",
                subject={
                    "kind": subject_kind,
                    "create": "if_absent" if create else "never",
                    "key": {"slug": "$item.key.slug" if records != "/" else subject_id},
                    **({"within": list(within)} if within else {}),
                },
                claims=mappings,
            ),
        ),
    )
    return definition, (row, out)
