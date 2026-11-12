"""A kit that requires another kit: the manifest's ``requires`` entries and their refusals.

A kit's manifest may list ``requires: [{kit_id, min_version}]``, sorted by kit
id and omitted when empty, so a kit that requires nothing keeps its manifest
bytes and content digest. ``kit build`` leaves out of the bundle every pin to a
definition another installed kit owns and records that kit here, at the
version installed where the kit was built. ``kit add`` then refuses unless each
required kit is installed at ``min_version`` or later
(``cruxible.kit.requires_missing``) and each left-out pin resolves on the
instance (``cruxible.kit.requires_unresolved``); ``kit remove`` refuses to
remove a kit an installed kit requires (``cruxible.kit.remove_required``).

The manifest field is declared as ``requires: KitRequires = Field(default=(),
exclude_if=lambda value: not value)``; the same tuple is recorded on the kit
receipt as installed.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Final, Literal, TypeAlias

from pydantic import AfterValidator, BaseModel, ConfigDict, field_validator

#: The most kits one kit may require.
MAX_KIT_REQUIRES = 16

KIT_REQUIRES_MISSING: Final = "cruxible.kit.requires_missing"
KIT_REQUIRES_UNRESOLVED: Final = "cruxible.kit.requires_unresolved"
KIT_REMOVE_REQUIRED: Final = "cruxible.kit.remove_required"

#: Every code a kit's ``requires`` can refuse with.
KitRequiresRefusalCode = Literal[
    "cruxible.kit.requires_missing",
    "cruxible.kit.requires_unresolved",
    "cruxible.kit.remove_required",
]


class KitRequirement(BaseModel):
    """One kit this kit requires, at ``min_version`` (MAJOR.MINOR.PATCH) or later."""

    model_config = ConfigDict(extra="forbid", frozen=True, json_schema_mode_override="validation")

    kit_id: str
    min_version: str

    @field_validator("kit_id")
    @classmethod
    def _kit_id(cls, value: str) -> str:
        # ``kits`` declares ``requires`` with this module's types, so it imports
        # this module: its rules are read when they run, never at import.
        from cruxible_client.contracts.kits import _kit_id

        return _kit_id(value)

    @field_validator("min_version")
    @classmethod
    def _min_version(cls, value: str) -> str:
        from cruxible_client.contracts.kits import kit_version_key

        kit_version_key(value)
        return value

    def met_by(self, installed_version: str | None) -> bool:
        """Whether a kit installed at ``installed_version`` (``None``: not installed) meets it."""

        from cruxible_client.contracts.kits import kit_version_key

        return installed_version is not None and kit_version_key(
            installed_version
        ) >= kit_version_key(self.min_version)


def validate_kit_requires(value: tuple[KitRequirement, ...]) -> tuple[KitRequirement, ...]:
    """Requirements are sorted by kit id, once each, and bounded."""

    if len(value) > MAX_KIT_REQUIRES:
        raise ValueError(f"a kit requires at most {MAX_KIT_REQUIRES} kits")
    ids = tuple(item.kit_id for item in value)
    if ids != tuple(sorted(set(ids))):
        raise ValueError("required kits are sorted by kit id, once each")
    return value


#: The manifest (and kit receipt) field type: ``requires: KitRequires``.
KitRequires: TypeAlias = Annotated[
    tuple[KitRequirement, ...], AfterValidator(validate_kit_requires)
]


def requires_missing_message(
    kit_id: str, version: str, requirement: KitRequirement, installed: str | None
) -> str:
    """``cruxible.kit.requires_missing``: a required kit is absent or older than required."""

    held = "none" if installed is None else f"{requirement.kit_id} {installed}"
    verb = "Add" if installed is None else "Upgrade"
    return (
        f"Kit {kit_id} {version} requires kit {requirement.kit_id} {requirement.min_version} "
        f"or later; this instance holds {held}. {verb} it first: "
        f"cruxible kit add {requirement.kit_id}."
    )


def requires_unresolved_message(
    kit_id: str, version: str, required_kit_id: str, definition: str, *, exact: bool
) -> str:
    """``cruxible.kit.requires_unresolved``: a pin the bundle left out does not resolve.

    ``definition`` names it (``Capability:web.fetch/v1``); ``exact``: it is
    immutable and must resolve at the pinned digest, else it resolves by name, live.
    """

    how = "at the pinned digest" if exact else "live"
    return (
        f"Kit {kit_id} {version} pins {definition} from kit {required_kit_id}; this instance "
        f"does not hold it {how}."
    )


def remove_required_message(kit_id: str, dependents: Sequence[str]) -> str:
    """``cruxible.kit.remove_required``: installed kits require the kit being removed."""

    names = ", ".join(sorted(dependents))
    subject = f"kit {names} requires" if len(dependents) == 1 else f"kits {names} require"
    return f"Kit {kit_id} cannot be removed: {subject} it."


__all__ = [
    "KIT_REMOVE_REQUIRED",
    "KIT_REQUIRES_MISSING",
    "KIT_REQUIRES_UNRESOLVED",
    "MAX_KIT_REQUIRES",
    "KitRequirement",
    "KitRequires",
    "KitRequiresRefusalCode",
    "remove_required_message",
    "requires_missing_message",
    "requires_unresolved_message",
    "validate_kit_requires",
]
