"""Field-typed dependencies of governed artifacts (not evidence or receipts)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactNameRef,
    ArtifactPin,
    ArtifactRef,
)
from cruxible_client.contracts.shapes import CONTRACT_PIN_ROLE, contract_identity

# These registry/manifest kinds have no governed ledger envelope. A digest
# names their material; interpreting it as a head-bound name would lose it.
EXACT_REGISTRY_TARGET_KINDS = frozenset(
    {
        "EffectPolicy",
        "EnvironmentManifest",
        "ExhaustReducer",
        "LandingFilter",
        "Policy",
        "ReceiptSetManifest",
        "Reducer",
    }
)

FIELD_ROLES = {
    "contract_in": "contract-in",
    "contract_out": "contract-out",
    "parameter_contract": "parameter-contract",
    "query": "query",
    "reducer_or_query": "query",
    "provider": "provider",
    "capture_contract": "capture-contract",
    "claim_type": "claim-type",
    "claim_types": "claim-type",
    "procedure": "procedure",
    "effect_policy": "effect-policy",
    "capture_contracts": "capture-contract",
    "line": "line",
    "coordinate_schemas": "coordinate-schema",
    "selector_schemas": "selector-schema",
    "commitment_canonicalizer": "commitment-canonicalizer",
    "replay_policy": "replay-policy",
    "provenance_rule": "provenance-rule",
    "source_subject_mapping": "source-subject-mapping",
    "coordinate_grammar": "coordinate-grammar",
    "proof_adapter": "proof-adapter",
    "erasure_rule": "erasure-rule",
}
NAME_REFERENCE_FAMILIES = frozenset(
    {
        "procedure",
        "query-definition",
        "trigger",
        "line",
        "capture-contract",
        "provider",
    }
)


def is_frozen_dependency(source_kind: str, role: str) -> bool:
    return source_kind not in NAME_REFERENCE_FAMILIES


def dependency_follows_name(
    source_kind: str,
    pins: tuple[ArtifactPin | ArtifactRef, ...],
    target: ArtifactIdentity,
    role: str,
) -> bool:
    return not is_frozen_dependency(source_kind, role) or any(
        isinstance(pin, ArtifactRef) and pin.target == target and pin.role == role for pin in pins
    )


def dependency_pins(
    artifact: Any, *, by_name: bool = True
) -> tuple[ArtifactPin | ArtifactRef, ...]:
    """Old exact spellings plus new field-typed names, for derived indexes only."""
    pins: list[ArtifactPin | ArtifactRef] = list(getattr(artifact, "pins", ()))
    if not by_name and getattr(artifact, "artifact_format", "") not in {
        "cruxible-procedure-v3",
        "cruxible-query-definition-v3",
        "cruxible-trigger-v2",
        "cruxible-line-v8",
        "cruxible-provider-v6",
        "cruxible-provider-identity-v3",
        "cruxible-capture-contract-v3",
    }:
        return tuple(pins)

    if by_name and getattr(getattr(artifact, "identity", None), "kind", "").lower() in {
        "procedure",
        "querydefinition",
        "trigger",
        "line",
        "capturecontract",
        "provider",
    }:
        pins = [
            pin
            if pin.target.kind in EXACT_REGISTRY_TARGET_KINDS
            else ArtifactRef(role=pin.role, target=pin.target)
            for pin in pins
        ]

    def visit(item: object, field: str = "") -> Iterator[ArtifactRef]:
        if isinstance(item, ArtifactNameRef):
            yield ArtifactRef(role=FIELD_ROLES[field], target=item.target)
        elif isinstance(item, ArtifactRef):
            yield item
        elif isinstance(item, ArtifactPin):
            return
        elif isinstance(item, BaseModel):
            # Retained source describes provenance. Actual graph fields already
            # contain their governed dependencies.
            for name in type(item).model_fields:
                if name not in {"source", "owned_contracts", "pins", "bindings"}:
                    yield from visit(getattr(item, name), name)
        elif isinstance(item, tuple | list):
            for member in item:
                yield from visit(member, field)

    pins.extend(visit(artifact))
    kind = getattr(getattr(artifact, "identity", None), "kind", None)
    if kind == "Line":
        # A Line pins its exact Procedure version and each bound build; the
        # dependency index follows them by identity (a build identity names one
        # build) so a retire or move finds the Lines that bind it.
        pins.append(ArtifactRef(role="procedure", target=artifact.procedure.target))
        pins.extend(
            ArtifactRef(role="provider", target=binding.build.target)
            for binding in getattr(artifact, "bindings", {}).values()
        )
    if kind == "Procedure":
        for slot in getattr(getattr(artifact, "definition", None), "slots", {}).values():
            for ref in (slot.in_, slot.out):
                pins.append(
                    ArtifactRef(
                        role=CONTRACT_PIN_ROLE, target=contract_identity(ref.name, ref.version)
                    )
                )
    if kind == "Provider":
        for operation in getattr(artifact, "operations", ()):
            for ref in operation.contract_refs():
                pins.append(
                    ArtifactRef(
                        role=CONTRACT_PIN_ROLE, target=contract_identity(ref.name, ref.version)
                    )
                )
    if getattr(artifact, "identity", None) is not None:
        if artifact.identity.kind == "QueryDefinition":
            if not getattr(artifact, "pins", ()):
                pins.extend(
                    ArtifactRef(
                        role="claim-type", target=ArtifactIdentity(kind="ClaimType", name=name)
                    )
                    for name in artifact.referenced_predicates
                )
            # parameter_contract and row_contract are ContractRef, not
            # ArtifactRef/ArtifactPin/ArtifactNameRef, so the generic walk
            # above never finds them; name them explicitly here.
            for contract_ref in (
                getattr(artifact, "parameter_contract", None),
                getattr(artifact, "row_contract", None),
            ):
                if contract_ref is not None:
                    pins.append(
                        ArtifactRef(
                            role=CONTRACT_PIN_ROLE,
                            target=contract_identity(contract_ref.name, contract_ref.version),
                        )
                    )
    identity = getattr(artifact, "identity", None)
    if identity is not None and identity.kind in {"Trigger", "Line"}:
        from cruxible_client.contracts.triggers import schedule_capture_selector

        selector = (
            schedule_capture_selector(artifact.schedule)
            if identity.kind == "Trigger"
            else artifact.trigger_event
        )
        if selector is not None and not any(
            p.target == selector.capture_contract_identity for p in pins
        ):
            pins.append(
                ArtifactRef(role="capture-contract", target=selector.capture_contract_identity)
            )
    unique: dict[tuple[str, str], ArtifactPin | ArtifactRef] = {}
    for pin in pins:
        key = pin.role, pin.target.qualified
        # An exact ArtifactPin, once recorded for a (role, target) key, is
        # never displaced by a later digest-less ArtifactRef for the same
        # key (the by-identity dependency index appends one unconditionally
        # for a Line's procedure/provider pins); only a key with nothing yet,
        # or only a Ref so far, takes the new pin.
        if key not in unique or isinstance(unique[key], ArtifactRef):
            unique[key] = pin
    return tuple(unique.values())
