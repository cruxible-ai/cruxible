"""What core adds around the Capability artifact model: its pins and the reserved names.

The model, path, digest, parse and law live in
``cruxible_client.contracts.capabilities``. This module holds the two things
core decides: the pin roles a capability's Contract pins carry in the
projection and the dependency closure, the capability names core's builtin
serves (the only names the law admits under ``workspace.``), and that version 1
of a name under ``workspace.`` is exactly the bundled base kit's.
"""

from __future__ import annotations

from typing import Final

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.capabilities import (
    CAPABILITY_RESERVED,
    RESERVED_CAPABILITY_PREFIX,
    Capability,
    capability_digest,
    capability_path,
    is_reserved_capability_name,
)
from cruxible_client.contracts.law_problems import LawProblem
from cruxible_core.procedures.ports import WORKSPACE_FILE_BUILTIN

#: The pin role of a capability's request Contract, then of its result Contract.
CAPABILITY_CONTRACT_PIN_ROLES: Final = ("contract-in", "contract-out")

#: The capability names core's builtin serves: file reading, one path or a glob
#: (ruling O2). A capability under ``workspace.`` may be declared only under one
#: of these names, any version.
BUILTIN_CAPABILITY_NAMES: Final = frozenset({WORKSPACE_FILE_BUILTIN, "workspace.files"})


def capability_pins(capability: Capability) -> tuple[ArtifactPin, ...]:
    """The capability's exact pins on its Contracts: one per distinct Contract version.

    The request pin comes first. A capability whose request and result are the
    same Contract pins it once, as ``contract-in``: the projection keeps one pin
    row per (source, target) and the dependency closure must name the same edges
    the projection does, or a later generation's replay diverges. The projection
    and the closure both take their pins from here.
    """

    pins: dict[str, ArtifactPin] = {}
    for role, ref in zip(CAPABILITY_CONTRACT_PIN_ROLES, capability.contract_refs(), strict=True):
        if ref.digest is not None:
            pins.setdefault(
                ref.identity.qualified,
                ArtifactPin(role=role, target=ref.identity, artifact_digest=ref.digest),
            )
    return tuple(pins.values())


def bundled_version_problems(capability: Capability) -> tuple[LawProblem, ...]:
    """Version 1 of a name under ``workspace.`` is accepted only as the bundled base kit's.

    Compared by digest with the base kit this core ships, so no caller-supplied
    kit id stands in for it. Later versions are the adopter's to author.
    """

    if capability.version != 1 or not is_reserved_capability_name(capability.name):
        return ()
    from cruxible_core.kits.base_kit import bundled_base_artifact_digest

    bundled = bundled_base_artifact_digest(capability_path(capability.name, 1))
    if bundled is not None and capability_digest(capability).tagged == bundled:
        return ()
    return (
        LawProblem(
            CAPABILITY_RESERVED,
            f"{capability.ref.spelling} is under {RESERVED_CAPABILITY_PREFIX}, which core "
            "reserves: version 1 is the bundled base kit's, byte for byte. Install it with "
            "`cruxible kit add base`, then author version 2 to change it.",
        ),
    )


__all__ = [
    "BUILTIN_CAPABILITY_NAMES",
    "CAPABILITY_CONTRACT_PIN_ROLES",
    "bundled_version_problems",
    "capability_pins",
]
