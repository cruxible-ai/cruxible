"""Typed ``post_inbox`` terminals for tests: one note Contract and a pinned node builder."""

from __future__ import annotations

from typing import Any

from cruxible_client.contracts.procedures.models import InboxEgressNode
from cruxible_client.contracts.shapes import (
    Contract,
    contract_digest,
    contract_path,
    render_contract,
)

#: The inbox item most tests post: one note.
NOTE = Contract.model_validate(
    {"name": "test.inbox-note", "version": 1, "fields": {"note": {"type": "string"}}}
)


def contract_members(*contracts: Contract) -> dict[str, bytes]:
    """The accepted-tree members that hold ``contracts`` (default: the note)."""

    return {
        contract_path(item.name, item.version): render_contract(item)
        for item in (contracts or (NOTE,))
    }


def inbox_node(
    node_id: str = "inbox",
    *,
    fields: dict[str, object],
    for_each: str | None = None,
    contract: Contract = NOTE,
    **extra: Any,
) -> InboxEgressNode:
    """A post_inbox terminal posting ``fields`` (per ``for_each`` element), typed ``contract``."""

    return InboxEgressNode.model_validate(
        {
            "node_id": node_id,
            "contract": {
                "name": contract.name,
                "version": contract.version,
                "digest": contract_digest(contract).tagged,
            },
            "fields": fields,
            **({} if for_each is None else {"for_each": for_each}),
            **extra,
        }
    )


def listed_inbox_procedure(template: Any, rows: list[dict[str, Any]], contract: Contract) -> Any:
    """``template`` (one shaping project) then a literal list of ``rows``, each posted.

    The list is the run's own data: a literal project typed by an owned list
    Contract, and a ``for_each`` over it posting each row as ``contract``.
    """

    from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
    from cruxible_client.contracts.canonical import canonical_bytes
    from cruxible_client.contracts.procedures.artifacts import (
        ProcedureOwnedContract,
        procedure_owned_contract_digest,
    )
    from cruxible_client.contracts.procedures.contract_schema import ContractSchema, PropertySchema
    from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
    from cruxible_client.contracts.procedures.models import ProjectNode
    from tests.support.procedures import named

    (shape,) = template.definition.nodes
    listed = ProcedureOwnedContract(
        identity=ArtifactIdentity(kind="Contract", name=f"{template.identity.name}-rows"),
        schema=ContractSchema(
            fields={
                "items": PropertySchema(
                    type="list",
                    item_fields={name: PropertySchema(type="string") for name in contract.fields},
                )
            }
        ),
    )
    pin = ArtifactPin(
        role="contract-out",
        target=listed.identity,
        artifact_digest=procedure_owned_contract_digest(listed).tagged,
    )
    definition = template.definition.model_copy(
        update={
            "nodes": (
                shape.model_copy(update={"next": "listed"}),
                ProjectNode(
                    node_id="listed",
                    literal={"items": rows},
                    contract_out=named(pin),
                    as_="listed",
                    next="inbox",
                ),
                inbox_node(
                    fields={name: f"$item.{name}" for name in contract.fields},
                    for_each="$steps.listed.items",
                    contract=contract,
                ),
            ),
            "terminal_capability": 1,
        }
    )
    return template.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest(definition).tagged,
            "owned_contracts": tuple(
                sorted(
                    (*template.owned_contracts, listed),
                    key=lambda item: canonical_bytes(item.model_dump(mode="json", by_alias=True)),
                )
            ),
        }
    )


__all__ = ["NOTE", "contract_members", "inbox_node", "listed_inbox_procedure"]
