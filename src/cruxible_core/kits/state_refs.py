"""Checked-in state alias catalog and resolver helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from cruxible_core.errors import ConfigError
from cruxible_core.transport.types import parse_transport_ref


@dataclass(frozen=True)
class StateCatalogEntry:
    """One checked-in published state alias entry."""

    alias: str
    base_transport_ref: str
    latest_release: str = "latest"
    default_kit: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class ResolvedStateSource:
    """Resolved state overlay source and tracking refs."""

    source_ref: str
    pull_transport_ref: str
    tracking_transport_ref: str
    default_kit: str | None = None
    requested_release: str | None = None
    alias: str | None = None


STATE_CATALOG: dict[str, StateCatalogEntry] = {
    "kev-reference": StateCatalogEntry(
        alias="kev-reference",
        base_transport_ref="oci://ghcr.io/cruxible-ai/models/kev-reference",
        default_kit="kev-triage",
        description="Published KEV reference state",
    ),
}


def get_state_catalog() -> dict[str, StateCatalogEntry]:
    """Return the checked-in state alias catalog."""
    return STATE_CATALOG


def resolve_state_source(
    *,
    transport_ref: str | None = None,
    state_ref: str | None = None,
) -> ResolvedStateSource:
    """Resolve a state overlay source from either a raw transport ref or an alias."""
    normalized_transport = (transport_ref or "").strip() or None
    normalized_state = (state_ref or "").strip() or None
    if (normalized_transport is None) == (normalized_state is None):
        raise ConfigError("Provide exactly one of transport_ref or state_ref")
    if normalized_transport is not None:
        return ResolvedStateSource(
            source_ref=normalized_transport,
            pull_transport_ref=normalized_transport,
            tracking_transport_ref=normalized_transport,
            default_kit=None,
        )
    assert normalized_state is not None
    if "://" in normalized_state:
        raise ConfigError("state_ref must be an alias like 'kev-reference' or 'kev-reference@v1'")

    alias, release = _parse_state_ref(normalized_state)
    try:
        entry = get_state_catalog()[alias]
    except KeyError as exc:
        known = ", ".join(sorted(get_state_catalog()))
        raise ConfigError(
            f"Unknown state_ref alias '{alias}'. Known aliases: {known or '(none)'}"
        ) from exc

    tracking_transport_ref = _compose_release_ref(entry.base_transport_ref, entry.latest_release)
    pull_transport_ref = _compose_release_ref(
        entry.base_transport_ref,
        release or entry.latest_release,
    )
    return ResolvedStateSource(
        source_ref=normalized_state,
        pull_transport_ref=pull_transport_ref,
        tracking_transport_ref=tracking_transport_ref,
        default_kit=entry.default_kit,
        requested_release=release,
        alias=alias,
    )


def _parse_state_ref(state_ref: str) -> tuple[str, str | None]:
    alias, sep, release = state_ref.partition("@")
    alias = alias.strip()
    release = release.strip()
    if not alias:
        raise ConfigError("state_ref alias must not be empty")
    _validate_state_ref_part(alias, label="alias")
    if sep and not release:
        raise ConfigError("state_ref release must not be empty")
    if release:
        _validate_state_ref_part(release, label="release")
    return alias, release or None


def _validate_state_ref_part(value: str, *, label: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", value):
        raise ConfigError(f"state_ref {label} must match [A-Za-z0-9._-]+")


def _compose_release_ref(base_transport_ref: str, release_id: str) -> str:
    scheme, remainder = parse_transport_ref(base_transport_ref)
    if scheme == "oci":
        leaf = remainder.rsplit("/", 1)[-1]
        if ":" in leaf or "@" in leaf:
            raise ConfigError("State catalog OCI refs must not already include a tag or digest")
        return f"oci://{remainder}:{release_id}"
    if scheme == "file":
        base_dir = Path(remainder)
        return f"file://{base_dir / release_id}"
    raise ConfigError(f"Unsupported state catalog transport scheme '{scheme}'")
