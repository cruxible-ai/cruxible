"""The one mapping between frozen stored identities and the names a reader sees.

Core's own reserved identities were minted under the ``playbill.`` spelling, and
digests and signatures cover them, so the stored and served values never change
(AGENTS.md "Frozen format tags"). Text a person or an agent reads says
``cruxible.`` instead. The mapping covers only the reserved families core
defines -- the own-words and direct-authoring CaptureContracts, their logical
sources and Capture components, the per-source ``playbill.foreign-source.``
contracts, and the Procedure taint labels -- never an arbitrary ``playbill.``
prefix, so a CaptureContract an author names ``playbill.x`` reads as itself.

- :func:`public_name` maps a stored name for display (prose, table cells).
  Typed JSON keeps the stored value.
- :func:`stored_contract_name` maps a CaptureContract name as typed back to the
  stored identity, so input accepts either spelling.
- :func:`public_name_map` is the same table as data, served once by
  ``server_info`` so a client applies it instead of copying the rule.
"""

from __future__ import annotations

import re
from functools import cache

from pydantic import BaseModel, ConfigDict

from cruxible_client.contracts.captures import capture_components

STORED_PREFIX = "playbill."
PUBLIC_PREFIX = "cruxible."

#: Derived Procedure taint labels (``cruxible_core.procedures.terminal_dependencies``).
#: Core owns their production; the reserved spellings are listed here so the
#: client can name them without importing core.
RESERVED_TAINT_LABELS: tuple[str, ...] = (
    "playbill.taint.accepted-state",
    "playbill.taint.conservative-default",
    "playbill.taint.omitted-optional",
    "playbill.taint.unpromoted-exhaust",
)

# A qualified reference keeps its kind (``CaptureContract:``, ``Contract:``) and
# maps only the name after it.
_QUALIFIER = re.compile(r"^([A-Z][A-Za-z]*:)(.*)$", re.DOTALL)


class NamePair(BaseModel):
    """One stored identity (or prefix) and the name a reader sees for it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stored: str
    public: str


class PublicNameMap(BaseModel):
    """The reserved-name display table as data.

    A client maps a stored name the way :func:`public_name` does: keep a leading
    ``Kind:`` qualifier, then an ``exact`` match, else the longest matching
    ``prefixes`` entry (replacing that prefix); any other name is shown as is.
    Read models keep stored names, and input accepts either spelling.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    exact: tuple[NamePair, ...]
    prefixes: tuple[NamePair, ...]


def _public_spelling(stored: str) -> str:
    return PUBLIC_PREFIX + stored.removeprefix(STORED_PREFIX)


@cache
def _reserved_exact() -> tuple[str, ...]:
    # Imported on first use: the contracts package imports this module, and the
    # Capture contracts import much of the package.
    from cruxible_client.contracts.captures import (
        _CAPTURE_COMPONENT_PIN_NAMES,
        COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
        COORDINATOR_SELF_SOURCE_CONTRACT_ID,
        COORDINATOR_SELF_SOURCE_IDENTITY,
        DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
        DIRECT_SELF_ASSERTED_CONTRACT_ID,
        DIRECT_SOURCE_IDENTITY,
    )

    names = {
        DIRECT_SELF_ASSERTED_CONTRACT_ID,
        DIRECT_SOURCE_IDENTITY,
        COORDINATOR_SELF_SOURCE_CONTRACT_ID,
        COORDINATOR_SELF_SOURCE_IDENTITY,
        *RESERVED_TAINT_LABELS,
        *(name for _role, name in _CAPTURE_COMPONENT_PIN_NAMES),
    }
    for contract in (
        DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
        COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    ):
        pins = (
            *capture_components(contract).coordinate_schema_pins,
            *capture_components(contract).selector_schema_pins,
            capture_components(contract).commitment_canonicalizer,
        )
        names.update(pin.target.name for pin in pins)
    return tuple(sorted(name for name in names if name.startswith(STORED_PREFIX)))


@cache
def _reserved_prefixes() -> tuple[str, ...]:
    from cruxible_client.contracts.captures import FOREIGN_SOURCE_CONTRACT_PREFIX

    return (FOREIGN_SOURCE_CONTRACT_PREFIX,)


@cache
def _exact_public() -> dict[str, str]:
    return {name: _public_spelling(name) for name in _reserved_exact()}


@cache
def _reserved_contracts() -> tuple[str, ...]:
    from cruxible_client.contracts.captures import (
        COORDINATOR_SELF_SOURCE_CONTRACT_ID,
        DIRECT_SELF_ASSERTED_CONTRACT_ID,
    )

    return (COORDINATOR_SELF_SOURCE_CONTRACT_ID, DIRECT_SELF_ASSERTED_CONTRACT_ID)


def _split(name: str) -> tuple[str, str]:
    match = _QUALIFIER.match(name)
    return ("", name) if match is None else (match.group(1), match.group(2))


def public_name(name: str) -> str:
    """A stored reserved identity as a reader sees it; any other name unchanged."""

    qualifier, bare = _split(name)
    mapped = _exact_public().get(bare)
    if mapped is not None:
        return qualifier + mapped
    for prefix in _reserved_prefixes():
        if bare.startswith(prefix) and len(bare) > len(prefix):
            return qualifier + _public_spelling(prefix) + bare[len(prefix) :]
    return name


def stored_contract_name(name: str) -> str:
    """A CaptureContract name as typed, mapped to the stored identity it names.

    Covers the reserved contracts only: the two own-words / direct-authoring ids
    and the per-source ``foreign-source`` prefix. A stored spelling, and every
    other name, comes back unchanged.
    """

    qualifier, bare = _split(name)
    for stored in _reserved_contracts():
        if bare == _public_spelling(stored):
            return qualifier + stored
    for prefix in _reserved_prefixes():
        public = _public_spelling(prefix)
        if bare.startswith(public) and len(bare) > len(public):
            return qualifier + prefix + bare[len(public) :]
    return name


@cache
def public_name_map() -> PublicNameMap:
    """The table :func:`public_name` applies, as served data (sorted, stable)."""

    return PublicNameMap(
        exact=tuple(
            NamePair(stored=stored, public=public) for stored, public in _exact_public().items()
        ),
        prefixes=tuple(
            NamePair(stored=prefix, public=_public_spelling(prefix))
            for prefix in _reserved_prefixes()
        ),
    )


__all__ = [
    "PUBLIC_PREFIX",
    "RESERVED_TAINT_LABELS",
    "STORED_PREFIX",
    "NamePair",
    "PublicNameMap",
    "public_name",
    "public_name_map",
    "stored_contract_name",
]
