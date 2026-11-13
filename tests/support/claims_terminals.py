"""Typed fixtures for Claim delivery, without the retired command-item wire."""

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactNameRef
from cruxible_client.contracts.procedures.claims import ClaimsNode
from cruxible_client.contracts.shapes import contract_digest
from tests.support.layouts import record_set, thin_node
from tests.support.procedures import boundary_contract, contract_pin, named, procedure_definition

#: The document header a dated delivery reads once per Capture.
DELIVERY_HEADER = {"listed_from": {"at": "/listed_from", "parse": "datetime"}}
#: Where a delivery's records are: the whole document, or one per proposal row.
_DELIVERY_PATHS = {"doc": "/", "rows": "/proposals/*"}
#: Which member keys a delivery's record: none, the row's ``id``, or its ``severity``.
_DELIVERY_KEYS = {"unkeyed": None, "id": "id", "severity": "severity"}


def delivery_row():
    return boundary_contract("security.delivery-row", {"severity": "string{max_len: 256}"})


def delivery_set_name(records: str, key: str, counted: bool) -> str:
    """The record set a delivery variant reads (the world kind declares every one)."""

    path = next(name for name, at in _DELIVERY_PATHS.items() if at == records)
    return f"delivery_{path}_{key}_{'counted' if counted else 'all'}"


def delivery_record_sets() -> dict[str, dict[str, object]]:
    """Every delivery variant's record set: path x key x counted, over the delivery row.

    A key part is a plain string; the node gives it the Subject key type of the
    kind it delivers to.
    """

    row = delivery_row()
    pinned = row.ref.model_copy(update={"digest": contract_digest(row).tagged})
    found: dict[str, dict[str, object]] = {}
    for at in _DELIVERY_PATHS.values():
        for key, member in _DELIVERY_KEYS.items():
            for counted in (False, True):
                found[delivery_set_name(at, key, counted)] = record_set(
                    at=at,
                    row=pinned,
                    fields={"severity": {"at": "severity"}},
                    key=None if member is None else {"slug": {"at": member}},
                    count_at="/count" if counted else None,
                )
    return found


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
    dated=False,
):
    source = procedure.definition.nodes[0]
    # ``dated``: each Claim is effective from the document's ``listed_from``, a
    # header read once per Capture (a row field would add extract work per row).
    row = delivery_row()
    out = boundary_contract("security.delivery-output", {})
    mappings = [
        {
            "type": ArtifactNameRef(target=ArtifactIdentity(kind="ClaimType", name=predicate)),
            "value": "$item.fields.severity",
            "role": "observation",
            **({"effective_from": "$steps.entries.header.listed_from"} if dated else {}),
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
    key_variant = (
        "id"
        if records != "/"
        else ("severity" if "$item.fields.severity" in subject_key.values() else "unkeyed")
    )
    definition = procedure_definition(
        base=procedure.definition,
        contract_out=named(contract_pin("contract-out", out)),
        returns=None,
        nodes=(
            source.model_copy(update={"next": "entries"}),
            thin_node(
                node_id="entries",
                capture=f"$steps.{source.as_}.capture",
                records=delivery_set_name(records, key_variant, bool(absence)),
                key={} if key_variant == "unkeyed" else {"slug": f"key_part<{subject_kind}:slug>"},
                only_keys=only_keys,
                header=("listed_from",) if dated else (),
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
