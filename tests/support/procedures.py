"""Build accepted graph-v6 Procedures for tests.

A Procedure is one envelope (``playbill-procedure-v2``) that carries every
Contract its graph pins. Tests that only care about graph behavior name
Contracts loosely (any digest); :func:`accepted_procedure` rewrites each such
Contract pin to the exact digest of a carried Contract -- permissive unless a
schema is supplied -- so the envelope law holds without per-test bookkeeping.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle, ArtifactPin
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedure,
    ProcedureArtifact,
    ProcedureOwnedContract,
    procedure_artifact_digest,
    procedure_owned_contract_digest,
    procedure_path,
)
from cruxible_client.contracts.procedures.contract_schema import ContractSchema
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
from cruxible_client.contracts.procedures.models import ProcedureDefinition, iter_pin_bindings

PERMISSIVE_CONTRACT = ContractSchema(fields={}, allow_extra=True)


def owned_contract(name: str, schema: ContractSchema | None = None) -> ProcedureOwnedContract:
    return ProcedureOwnedContract(
        identity=ArtifactIdentity(kind="Contract", name=name),
        schema=PERMISSIVE_CONTRACT if schema is None else schema,
    )


def owned_pin(role: str, contract: ProcedureOwnedContract) -> ArtifactPin:
    return ArtifactPin(
        role=role,
        target=contract.identity,
        artifact_digest=procedure_owned_contract_digest(contract).tagged,
    )


def _pin_key(pin: ArtifactPin) -> tuple[bytes, bytes, bytes]:
    return (
        pin.role.encode("utf-8"),
        pin.target.qualified.encode("utf-8"),
        pin.artifact_digest.encode("ascii"),
    )


def _carry_contracts(
    value: Any,
    *,
    schemas: Mapping[str, ContractSchema],
    carried: dict[str, ProcedureOwnedContract],
) -> Any:
    if isinstance(value, dict):
        target = value.get("target")
        if (
            set(value) >= {"role", "target", "artifact_digest"}
            and isinstance(target, dict)
            and target.get("kind") == "Contract"
        ):
            name = str(target["name"])
            contract = carried.setdefault(name, owned_contract(name, schemas.get(name)))
            return {**value, "artifact_digest": procedure_owned_contract_digest(contract).tagged}
        return {
            key: _carry_contracts(item, schemas=schemas, carried=carried)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_carry_contracts(item, schemas=schemas, carried=carried) for item in value]
    return value


def carried_definition(
    definition: ProcedureDefinition | Mapping[str, Any],
    *,
    contracts: Mapping[str, ContractSchema] | None = None,
) -> tuple[ProcedureDefinition, tuple[ProcedureOwnedContract, ...]]:
    """The definition with every Contract pin pointing at a carried Contract."""

    raw = (
        definition.model_dump(mode="json", by_alias=True)
        if isinstance(definition, ProcedureDefinition)
        else dict(definition)
    )
    carried: dict[str, ProcedureOwnedContract] = {}
    rewritten = _carry_contracts(raw, schemas=contracts or {}, carried=carried)
    owned = tuple(
        sorted(
            carried.values(),
            key=lambda item: canonical_bytes(item.model_dump(mode="json", by_alias=True)),
        )
    )
    return ProcedureDefinition.model_validate(rewritten), owned


def procedure_artifact(
    definition: ProcedureDefinition | Mapping[str, Any],
    *,
    contracts: Mapping[str, ContractSchema] | None = None,
    extra_pins: Iterable[ArtifactPin] = (),
    activation_policy: str = "abort",
    lifecycle: ArtifactLifecycle | None = None,
) -> ProcedureArtifact:
    """One v2 envelope over a graph-v6 definition, its Contracts carried."""

    resolved, owned = carried_definition(definition, contracts=contracts)
    pins = {
        _pin_key(pin): pin
        for pin in (
            *(item for item in iter_pin_bindings(resolved) if isinstance(item, ArtifactPin)),
            *extra_pins,
        )
    }
    return ProcedureArtifact(
        identity=ArtifactIdentity(kind="Procedure", name=resolved.name),
        definition=resolved,
        definition_digest=compute_procedure_definition_digest(resolved).tagged,
        pins=tuple(pins[key] for key in sorted(pins)),
        owned_contracts=owned,
        activation_policy=activation_policy,  # type: ignore[arg-type]
        lifecycle=lifecycle or ArtifactLifecycle(),
    )


def accepted_procedure(
    definition: ProcedureDefinition | Mapping[str, Any],
    *,
    contracts: Mapping[str, ContractSchema] | None = None,
    extra_pins: Iterable[ArtifactPin] = (),
    activation_policy: str = "abort",
    lifecycle: ArtifactLifecycle | None = None,
) -> AcceptedProcedure:
    procedure = procedure_artifact(
        definition,
        contracts=contracts,
        extra_pins=extra_pins,
        activation_policy=activation_policy,
        lifecycle=lifecycle,
    )
    return AcceptedProcedure(
        path=procedure_path(procedure.identity.name),
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )


__all__ = [
    "PERMISSIVE_CONTRACT",
    "accepted_procedure",
    "carried_definition",
    "owned_contract",
    "owned_pin",
    "procedure_artifact",
]
