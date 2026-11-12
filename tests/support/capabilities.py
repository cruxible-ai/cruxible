"""Capabilities for test fixtures: a Procedure's slot names one, so a fixture authors one.

A slot is ``{capability, may_truncate}``. A fixture that used to declare a slot
by ``{in, out, effect}`` now authors the capability with those Contracts and
effect (:func:`capability`, accepted with :func:`capability_files` or
:func:`accept_capabilities`) and names it in the slot (:func:`slot`).

The workspace read core's builtin serves is the capability ``workspace.file``
over the genesis ``workspace.file.request@1 -> workspace.file.result@1`` (pure)
(:func:`workspace_file_capability`); until the base kit slice lands it is
authored per test world like any other.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cruxible_client.contracts.capabilities import (
    Capability,
    capability_digest,
    capability_identity,
    capability_path,
    render_capability,
)
from cruxible_client.contracts.shapes import (
    Contract,
    ContractRef,
    contract_digest,
    contract_path,
    parse_contract,
    parse_contract_ref,
)

#: The capability name core's builtin serves for one-file reads.
WORKSPACE_FILE = "workspace.file"

#: Every capability a fixture declared, by identity: what fixture binding resolves.
_DECLARED: dict[str, Capability] = {}


def _pinned(
    value: Contract | ContractRef | str, contracts: Mapping[str, Contract]
) -> dict[str, Any]:
    if isinstance(value, ContractRef):
        return value.model_dump(mode="json")
    if isinstance(value, Contract):
        return {
            "name": value.name,
            "version": value.version,
            "digest": contract_digest(value).tagged,
        }
    ref = parse_contract_ref(value)
    found = contracts.get(ref.spelling)
    return {
        "name": ref.name,
        "version": ref.version,
        "digest": ref.digest if found is None else contract_digest(found).tagged,
    }


def capability(
    name: str,
    *,
    in_: Contract | ContractRef | str,
    out: Contract | ContractRef | str,
    effect: str,
    description: str | None = None,
    contracts: Mapping[str, Contract] | None = None,
) -> Capability:
    """Version 1 of capability ``name``, pinning ``in_`` and ``out`` by digest.

    ``in_``/``out`` are Contracts, or ``name@N`` spellings resolved through
    ``contracts`` (``name@N`` -> Contract). The capability is recorded so
    :func:`declared_capability` (and fixture binding) resolves it.
    """

    found = Capability.model_validate(
        {
            "name": name,
            "version": 1,
            "description": description or f"Test capability {name}.",
            "effect": effect,
            "in": _pinned(in_, contracts or {}),
            "out": _pinned(out, contracts or {}),
        }
    )
    if found.pinned:
        _DECLARED[found.identity.qualified] = found
    return found


def signature_capability(signature: Any, *, name: str) -> Capability:
    """Capability ``name`` over an exact operation signature (pinned ``in``/``out``)."""

    return capability(name, in_=signature.in_, out=signature.out, effect=signature.effect)


def tree_capability(
    tree: Mapping[str, bytes], name: str, *, in_: str, out: str, effect: str
) -> Capability:
    """Capability ``name`` over two Contracts read from ``tree`` (``name@N`` each)."""

    contracts: dict[str, Contract] = {}
    for spelling in (in_, out):
        ref = parse_contract_ref(spelling)
        path = contract_path(ref.name, ref.version)
        contracts[spelling] = parse_contract(tree[path], path=path)
    return capability(name, in_=in_, out=out, effect=effect, contracts=contracts)


def capability_files(*capabilities: Capability) -> dict[str, bytes]:
    """The tree files of ``capabilities``, for an accept beside their Contracts."""

    return {
        capability_path(item.name, item.version): render_capability(item) for item in capabilities
    }


def workspace_file_capability() -> Capability:
    """``workspace.file@1`` over the genesis workspace.file Contracts (pure)."""

    from cruxible_client.contracts.workspace_file import workspace_file_contracts

    _source, _content, request, result = workspace_file_contracts()
    assert isinstance(request, Contract) and isinstance(result, Contract)
    return capability(
        WORKSPACE_FILE,
        in_=request,
        out=result,
        effect="pure",
        description="Read one workspace file; core's builtin serves it.",
    )


def accept_capabilities(
    instance: Any, *capabilities: Capability, name: str = "capabilities"
) -> None:
    """Accept ``capabilities`` over the head (their Contracts must be accepted)."""

    from tests.core_support._identity_support import accept_tree

    accept_tree(instance, capability_files(*capabilities), name=name)


def accept_head_capabilities(
    instance: Any, *specs: tuple[str, str, str, str], name: str = "capabilities"
) -> tuple[Capability, ...]:
    """Accept one capability per ``(name, in, out, effect)`` over Contracts at the head."""

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    found = tuple(
        tree_capability(tree, capability_name, in_=in_, out=out, effect=effect)
        for capability_name, in_, out, effect in specs
    )
    accept_capabilities(instance, *found, name=name)
    return found


def accept_workspace_file_capability(instance: Any) -> Capability:
    """Accept ``workspace.file@1`` on an instance whose genesis holds its Contracts."""

    found = workspace_file_capability()
    accept_capabilities(instance, found, name="workspace-file-capability")
    return found


def slot(value: Capability | str, *, may_truncate: bool = False) -> dict[str, Any]:
    """The authored slot naming ``value`` (a capability or ``name@N``)."""

    spelling = value if isinstance(value, str) else value.ref.spelling
    return {"capability": spelling, **({"may_truncate": True} if may_truncate else {})}


def pinned_slot(value: Capability, *, may_truncate: bool = False) -> dict[str, Any]:
    """The stored slot naming ``value`` at its exact digest."""

    return {
        "capability": {
            "name": value.name,
            "version": value.version,
            "digest": capability_digest(value).tagged,
        },
        "may_truncate": may_truncate,
    }


def declared_capability(identity: Any) -> tuple[Capability, str] | None:
    """A declared fixture capability and its digest, for fixture binding lookups."""

    qualified = getattr(identity, "qualified", identity)
    found = _DECLARED.get(qualified)
    return None if found is None else (found, capability_digest(found).tagged)


def declared_capability_lookup() -> Any:
    """A ``CapabilityLookup`` over every capability a fixture declared."""

    return capability_lookup(*_DECLARED.values())


def capability_lookup(*capabilities: Capability) -> Any:
    """A ``CapabilityLookup`` over ``capabilities`` (exact digest when pinned)."""

    by_identity = {item.identity.qualified: item for item in capabilities}

    def resolve(ref: Any) -> Capability | None:
        found = by_identity.get(capability_identity(ref.name, ref.version).qualified)
        if found is None or (
            ref.digest is not None and capability_digest(found).tagged != ref.digest
        ):
            return None
        return found

    return resolve


__all__ = [
    "WORKSPACE_FILE",
    "accept_capabilities",
    "accept_head_capabilities",
    "accept_workspace_file_capability",
    "capability",
    "capability_files",
    "capability_lookup",
    "declared_capability",
    "declared_capability_lookup",
    "pinned_slot",
    "signature_capability",
    "slot",
    "tree_capability",
    "workspace_file_capability",
]
