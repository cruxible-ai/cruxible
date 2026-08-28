"""Governed Procedure envelopes and acceptance laws."""

from __future__ import annotations

import json
import re
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_client.contracts.canonical import ArtifactDigest, canonical_bytes, typed_digest
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.governance import PermissionTier
from cruxible_client.contracts.procedures.contract_schema import ContractSchema, PropertySchema
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest_v3
from cruxible_client.contracts.procedures.models import (
    ProcedureDefinitionV3,
    ProcedurePinSlotRefV1,
    iter_pin_bindings,
)
from cruxible_client.contracts.semantic import SemanticAddress

_PROCEDURE_NAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")


class ProcedureFormatError(PlaybillFormatError):
    """A Procedure artifact or canonical path is invalid."""


class _StrictProcedureArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _pin_key(pin: ArtifactPin) -> tuple[bytes, bytes, bytes]:
    return (
        pin.role.encode("utf-8"),
        pin.target.qualified.encode("utf-8"),
        pin.artifact_digest.encode("ascii"),
    )


class ProcedureArtifactV1(_StrictProcedureArtifactModel):
    artifact_format: Literal["playbill-procedure-v1"] = "playbill-procedure-v1"
    identity: ArtifactIdentity
    definition: ProcedureDefinitionV3
    definition_digest: str
    pins: tuple[ArtifactPin, ...]
    activation_policy: Literal["drain", "abort", "snapshot", "epoch-check"]
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()

    @field_validator("definition_digest")
    @classmethod
    def _definition_digest(cls, value: str) -> str:
        ArtifactDigest.from_tagged(value)
        return value

    @field_validator("pins")
    @classmethod
    def _pins(cls, value: tuple[ArtifactPin, ...]) -> tuple[ArtifactPin, ...]:
        if value != tuple(sorted(value, key=_pin_key)):
            raise ValueError("Procedure pins must be canonically sorted")
        keys = tuple((pin.role, pin.target.qualified) for pin in value)
        if len(set(keys)) != len(keys):
            raise ValueError("Procedure pins must be unique by role and target")
        return value

    @model_validator(mode="after")
    def _correspondence(self) -> "ProcedureArtifactV1":
        if self.identity.kind != "Procedure" or not _PROCEDURE_NAME_RE.fullmatch(
            self.identity.name
        ):
            raise ValueError("Procedure identity must be path-addressable")
        if self.definition.name != self.identity.name:
            raise ValueError("Procedure definition name must match stable artifact identity")
        expected = compute_procedure_definition_digest_v3(self.definition).tagged
        if self.definition_digest != expected:
            raise ValueError("Procedure definition_digest does not reproduce graph-format v3")
        declared_exact = {
            (pin.role, pin.target.qualified, pin.artifact_digest) for pin in self.pins
        }
        referenced_exact = {
            (binding.role, binding.target.qualified, binding.artifact_digest)
            for binding in iter_pin_bindings(self.definition)
            if isinstance(binding, ArtifactPin)
        }
        if not referenced_exact.issubset(declared_exact):
            raise ValueError("Procedure definition contains exact pins absent from its envelope")
        declared_slots = {slot.slot_name for slot in self.definition.pin_slots}
        referenced_slots = {
            binding.slot_name
            for binding in iter_pin_bindings(self.definition)
            if isinstance(binding, ProcedurePinSlotRefV1)
        }
        if not referenced_slots.issubset(declared_slots):
            raise ValueError("Procedure definition references undeclared slots")
        return self

    @property
    def directly_runnable(self) -> bool:
        return not any(
            isinstance(binding, ProcedurePinSlotRefV1)
            for binding in iter_pin_bindings(self.definition)
        )


class ProcedureOwnedContractV1(_StrictProcedureArtifactModel):
    """A closed Contract artifact carried by exactly one Procedure envelope."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
    )

    tag: Literal["playbill-procedure-owned-contract-v1"] = "playbill-procedure-owned-contract-v1"
    identity: ArtifactIdentity
    contract_schema: ContractSchema = Field(alias="schema")

    @field_validator("contract_schema", mode="before")
    @classmethod
    def _closed_schema(cls, value: object) -> object:
        if isinstance(value, dict) and set(value) - {
            "description",
            "fields",
            "allow_extra",
        }:
            raise ValueError("owned Contract schema contains unknown fields")
        if isinstance(value, dict) and isinstance(value.get("fields"), dict):
            for name, field in value["fields"].items():
                if isinstance(field, dict) and set(field) - set(PropertySchema.model_fields):
                    raise ValueError(f"owned Contract field {name!r} contains unknown fields")
        return value

    @model_validator(mode="after")
    def _contract_identity(self) -> "ProcedureOwnedContractV1":
        if self.identity.kind != "Contract":
            raise ValueError("owned Contract identity must use kind Contract")
        return self


def procedure_owned_contract_digest(contract: ProcedureOwnedContractV1) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest,
        "playbill-procedure-owned-contract-v1",
        {
            "identity": contract.identity.model_dump(mode="json"),
            "schema": contract.contract_schema.model_dump(mode="json"),
        },
    )


def _owned_contract_key(contract: ProcedureOwnedContractV1) -> bytes:
    return canonical_bytes(contract.model_dump(mode="json", by_alias=True))


class ProcedureArtifactV2(_StrictProcedureArtifactModel):
    """Procedure envelope whose Contract closure rides with its owner."""

    artifact_format: Literal["playbill-procedure-v2"] = "playbill-procedure-v2"
    identity: ArtifactIdentity
    definition: ProcedureDefinitionV3
    definition_digest: str
    pins: tuple[ArtifactPin, ...]
    owned_contracts: tuple[ProcedureOwnedContractV1, ...]
    activation_policy: Literal["drain", "abort", "snapshot", "epoch-check"]
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()

    @field_validator("definition_digest")
    @classmethod
    def _definition_digest(cls, value: str) -> str:
        ArtifactDigest.from_tagged(value)
        return value

    @field_validator("pins")
    @classmethod
    def _pins(cls, value: tuple[ArtifactPin, ...]) -> tuple[ArtifactPin, ...]:
        if value != tuple(sorted(value, key=_pin_key)):
            raise ValueError("Procedure pins must be canonically sorted")
        keys = tuple((pin.role, pin.target.qualified) for pin in value)
        if len(set(keys)) != len(keys):
            raise ValueError("Procedure pins must be unique by role and target")
        return value

    @field_validator("owned_contracts")
    @classmethod
    def _owned_contracts(
        cls,
        value: tuple[ProcedureOwnedContractV1, ...],
    ) -> tuple[ProcedureOwnedContractV1, ...]:
        if value != tuple(sorted(value, key=_owned_contract_key)):
            raise ValueError("owned Contracts must be canonically byte-sorted")
        identities = tuple(contract.identity.qualified for contract in value)
        digests = tuple(procedure_owned_contract_digest(contract).tagged for contract in value)
        if len(set(identities)) != len(identities):
            raise ValueError("owned Contracts must be unique by identity")
        if len(set(digests)) != len(digests):
            raise ValueError("owned Contracts must be unique by digest")
        return value

    @model_validator(mode="after")
    def _correspondence(self) -> "ProcedureArtifactV2":
        if self.identity.kind != "Procedure" or not _PROCEDURE_NAME_RE.fullmatch(
            self.identity.name
        ):
            raise ValueError("Procedure identity must be path-addressable")
        if self.definition.name != self.identity.name:
            raise ValueError("Procedure definition name must match stable artifact identity")
        expected = compute_procedure_definition_digest_v3(self.definition).tagged
        if self.definition_digest != expected:
            raise ValueError("Procedure definition_digest does not reproduce graph-format v3")
        declared_exact = {
            (pin.role, pin.target.qualified, pin.artifact_digest) for pin in self.pins
        }
        referenced_bindings = tuple(
            binding
            for binding in iter_pin_bindings(self.definition)
            if isinstance(binding, ArtifactPin)
        )
        referenced_exact = {
            (binding.role, binding.target.qualified, binding.artifact_digest)
            for binding in referenced_bindings
        }
        if not referenced_exact.issubset(declared_exact):
            raise ValueError("Procedure definition contains exact pins absent from its envelope")
        declared_slots = {slot.slot_name for slot in self.definition.pin_slots}
        referenced_slots = {
            binding.slot_name
            for binding in iter_pin_bindings(self.definition)
            if isinstance(binding, ProcedurePinSlotRefV1)
        }
        if not referenced_slots.issubset(declared_slots):
            raise ValueError("Procedure definition references undeclared slots")

        contracts = {
            contract.identity.qualified: procedure_owned_contract_digest(contract).tagged
            for contract in self.owned_contracts
        }
        referenced_contracts = tuple(
            binding for binding in referenced_bindings if binding.target.kind == "Contract"
        )
        for binding in referenced_contracts:
            if contracts.get(binding.target.qualified) != binding.artifact_digest:
                raise ValueError("exact Contract binding does not resolve to its owned Contract")
        referenced_contract_keys = {
            (binding.target.qualified, binding.artifact_digest) for binding in referenced_contracts
        }
        if referenced_contract_keys != set(contracts.items()):
            raise ValueError("every owned Contract must be referenced exactly by the Procedure")
        declared_contract_keys = {
            (pin.target.qualified, pin.artifact_digest)
            for pin in self.pins
            if pin.target.kind == "Contract"
        }
        if declared_contract_keys != referenced_contract_keys:
            raise ValueError("Procedure envelope contains an unreferenced Contract pin")
        return self

    @property
    def directly_runnable(self) -> bool:
        return not any(
            isinstance(binding, ProcedurePinSlotRefV1)
            for binding in iter_pin_bindings(self.definition)
        )


ProcedureArtifactAny: TypeAlias = Annotated[
    ProcedureArtifactV1 | ProcedureArtifactV2,
    Field(discriminator="artifact_format"),
]
_PROCEDURE_ADAPTER: TypeAdapter[ProcedureArtifactAny] = TypeAdapter(ProcedureArtifactAny)


def procedure_path(name: str) -> str:
    if not _PROCEDURE_NAME_RE.fullmatch(name):
        raise ProcedureFormatError("Procedure identity is not path-addressable")
    return f"procedures/{name}.yaml"


def render_procedure(procedure: ProcedureArtifactAny) -> bytes:
    return canonical_bytes(procedure.model_dump(mode="json", by_alias=True)) + b"\n"


def parse_procedure(content: bytes, *, path: str) -> ProcedureArtifactAny:
    try:
        procedure = _PROCEDURE_ADAPTER.validate_python(json.loads(content))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProcedureFormatError("Procedure failed strict versioned validation") from exc
    if path != procedure_path(procedure.identity.name):
        raise ProcedureFormatError("Procedure identity/path disagreement")
    if render_procedure(procedure) != content:
        raise ProcedureFormatError("Procedure is not in canonical wire form")
    return procedure


def procedure_artifact_digest(procedure: ProcedureArtifactAny) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        procedure.model_dump(mode="json", by_alias=True),
    )


class AcceptedProcedureV1(_StrictProcedureArtifactModel):
    path: str
    procedure: ProcedureArtifactAny
    artifact_digest: str

    @model_validator(mode="after")
    def _binding(self) -> "AcceptedProcedureV1":
        if self.path != procedure_path(self.procedure.identity.name):
            raise ValueError("accepted Procedure path does not reproduce")
        if self.artifact_digest != procedure_artifact_digest(self.procedure).tagged:
            raise ValueError("accepted Procedure digest does not reproduce")
        return self


class ProcedureLawResultV1(_StrictProcedureArtifactModel):
    verdict: Literal["accepted", "refused"]
    artifact_digest: str | None = None
    required_tier: PermissionTier | None = None
    approval_scope: tuple[str, ...] = ()
    diagnostics: tuple[CompilerDiagnostic, ...] = ()


def _refusal(code: str, message: str, *, path: str) -> ProcedureLawResultV1:
    return ProcedureLawResultV1(
        verdict="refused",
        diagnostics=(
            CompilerDiagnostic(
                code=code,
                severity="error",
                message=message,
                subject=SemanticAddress.whole_artifact(path),
            ),
        ),
    )


def evaluate_procedure_law(
    procedure: ProcedureArtifactAny,
    *,
    path: str,
    predecessor: AcceptedProcedureV1 | None,
) -> ProcedureLawResultV1:
    """Evaluate stable identity, predecessor, and exact closure."""

    if path != procedure_path(procedure.identity.name):
        return _refusal(
            "playbill.procedure.path_mismatch",
            "Procedure identity/path disagreement.",
            path=path,
        )
    if predecessor is None:
        if procedure.lifecycle.predecessor_digest is not None:
            return _refusal(
                "playbill.procedure.predecessor_missing",
                "A new Procedure cannot name a predecessor.",
                path=path,
            )
    else:
        if procedure.identity != predecessor.procedure.identity:
            return _refusal(
                "playbill.procedure.stable_identity_changed",
                "A Procedure successor must retain stable identity.",
                path=path,
            )
        if isinstance(predecessor.procedure, ProcedureArtifactV2) and isinstance(
            procedure, ProcedureArtifactV1
        ):
            return _refusal(
                "playbill.procedure.wire_downgrade",
                "A v2 Procedure lineage cannot be succeeded by the legacy v1 wire.",
                path=path,
            )
        if procedure.lifecycle.predecessor_digest != predecessor.artifact_digest:
            return _refusal(
                "playbill.procedure.predecessor_mismatch",
                "Procedure successor does not pin its exact predecessor.",
                path=path,
            )
    return ProcedureLawResultV1(
        verdict="accepted",
        artifact_digest=procedure_artifact_digest(procedure).tagged,
        required_tier="governed_write",
        approval_scope=(),
    )


__all__ = [
    "AcceptedProcedureV1",
    "ProcedureArtifactAny",
    "ProcedureArtifactV1",
    "ProcedureArtifactV2",
    "ProcedureFormatError",
    "ProcedureLawResultV1",
    "ProcedureOwnedContractV1",
    "evaluate_procedure_law",
    "parse_procedure",
    "procedure_artifact_digest",
    "procedure_owned_contract_digest",
    "procedure_path",
    "render_procedure",
]
