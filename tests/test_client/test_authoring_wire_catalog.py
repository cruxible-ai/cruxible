"""Freeze the complete nested authoring wire model closure."""

from __future__ import annotations

from cruxible_client.contracts.authoring.wire_catalog import (
    AUTHORING_WIRE_CONTRACT_CATALOG_DIGEST,
    AUTHORING_WIRE_MODEL_NAMES,
    authoring_wire_contract_catalog_digest,
    discovered_authoring_wire_model_names,
)


def test_authoring_wire_catalog_enumerates_every_public_model() -> None:
    assert discovered_authoring_wire_model_names() == AUTHORING_WIRE_MODEL_NAMES


def test_authoring_wire_catalog_digest_is_frozen() -> None:
    assert authoring_wire_contract_catalog_digest() == AUTHORING_WIRE_CONTRACT_CATALOG_DIGEST
