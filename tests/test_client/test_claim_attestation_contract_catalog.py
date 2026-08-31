"""The evidence-door wire has an independent, exhaustive frozen catalog."""

from __future__ import annotations

import inspect

from pydantic import BaseModel

from cruxible_client.contracts import claim_attestation_store, claim_attestations
from cruxible_client.contracts.authoring.wire_catalog import (
    AUTHORING_WIRE_CONTRACT_CATALOG_DIGEST,
    authoring_wire_contract_catalog_digest,
)
from cruxible_client.contracts.claim_attestation_wire_catalog import (
    CLAIM_ATTESTATION_WIRE_CONTRACT_CATALOG_DIGEST,
    CLAIM_ATTESTATION_WIRE_MODEL_NAMES,
    claim_attestation_wire_contract_catalog_digest,
)


def test_claim_attestation_wire_catalog_is_current_and_exhaustive() -> None:
    discovered: set[tuple[str, str]] = set()
    for short_name, module in (
        ("claim_attestations", claim_attestations),
        ("claim_attestation_store", claim_attestation_store),
    ):
        for name, value in vars(module).items():
            if (
                name.startswith("ClaimAttestation")
                and inspect.isclass(value)
                and issubclass(value, BaseModel)
                and value.__module__ == module.__name__
            ):
                if short_name == "claim_attestations" and name in {
                    "ClaimAttestation",
                    "ClaimAttestationStatement",
                    "ClaimAttestationSourceRegistrationV1",
                }:
                    continue
                discovered.add((short_name, name))
        if short_name == "claim_attestations":
            discovered.update(
                {
                    (short_name, "PreparedClaimAttestationRequestV1"),
                    (short_name, "VerifiedClaimAttestationV2"),
                }
            )
    assert set(CLAIM_ATTESTATION_WIRE_MODEL_NAMES) == discovered
    assert (
        claim_attestation_wire_contract_catalog_digest()
        == CLAIM_ATTESTATION_WIRE_CONTRACT_CATALOG_DIGEST
    )


def test_attestation_door_does_not_move_the_authoring_wire_catalog() -> None:
    assert AUTHORING_WIRE_CONTRACT_CATALOG_DIGEST == (
        "sha256:dd8232f67da288502f2f5a52e2e16d320dcc9d496dd78e3938c42d56d2a207a1"
    )
    assert authoring_wire_contract_catalog_digest() == AUTHORING_WIRE_CONTRACT_CATALOG_DIGEST
