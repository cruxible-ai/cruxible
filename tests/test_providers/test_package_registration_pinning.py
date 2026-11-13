"""An install pins each operation Contract to the package's own carried copy, never the instance's.

The registration law (schema 3) is judged over what the package carries: a pin
that does not resolve inside ``declares``, a carried Contract no operation
reaches, a nested Contract a copy names without its digest, and an operation
serving a ``workspace.`` capability each refuse typed, before anything is
proposed.
"""

from __future__ import annotations

from typing import Any

import pytest

from cruxible_client.contracts.provider_registration import (
    PROVIDER_CAPABILITY_RESERVED,
    PROVIDER_DECLARED_CONTRACT_MISSING,
    PROVIDER_DECLARED_CONTRACT_UNPINNED,
    PROVIDER_DECLARED_UNREACHED,
    ProviderDeclares,
)
from cruxible_client.contracts.providers import (
    ProviderDistributionRef,
    ProviderImplementationManifest,
    ProviderRuntimeManifest,
    operation_law_problem,
)
from cruxible_client.contracts.shapes import Contract, contract_digest
from cruxible_core.errors import RequestRefusedError
from cruxible_core.providers.package_registration import PackageRegistrationDocumentV1
from tests.support.declared_operations import declared_operation, registration_document
from tests.support.operation_world import REFUSAL, VALUE, operation, operation_build

_MANIFEST = ProviderRuntimeManifest(
    provider_id="demo-provider",
    distribution=ProviderDistributionRef(name="cruxible-provider-demo", version="1.0.0"),
    supported_protocol_majors=(2,),
    implementations=(
        ProviderImplementationManifest(
            entrypoint="demo.runtime:Provider", backends=("local_env",), deterministic=True
        ),
    ),
)
_OPERATIONS = (
    operation("compute", refusals={"too_big": False}),
    operation("measure", refusals={"too_big": False}),
)


def _document(*contracts: Contract, operations: Any = _OPERATIONS) -> PackageRegistrationDocumentV1:
    return PackageRegistrationDocumentV1(
        schema_version=3,
        manifest=_MANIFEST,
        operations=operations,
        declares=ProviderDeclares(contracts=contracts),
    )


def _wrapping(name: str, in_: str, *, serves: tuple[str, ...] | None = None) -> Any:
    return declared_operation(
        {
            "name": name,
            **({} if serves is None else {"serves": list(serves)}),
            "in": in_,
            "out": "test.value@1",
            "effect": "pure",
            "entrypoint": "demo.runtime:Provider",
        }
    )


def test_every_operation_pins_its_carried_copies() -> None:
    pinned = _document(REFUSAL, VALUE).pinned_operations()

    assert [item.name for item in pinned] == ["compute", "measure"]
    assert all(item.in_.digest == contract_digest(VALUE).tagged for item in pinned)
    assert all(
        refusal.contract.digest == contract_digest(REFUSAL).tagged
        for item in pinned
        for refusal in item.refusals.values()
    )


def test_a_contract_the_package_does_not_carry_refuses_naming_it() -> None:
    with pytest.raises(RequestRefusedError) as refused:
        _document(VALUE).pinned_operations()

    assert refused.value.error_code == PROVIDER_DECLARED_CONTRACT_MISSING
    assert "test.refusal@1" in str(refused.value)
    assert "cruxible-provider-demo cannot be installed" in str(refused.value)


def test_a_carried_contract_no_operation_reaches_refuses() -> None:
    extra = Contract.model_validate({"name": "test.extra", "version": 1, "fields": {}})

    with pytest.raises(RequestRefusedError) as refused:
        _document(extra, REFUSAL, VALUE).pinned_operations()

    assert refused.value.error_code == PROVIDER_DECLARED_UNREACHED
    assert "test.extra@1" in str(refused.value)


def test_a_carried_copy_naming_a_nested_contract_without_its_digest_refuses() -> None:
    outer = Contract.model_validate(
        {"name": "test.outer", "version": 1, "fields": {"inner": {"type": "test.value@1"}}}
    )

    with pytest.raises(RequestRefusedError) as refused:
        _document(outer, VALUE, operations=(_wrapping("wrap", "test.outer@1"),)).pinned_operations()

    assert refused.value.error_code == PROVIDER_DECLARED_CONTRACT_UNPINNED


def test_an_operation_serving_a_workspace_capability_refuses_reserved() -> None:
    served = (_wrapping("read", "test.value@1", serves=("workspace.file",)),)

    with pytest.raises(RequestRefusedError) as refused:
        registration_document(_MANIFEST, served, (VALUE,)).pinned_operations()

    assert refused.value.error_code == PROVIDER_CAPABILITY_RESERVED
    assert "`workspace.file` is served by core." in str(refused.value)


def test_the_build_carries_its_declares_block_and_its_law_reads_only_it() -> None:
    build = operation_build()

    assert build.declares is not None
    assert [item.ref.spelling for item in build.declares.contracts] == ["test.value@1"]
    assert operation_law_problem(build) is None
    # A build whose operation names a Contract its copies lack is refused at
    # acceptance too, whatever the instance holds.
    stripped = build.model_copy(update={"declares": ProviderDeclares(contracts=(REFUSAL,))})
    problem = operation_law_problem(stripped)
    assert problem is not None and problem[0] == PROVIDER_DECLARED_CONTRACT_MISSING
