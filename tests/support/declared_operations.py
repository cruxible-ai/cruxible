"""Schema-3 operations and carried shapes for tests that build provider builds by hand.

A provider build carries every Contract its operations reach (``declares``);
these helpers build the operation with its schema-3 metadata (a description, a
source and the capability names it serves) and the exact carried closure out of
a pool of known Contracts, so a test states only what it is about.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from cruxible_client.contracts.capture_layouts import DeclaredCaptureKind
from cruxible_client.contracts.provider_registration import DeclaredOperation, ProviderDeclares
from cruxible_client.contracts.providers import ProviderRuntimeManifest
from cruxible_client.contracts.shapes import (
    ANY_CAPTURE_KIND,
    Contract,
    all_field_refs,
)

#: The source every hand-built test operation names.
TEST_SOURCE = {"path": "tests/support/declared_operations.py"}


def declared_operation(data: Mapping[str, Any]) -> DeclaredOperation:
    """One operation from its schema-2 style fields, schema-3 metadata defaulted.

    ``description`` defaults to a sentence naming the operation, ``source`` to
    this module, and ``serves`` to the operation's own name.
    """

    return DeclaredOperation.model_validate(
        {
            "description": f"Test operation {data['name']}.",
            "source": TEST_SOURCE,
            "serves": [data["name"]],
            **data,
        }
    )


def carried_declares(
    operations: Iterable[DeclaredOperation],
    contracts: Iterable[Contract],
    *,
    capture_kinds: Iterable[DeclaredCaptureKind] = (),
) -> ProviderDeclares:
    """Exactly the Contracts (and named CaptureKinds) ``operations`` reach, from ``contracts``.

    Every Contract in the pool pins its nested Contracts by digest, as a
    carried copy must. A ``capture<K>`` kind the reached Contracts name is
    carried as given in ``capture_kinds``, or as a raw kind (name only).
    """

    pool = {(item.name, item.version): item for item in contracts}
    kinds = {item.name: item for item in capture_kinds}
    reached: dict[tuple[str, int], Contract] = {}
    named: dict[str, DeclaredCaptureKind] = {}
    pending = [ref for operation in operations for ref in operation.contract_refs()]
    while pending:
        ref = pending.pop()
        key = (ref.name, ref.version)
        if key in reached:
            continue
        contract = pool[key]
        reached[key] = contract
        pending.extend(all_field_refs(contract))
        for declared in contract.fields.values():
            for kind in _capture_kinds(declared.type):
                named[kind] = kinds.get(kind) or DeclaredCaptureKind(name=kind)
    return ProviderDeclares(
        contracts=tuple(
            reached[key] for key in sorted(reached, key=lambda key: (key[0].encode(), key[1]))
        ),
        capture_kinds=tuple(named[name] for name in sorted(named, key=str.encode)),
    )


def _capture_kinds(field_type: Any) -> tuple[str, ...]:
    from cruxible_client.contracts.capture_layouts import capture_kinds_of

    return tuple(kind for kind in capture_kinds_of(field_type) if kind != ANY_CAPTURE_KIND)


def registration_document(
    manifest: ProviderRuntimeManifest,
    operations: Iterable[DeclaredOperation | Mapping[str, Any]],
    contracts: Iterable[Contract],
    *,
    capture_kinds: Iterable[DeclaredCaptureKind] = (),
    runtime_requirements: tuple[Any, ...] = (),
) -> Any:
    """A schema-3 registration document whose ``declares`` is the operations' exact closure."""

    from cruxible_core.providers.package_registration import PackageRegistrationDocumentV1

    served = tuple(
        item if isinstance(item, DeclaredOperation) else declared_operation(item)
        for item in operations
    )
    return PackageRegistrationDocumentV1(
        schema_version=3,
        manifest=manifest,
        operations=served,
        declares=carried_declares(served, contracts, capture_kinds=capture_kinds),
        runtime_requirements=runtime_requirements,
    )


__all__ = [
    "TEST_SOURCE",
    "carried_declares",
    "declared_operation",
    "registration_document",
]
