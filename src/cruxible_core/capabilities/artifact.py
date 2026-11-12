"""What core adds around the Capability artifact model: its pins and the reserved names.

The model, path, digest, parse and law live in
``cruxible_client.contracts.capabilities``. This module holds the two things
core decides: the pin roles a capability's Contract pins carry in the
projection and the dependency closure, and the capability names core's builtin
serves (the only names the law admits under ``workspace.``).
"""

from __future__ import annotations

from typing import Final

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.capabilities import Capability
from cruxible_core.procedures.ports import WORKSPACE_FILE_BUILTIN

#: The pin role of a capability's request Contract, then of its result Contract.
CAPABILITY_CONTRACT_PIN_ROLES: Final = ("contract-in", "contract-out")

#: The capability names core's builtin serves: file reading, one path or a glob
#: (ruling O2). A capability under ``workspace.`` may be declared only under one
#: of these names, any version.
BUILTIN_CAPABILITY_NAMES: Final = frozenset({WORKSPACE_FILE_BUILTIN, "workspace.files"})


def capability_pins(capability: Capability) -> tuple[ArtifactPin, ...]:
    """The capability's exact pins on its two Contracts (an accepted capability pins both)."""

    return tuple(
        ArtifactPin(role=role, target=ref.identity, artifact_digest=ref.digest)
        for role, ref in zip(CAPABILITY_CONTRACT_PIN_ROLES, capability.contract_refs(), strict=True)
        if ref.digest is not None
    )


__all__ = ["BUILTIN_CAPABILITY_NAMES", "CAPABILITY_CONTRACT_PIN_ROLES", "capability_pins"]
