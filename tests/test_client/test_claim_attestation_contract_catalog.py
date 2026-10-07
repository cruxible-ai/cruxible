"""The evidence-door wire has an independent, exhaustive frozen catalog."""

from __future__ import annotations

import inspect

from pydantic import BaseModel

from cruxible_client.contracts import claim_attestation_store, claim_attestations
from tests.support.authoring_wire_catalog import (
    AUTHORING_WIRE_CONTRACT_CATALOG_DIGEST,
    authoring_wire_contract_catalog_digest,
)
from tests.support.claim_attestation_wire_catalog import (
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
                    "ClaimAttestationV1",
                    "ClaimAttestationStatementV1",
                    "ClaimAttestationSourceRegistrationV1",
                }:
                    continue
                discovered.add((short_name, name))
        if short_name == "claim_attestations":
            discovered.update(
                {
                    (short_name, "PreparedClaimAttestationRequest"),
                    (short_name, "VerifiedClaimAttestation"),
                }
            )
    assert set(CLAIM_ATTESTATION_WIRE_MODEL_NAMES) == discovered
    assert (
        claim_attestation_wire_contract_catalog_digest()
        == CLAIM_ATTESTATION_WIRE_CONTRACT_CATALOG_DIGEST
    )


def test_authoring_wire_catalog_cross_check_tracks_the_current_successor() -> None:
    assert AUTHORING_WIRE_CONTRACT_CATALOG_DIGEST == (
        "sha256:f12754d6367a4a2469f8775abd3855fd8f58bbeb10cba9d547a7a244e8c4c91a"
    )
    assert authoring_wire_contract_catalog_digest() == AUTHORING_WIRE_CONTRACT_CATALOG_DIGEST
