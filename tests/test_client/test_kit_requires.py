"""A kit's ``requires``: the model, its bounds, its refusals, and unmoved manifest digests."""

from __future__ import annotations

from typing import get_args

import pytest
from pydantic import Field, TypeAdapter, ValidationError

from cruxible_client.contracts.kit_requires import (
    KitRequirement,
    KitRequires,
    KitRequiresRefusalCode,
    remove_required_message,
    required_kit_reference,
    requires_lowered_message,
    requires_missing_message,
    requires_unresolved_message,
)
from cruxible_client.contracts.kits import KitManifest

REQUIRES = TypeAdapter(KitRequires)


class _ManifestWithRequires(KitManifest):
    """How the kit-requires slice declares the field on the manifest."""

    requires: KitRequires = Field(default=(), exclude_if=lambda value: not value)


def _manifest_data() -> dict[str, object]:
    return {
        "kit_id": "security",
        "version": "1.3.0",
        "owns": ["security."],
        "artifacts": [
            {
                "path": "contracts/security.osv-affected/v1.json",
                "artifact_digest": "sha256:" + "1" * 64,
            }
        ],
    }


def test_a_requirement_names_a_kit_and_a_minimum_version() -> None:
    requirement = KitRequirement(kit_id="base", min_version="1.0.0")

    assert requirement.met_by("1.0.0")
    assert requirement.met_by("1.2.0")
    assert not requirement.met_by("0.9.9")
    assert not requirement.met_by(None)
    for bad in (
        {"kit_id": "Base", "min_version": "1.0.0"},
        {"kit_id": "base", "min_version": "1.0"},
    ):
        with pytest.raises(ValidationError):
            KitRequirement.model_validate(bad)


def test_requires_is_sorted_unique_and_bounded() -> None:
    assert (
        REQUIRES.validate_python(
            [{"kit_id": "base", "min_version": "1.0.0"}, {"kit_id": "web", "min_version": "2.0.0"}]
        )[1].kit_id
        == "web"
    )
    with pytest.raises(ValidationError, match="sorted by kit id"):
        REQUIRES.validate_python(
            [{"kit_id": "web", "min_version": "1.0.0"}, {"kit_id": "base", "min_version": "1.0.0"}]
        )
    with pytest.raises(ValidationError, match="sorted by kit id"):
        REQUIRES.validate_python(
            [{"kit_id": "base", "min_version": "1.0.0"}, {"kit_id": "base", "min_version": "2.0.0"}]
        )
    with pytest.raises(ValidationError, match="at most 16"):
        REQUIRES.validate_python(
            [{"kit_id": f"kit-{index:02d}", "min_version": "1.0.0"} for index in range(17)]
        )


def test_a_kit_that_requires_nothing_keeps_its_manifest_bytes_and_digest() -> None:
    plain = KitManifest.model_validate(_manifest_data())
    widened = _ManifestWithRequires.model_validate(_manifest_data())

    assert widened.model_dump(mode="json") == plain.model_dump(mode="json")
    assert widened.content_digest == plain.content_digest

    requiring = _ManifestWithRequires.model_validate(
        {**_manifest_data(), "requires": [{"kit_id": "base", "min_version": "1.0.0"}]}
    )
    assert requiring.model_dump(mode="json")["requires"] == [
        {"kit_id": "base", "min_version": "1.0.0"}
    ]
    assert requiring.content_digest != plain.content_digest


def test_the_refusal_codes_are_a_closed_vocabulary() -> None:
    assert set(get_args(KitRequiresRefusalCode)) == {
        "cruxible.kit.requires_missing",
        "cruxible.kit.requires_unresolved",
        "cruxible.kit.remove_required",
    }


def test_the_refusals_name_the_kit_and_the_repair() -> None:
    base = KitRequirement(kit_id="base", min_version="1.0.0")

    assert requires_missing_message("security", "1.3.0", base, None) == (
        "Kit security 1.3.0 requires kit base 1.0.0 or later; this instance holds none. "
        "Add it first: kit add base (or the path of a local build of base 1.0.0 or later)."
    )
    assert requires_missing_message("security", "1.3.0", base, "0.9.0") == (
        "Kit security 1.3.0 requires kit base 1.0.0 or later; this instance holds base 0.9.0. "
        "Upgrade it first: kit add base (or the path of a local build of base 1.0.0 or later)."
    )
    # coldps-01: the base kit's repair printed a literal '<KIT>'; it is bundled, so it
    # is named. Review F-002: any other kit's id and minimum version name no release
    # a registry is known to hold, so no reference is invented for it.
    web = KitRequirement(kit_id="web", min_version="2.1.0")
    assert required_kit_reference(base) == "base"
    assert required_kit_reference(web) is None
    assert requires_missing_message("acme", "1.0.0", web, None).endswith(
        "Add it first: kit add with the registry reference of a release of web 2.1.0 or "
        "later, or the path of a local build."
    )
    # The required kit's own add awaits approval: approving it is the repair.
    assert requires_missing_message(
        "security", "1.3.0", base, None, pending_proposal="sha256:ab"
    ).endswith(
        "this instance holds none. Kit base awaits approval as proposal sha256:ab: approve "
        "it first, then add kit security again."
    )
    assert requires_unresolved_message(
        "security", "1.3.0", "base", "Capability:web.fetch/v1", exact=True
    ) == (
        "Kit security 1.3.0 pins Capability:web.fetch/v1 from kit base; this instance does "
        "not hold it at the pinned digest."
    )
    assert requires_unresolved_message(
        "security", "1.3.0", "base", "CaptureContract:web.search-results", exact=False
    ).endswith("does not hold it live.")
    assert remove_required_message("base", ["security"]) == (
        "Kit base cannot be removed: kit security requires it."
    )
    assert requires_lowered_message("base", "0.9.0", "security", "1.3.0", base) == (
        "Kit base cannot be changed to 0.9.0: kit security 1.3.0 requires base 1.0.0 or "
        "later. Keep base at 1.0.0 or later, or remove security first."
    )
    assert remove_required_message("base", ["web", "security"]) == (
        "Kit base cannot be removed: kits security, web require it."
    )
