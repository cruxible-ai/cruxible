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
    fixed_subject=False,
    absence=None,
    many=False,
    only_keys=None,
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
    subject_key = {"slug": "$item.key.slug" if records != "/" and not fixed_subject else subject_id}
    if absence == "inverse":
        mappings = [{**mapping, "value": "high"} for mapping in mappings]
    if fixed_subject and not many:
        mappings = [{**mapping, "qualifier": "$item.key.slug"} for mapping in mappings]
    if templates is not None:
        if not isinstance(templates, (list, tuple)):
            raise ValueError("provider-made command items are retired; use a typed claims template")
        mappings = []
        creations = [
            item
            for item in templates
            if isinstance(item, dict) and "subject_kind" in item and "key" in item
        ]
        if creations:
            if len(creations) != 1:
                raise ValueError("Native claims declares one Subject key per row")
            creation = creations[0]
            subject_kind = creation["subject_kind"]
            subject_key = {
                name: "$item.fields.severity" if value == "$steps.result.severity" else value
                for name, value in creation["key"].items()
            }
            create = True
        for item in templates:
            if item in creations:
                continue
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
            reference = statement.get("subject")
            if not creations and isinstance(reference, dict) and "key" in reference:
                subject_kind = reference["subject_kind"]
                key = reference["key"]
                subject_key = key if isinstance(key, dict) else {"slug": key}
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
        if not mappings and creations:
            mappings = [
                {
                    "type": ArtifactNameRef(
                        target=ArtifactIdentity(kind="ClaimType", name=subject_kind + ".severity")
                    ),
                    "value": "$item.fields.severity",
                    "role": "observation",
                    "rationale": creations[0]["rationale"],
                }
            ]
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
                else (
                    {"slug": {"at": "severity", "type": f"key_part<{subject_kind}:slug>"}}
                    if "$item.fields.severity" in subject_key.values()
                    else {}
                ),
                only_keys=only_keys,
                complete_when={"count_at": "/count"} if absence else None,
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
                    "key": {
                        k: "$item.key.slug" if v == "$item.fields.severity" else v
                        for k, v in subject_key.items()
                    },
                    **({"within": list(within)} if within else {}),
                },
                claims=[{**mapping, "when_absent": absence} for mapping in mappings]
                if absence
                else mappings,
                **({"expect": list(within)} if absence else {}),
            ),
        ),
    )
    return definition, (row, out)
