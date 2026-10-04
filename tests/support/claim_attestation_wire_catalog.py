"""Frozen catalog of the public/deep Claim-attestation evidence-door wire."""

from __future__ import annotations

import hashlib
from typing import Any, cast

from pydantic import BaseModel

from cruxible_client.contracts import claim_attestation_store as store_models
from cruxible_client.contracts import claim_attestations as public_models
from cruxible_client.contracts.primitives import canonical_json

CLAIM_ATTESTATION_WIRE_CATALOG_VERSION = 1
# Pinned from the real Pydantic models in the authorized atomic re-pin commit.
CLAIM_ATTESTATION_WIRE_CONTRACT_CATALOG_DIGEST = (
    "sha256:623a8a637bd412bf6be87910d6976556041ef61d7f7efc1768bc3bcca4f0144e"
)

CLAIM_ATTESTATION_WIRE_MODEL_NAMES = (
    ("claim_attestations", "ClaimAttestationAppendRequest"),
    ("claim_attestations", "ClaimAttestationAppendResult"),
    ("claim_attestations", "ClaimAttestationCaptureReference"),
    ("claim_attestations", "ClaimAttestationResolvedArtifact"),
    ("claim_attestations", "ClaimAttestationStatement"),
    ("claim_attestations", "ClaimAttestation"),
    ("claim_attestations", "PreparedClaimAttestationRequest"),
    ("claim_attestations", "VerifiedClaimAttestation"),
    ("claim_attestation_store", "ClaimAttestationAccelerator"),
    ("claim_attestation_store", "ClaimAttestationEventPayload"),
    ("claim_attestation_store", "ClaimAttestationEvent"),
    ("claim_attestation_store", "ClaimAttestationHeadMapEntry"),
    ("claim_attestation_store", "ClaimAttestationHeadMapNode"),
    ("claim_attestation_store", "ClaimAttestationOutstandingMembership"),
    ("claim_attestation_store", "ClaimAttestationPartitionGenesis"),
    ("claim_attestation_store", "ClaimAttestationPartitionHead"),
    ("claim_attestation_store", "ClaimAttestationPublishedPointer"),
    ("claim_attestation_store", "ClaimAttestationPublishedRoot"),
    ("claim_attestation_store", "ClaimAttestationStoreManifest"),
)


def generate_claim_attestation_wire_contract_catalog() -> dict[str, Any]:
    schemas: dict[str, Any] = {}
    modules = {
        "claim_attestations": public_models,
        "claim_attestation_store": store_models,
    }
    for module_name, name in CLAIM_ATTESTATION_WIRE_MODEL_NAMES:
        model = cast(type[BaseModel], getattr(modules[module_name], name))
        model.model_rebuild()
        schemas[f"{module_name}.{name}"] = model.model_json_schema(ref_template="#/$defs/{model}")
    return {
        "catalog_version": CLAIM_ATTESTATION_WIRE_CATALOG_VERSION,
        "modules": tuple(module.__name__ for module in modules.values()),
        "models": schemas,
    }


def claim_attestation_wire_contract_catalog_digest() -> str:
    content = canonical_json(generate_claim_attestation_wire_contract_catalog()).encode("utf-8")
    return "sha256:" + hashlib.sha256(content).hexdigest()


__all__ = [
    "CLAIM_ATTESTATION_WIRE_CATALOG_VERSION",
    "CLAIM_ATTESTATION_WIRE_CONTRACT_CATALOG_DIGEST",
    "CLAIM_ATTESTATION_WIRE_MODEL_NAMES",
    "claim_attestation_wire_contract_catalog_digest",
    "generate_claim_attestation_wire_contract_catalog",
]
