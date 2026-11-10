"""Installing a provider whose Contracts are not accepted refuses once, naming the next step."""

from __future__ import annotations

from typing import Any

import pytest

from cruxible_client.contracts.providers import (
    ProviderDistributionRef,
    ProviderImplementationManifest,
    ProviderRuntimeManifest,
)
from cruxible_client.contracts.repairs import RepairOperation
from cruxible_client.contracts.shapes import contract_digest
from cruxible_core.errors import RequestRefusedError
from cruxible_core.providers.package_registration import PackageRegistrationDocumentV1
from tests.support.operation_world import REFUSAL, VALUE, operation


def _document() -> PackageRegistrationDocumentV1:
    manifest = ProviderRuntimeManifest(
        provider_id="demo-provider",
        distribution=ProviderDistributionRef(name="cruxible-provider-demo", version="1.0.0"),
        supported_protocol_majors=(2,),
        implementations=(
            ProviderImplementationManifest(
                entrypoint="demo.runtime:Provider", backends=("local_env",), deterministic=True
            ),
        ),
    )
    return PackageRegistrationDocumentV1(
        schema_version=2,
        manifest=manifest,
        operations=(
            operation("compute", refusals={"too_big": False}),
            operation("measure", refusals={"too_big": False}),
        ),
    )


def _accepting(*accepted: Any) -> Any:
    digests = {(item.name, item.version): contract_digest(item).tagged for item in accepted}
    return lambda ref: digests.get((ref.name, ref.version))


def test_no_accepted_contracts_refuses_once_counting_them_and_naming_kit_add() -> None:
    with pytest.raises(RequestRefusedError) as refused:
        _document().pinned_operations(_accepting())

    error = refused.value
    assert error.error_code == "cruxible.provider.operation_contract_unresolved"
    message = str(error)
    assert "cruxible-provider-demo cannot be installed here yet" in message
    assert "2 of the 2 Contract versions" in message
    assert "(first: test.refusal@1)" in message
    assert "cruxible kit add" in message
    assert error.repair == RepairOperation(operation="cruxible.kit.add")


def test_some_accepted_counts_only_the_missing_ones() -> None:
    with pytest.raises(RequestRefusedError) as refused:
        _document().pinned_operations(_accepting(VALUE))

    assert "1 of the 2 Contract versions" in str(refused.value)
    assert "(first: test.refusal@1)" in str(refused.value)


def test_all_accepted_pins_every_operation() -> None:
    pinned = _document().pinned_operations(_accepting(VALUE, REFUSAL))

    assert [item.name for item in pinned] == ["compute", "measure"]
    assert all(item.in_.digest == contract_digest(VALUE).tagged for item in pinned)
    assert all(
        refusal.contract.digest == contract_digest(REFUSAL).tagged
        for item in pinned
        for refusal in item.refusals.values()
    )
