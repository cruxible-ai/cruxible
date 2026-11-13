"""Capabilities for test fixtures: a Procedure's slot names one, so a fixture authors one.

A slot is ``{capability, may_truncate}``. A fixture that used to declare a slot
by ``{in, out, effect}`` now authors the capability with those Contracts and
effect (:func:`capability`, accepted with :func:`capability_files` or
:func:`accept_capabilities`) and names it in the slot (:func:`slot`).

Base-kit capabilities (``workspace.file``, ``web.fetch``, ``web.fetch.pages``)
go through one door, :func:`ensure_capabilities`: a capability the instance
already holds is used as it is held, never re-authored. Once the base kit is
installed at creation, every new instance holds them with the bundled bytes and
nothing is authored. Before it, the instance holds none of them and they are
authored over the Contracts the instance does hold: the wave 0 example bytes
(``tests/pilot_contract_batch/examples/base``) cannot be accepted there, because
their ``workspace.file.request``/``.result`` collide with the genesis seeds of the
same name and version, and their ``web.fetch.result`` differs from the web
package's. Nothing here authors a base-kit capability in a static file set.
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


#: The genesis Contracts ``workspace.file@1`` is authored over before the base kit lands.
WORKSPACE_FILE_SPEC = (
    WORKSPACE_FILE,
    "workspace.file.request@1",
    "workspace.file.result@1",
    "pure",
)


def held_capability(tree: Mapping[str, bytes], name: str, version: int = 1) -> Capability | None:
    """The capability version ``tree`` holds, or ``None``."""

    from cruxible_client.contracts.capabilities import parse_capability

    path = capability_path(name, version)
    content = tree.get(path)
    if content is None:
        return None
    found = parse_capability(content, path=path)
    _DECLARED[found.identity.qualified] = found
    return found


def head_tree(instance: Any) -> dict[str, bytes]:
    return dict(instance.tree_at(instance.accepted_coordinate().git_oid))


def workspace_file_capability(tree: Mapping[str, bytes]) -> Capability:
    """``workspace.file@1`` as ``tree`` holds it, else as authored over its Contracts."""

    found = held_capability(tree, WORKSPACE_FILE)
    if found is not None:
        return found
    _name, in_, out, effect = WORKSPACE_FILE_SPEC
    return tree_capability(tree, WORKSPACE_FILE, in_=in_, out=out, effect=effect)


def accept_capabilities(
    instance: Any, *capabilities: Capability, name: str = "capabilities"
) -> None:
    """Accept ``capabilities`` over the head (their Contracts must be accepted)."""

    from tests.core_support._identity_support import accept_tree

    accept_tree(instance, capability_files(*capabilities), name=name)


def missing_capability_files(
    tree: Mapping[str, bytes], *specs: tuple[str, str, str, str]
) -> dict[str, bytes]:
    """The files of each ``(name, in, out, effect)`` capability ``tree`` does not hold yet,
    authored over the Contracts it holds; a held one is left as it is."""

    return capability_files(
        *(
            tree_capability(tree, capability_name, in_=in_, out=out, effect=effect)
            for capability_name, in_, out, effect in specs
            if held_capability(tree, capability_name) is None
        )
    )


def ensure_capabilities(
    instance: Any, *specs: tuple[str, str, str, str], name: str = "capabilities"
) -> tuple[Capability, ...]:
    """Each ``(name, in, out, effect)`` capability on the instance: as held, or authored
    over the Contracts at the head and accepted."""

    files = missing_capability_files(head_tree(instance), *specs)
    if files:
        from tests.core_support._identity_support import accept_tree

        accept_tree(instance, files, name=name)
    tree = head_tree(instance)
    return tuple(held_capability(tree, spec[0]) for spec in specs)  # type: ignore[misc]


def ensure_workspace_file_capability(instance: Any) -> Capability:
    """``workspace.file@1`` on the instance: as held (the base kit), or authored."""

    (found,) = ensure_capabilities(instance, WORKSPACE_FILE_SPEC, name="workspace-file-capability")
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
    "WORKSPACE_FILE_SPEC",
    "accept_capabilities",
    "ensure_capabilities",
    "ensure_workspace_file_capability",
    "head_tree",
    "held_capability",
    "missing_capability_files",
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
