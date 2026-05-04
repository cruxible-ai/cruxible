"""Checked-in world alias catalog and resolver helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from cruxible_core.errors import ConfigError
from cruxible_core.transport.types import parse_transport_ref


@dataclass(frozen=True)
class WorldCatalogEntry:
    """One checked-in published world alias entry."""

    alias: str
    base_transport_ref: str
    latest_release: str = "latest"
    default_kit: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class ResolvedWorldSource:
    """Resolved world overlay source and tracking refs."""

    source_ref: str
    pull_transport_ref: str
    tracking_transport_ref: str
    default_kit: str | None = None
    requested_release: str | None = None
    alias: str | None = None


WORLD_CATALOG: dict[str, WorldCatalogEntry] = {
    "kev-reference": WorldCatalogEntry(
        alias="kev-reference",
        base_transport_ref="oci://ghcr.io/cruxible-ai/models/kev-reference",
        default_kit="kev-triage",
        description="Published KEV reference world",
    ),
}


def get_world_catalog() -> dict[str, WorldCatalogEntry]:
    """Return the checked-in world alias catalog."""
    return WORLD_CATALOG


def resolve_world_source(
    *,
    transport_ref: str | None = None,
    world_ref: str | None = None,
) -> ResolvedWorldSource:
    """Resolve a world overlay source from either a raw transport ref or an alias."""
    normalized_transport = (transport_ref or "").strip() or None
    normalized_world = (world_ref or "").strip() or None
    if (normalized_transport is None) == (normalized_world is None):
        raise ConfigError("Provide exactly one of transport_ref or world_ref")
    if normalized_transport is not None:
        return ResolvedWorldSource(
            source_ref=normalized_transport,
            pull_transport_ref=normalized_transport,
            tracking_transport_ref=normalized_transport,
            default_kit=None,
        )
    assert normalized_world is not None
    if "://" in normalized_world:
        raise ConfigError("world_ref must be an alias like 'kev-reference' or 'kev-reference@v1'")

    alias, release = _parse_world_ref(normalized_world)
    try:
        entry = get_world_catalog()[alias]
    except KeyError as exc:
        known = ", ".join(sorted(get_world_catalog()))
        raise ConfigError(
            f"Unknown world_ref alias '{alias}'. Known aliases: {known or '(none)'}"
        ) from exc

    tracking_transport_ref = _compose_release_ref(entry.base_transport_ref, entry.latest_release)
    pull_transport_ref = _compose_release_ref(
        entry.base_transport_ref,
        release or entry.latest_release,
    )
    return ResolvedWorldSource(
        source_ref=normalized_world,
        pull_transport_ref=pull_transport_ref,
        tracking_transport_ref=tracking_transport_ref,
        default_kit=entry.default_kit,
        requested_release=release,
        alias=alias,
    )


def _parse_world_ref(world_ref: str) -> tuple[str, str | None]:
    alias, sep, release = world_ref.partition("@")
    alias = alias.strip()
    release = release.strip()
    if not alias:
        raise ConfigError("world_ref alias must not be empty")
    _validate_world_ref_part(alias, label="alias")
    if sep and not release:
        raise ConfigError("world_ref release must not be empty")
    if release:
        _validate_world_ref_part(release, label="release")
    return alias, release or None


def _validate_world_ref_part(value: str, *, label: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", value):
        raise ConfigError(
            f"world_ref {label} must match [A-Za-z0-9._-]+"
        )


def _compose_release_ref(base_transport_ref: str, release_id: str) -> str:
    scheme, remainder = parse_transport_ref(base_transport_ref)
    if scheme == "oci":
        leaf = remainder.rsplit("/", 1)[-1]
        if ":" in leaf or "@" in leaf:
            raise ConfigError("World catalog OCI refs must not already include a tag or digest")
        return f"oci://{remainder}:{release_id}"
    if scheme == "file":
        base_dir = Path(remainder)
        return f"file://{base_dir / release_id}"
    raise ConfigError(f"Unsupported world catalog transport scheme '{scheme}'")
