"""The base kit and the pinned providers carry the same bytes for each shared Contract.

A provider's registration carries the Contracts its operations reach. Where the
base kit holds a Contract at the same versioned path, installing the provider
beside the base kit needs the two to be the same Contract: a reworded base
Contract makes the install refuse (``cruxible.change_set.incomplete_closure``),
and so does ``kit add`` of any kit that bundles that provider. A versioned
Contract is changed by publishing a new version, never by editing one a provider
already carries.

Runs against the checkout ``CRUXIBLE_PROVIDERS_CHECKOUT`` names (the CI commit),
and skips without one, as the other provider tests do.
"""

from __future__ import annotations

import json

import pytest

from cruxible_client.contracts.provider_registration import PackageRegistration
from cruxible_client.contracts.shapes import Contract, contract_digest, contract_path
from cruxible_core.kits.base_kit import base_kit_directory
from tests.support.provider_checkout import provider_checkout_path


def test_every_contract_a_provider_carries_matches_the_base_kit_at_its_path() -> None:
    checkout = provider_checkout_path()
    if checkout is None:
        pytest.skip("set CRUXIBLE_PROVIDERS_CHECKOUT to compare its Contracts with the base kit")
    artifacts = base_kit_directory() / "artifacts"
    shared = 0
    mismatched: list[str] = []
    for registration_path in sorted(checkout.glob("packages/*/src/*/registration.json")):
        document = json.loads(registration_path.read_text(encoding="utf-8"))
        if document.get("schema_version") != 3:
            continue  # carries no Contracts
        registration = PackageRegistration.model_validate(document)
        package = registration_path.relative_to(checkout).parts[1]
        for carried in registration.declares.contracts:
            path = contract_path(carried.name, carried.version)
            base_file = artifacts / path
            if not base_file.is_file():
                continue
            shared += 1
            base = Contract.model_validate_json(base_file.read_bytes())
            if contract_digest(base) != contract_digest(carried):
                mismatched.append(f"{path} (carried by {package})")
    assert not mismatched, (
        "the base kit's bytes differ from the Contract a pinned provider carries at: "
        + ", ".join(mismatched)
        + ". Restore the base kit's Contract, or publish the change as a new version."
    )
    assert shared, "no provider in the checkout carries a base kit Contract"
