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
    "sha256:d65318716a291de9c50347a0cfbb5e07695ec6ca3023f5e7beaa91eb3aa8ea0d"
)

CLAIM_ATTESTATION_WIRE_MODEL_NAMES = (
    ("claim_attestations", "ClaimAttestationAppendRequestV1"),
    ("claim_attestations", "ClaimAttestationAppendResultV1"),
    ("claim_attestations", "ClaimAttestationCaptureReferenceV1"),
    ("claim_attestations", "ClaimAttestationResolvedArtifactV1"),
    ("claim_attestations", "ClaimAttestationStatementV2"),
    ("claim_attestations", "ClaimAttestationV2"),
    ("claim_attestations", "PreparedClaimAttestationRequestV1"),
    ("claim_attestations", "VerifiedClaimAttestationV2"),
    ("claim_attestation_store", "ClaimAttestationAcceleratorV1"),
    ("claim_attestation_store", "ClaimAttestationEventPayloadV1"),
    ("claim_attestation_store", "ClaimAttestationEventV1"),
    ("claim_attestation_store", "ClaimAttestationHeadMapEntryV1"),
    ("claim_attestation_store", "ClaimAttestationHeadMapNodeV1"),
    ("claim_attestation_store", "ClaimAttestationOutstandingMembershipV1"),
    ("claim_attestation_store", "ClaimAttestationPartitionGenesisV1"),
    ("claim_attestation_store", "ClaimAttestationPartitionHeadV1"),
    ("claim_attestation_store", "ClaimAttestationPublishedPointerV1"),
    ("claim_attestation_store", "ClaimAttestationPublishedRootV1"),
    ("claim_attestation_store", "ClaimAttestationStoreManifestV1"),
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
