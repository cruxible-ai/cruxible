"""Lower one authored Contract version onto its immutable artifact bytes.

The author names nested Contracts ``name@N``; lowering pins each to the digest
of the version accepted (or staged in the same change set), and names version
N-1 as the predecessor of every later version. Whether the result is admissible
is the Contract acceptance law's to judge against the candidate tree.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from cruxible_client.contracts.authoring.models import ContractAuthoringPayload
from cruxible_client.contracts.shapes import (
    ContractFormatError,
    ContractRef,
    ContractTypeRefused,
    contract_digest,
    contract_path,
    parse_contract,
    pin_contract_refs,
    render_contract,
    validate_contract,
)


@dataclass(frozen=True)
class ContractLoweringRefusal(Exception):
    """One typed authoring refusal; ``lowering`` re-raises it with its repair."""

    code: str
    message: str
    repair: str


def _accepted_digest(tree: Mapping[str, bytes], name: str, version: int) -> str | None:
    path = contract_path(name, version)
    content = tree.get(path)
    if content is None:
        return None
    return contract_digest(parse_contract(content, path=path)).tagged


def render_contract_member(
    payload: ContractAuthoringPayload, *, tree: Mapping[str, bytes]
) -> tuple[str, bytes, str]:
    """``(path, bytes, digest)`` of one Contract version, pinned against ``tree``."""

    def pin(ref: ContractRef) -> ContractRef:
        digest = _accepted_digest(tree, ref.name, ref.version)
        if digest is None:
            raise ContractLoweringRefusal(
                "cruxible.contract.pin_unresolved",
                f"{ref.spelling} is not an accepted Contract.",
                f"Accept {ref.spelling} first, or in the same change set.",
            )
        if ref.digest is not None and ref.digest != digest:
            raise ContractLoweringRefusal(
                "cruxible.contract.pin_digest_mismatch",
                f"{ref.spelling} is accepted at {digest}, not {ref.digest}.",
                f"Write {ref.spelling} without a digest; lowering pins the accepted version.",
            )
        return ref.model_copy(update={"digest": digest})

    predecessor: str | None = None
    if payload.version > 1:
        predecessor = _accepted_digest(tree, payload.name, payload.version - 1)
        if predecessor is None:
            raise ContractLoweringRefusal(
                "cruxible.contract.predecessor_missing",
                f"{payload.name}@{payload.version} succeeds version {payload.version - 1}, "
                "which is not accepted.",
                f"Author {payload.name}@{payload.version - 1} first.",
            )
    try:
        declared = validate_contract(
            {
                "name": payload.name,
                "version": payload.version,
                "fields": {
                    name: item.model_dump(mode="json") for name, item in payload.fields.items()
                },
                **({"description": payload.description} if payload.description else {}),
                **({"predecessor_digest": predecessor} if predecessor else {}),
            }
        )
    except ContractTypeRefused as exc:
        raise ContractLoweringRefusal(
            "cruxible.contract.contract_type_refused",
            str(exc),
            "Declare a typed shape: no json or untyped map, and give every list max_items.",
        ) from exc
    except ContractFormatError as exc:
        raise ContractLoweringRefusal(
            "cruxible.contract.contract_invalid",
            str(exc),
            "Repair the Contract's name, version and field types.",
        ) from exc
    contract = pin_contract_refs(declared, pin)
    path = contract_path(contract.name, contract.version)
    content = render_contract(contract)
    previous = tree.get(path)
    if previous is not None and previous != content:
        raise ContractLoweringRefusal(
            "cruxible.contract.immutable",
            f"{contract.identity.qualified} is accepted and immutable.",
            f"Author {contract.name}@{contract.version + 1} instead.",
        )
    return path, content, contract_digest(contract).tagged


__all__ = ["ContractLoweringRefusal", "render_contract_member"]
