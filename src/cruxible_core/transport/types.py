"""Shared transport types for published state bundles."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cruxible_core.errors import ConfigError
from cruxible_core.snapshot.types import PublishedStateManifest, StateSnapshot


@dataclass
class PulledReleaseBundle:
    """Materialized published release bundle."""

    root_dir: Path
    manifest: PublishedStateManifest
    snapshot: StateSnapshot


class ReleaseTransport(Protocol):
    """Backend interface for publishing and pulling release bundles."""

    def publish(self, ref: str, bundle_dir: Path) -> str: ...
    def pull(self, ref: str, dest_dir: Path) -> PulledReleaseBundle: ...


def parse_transport_ref(ref: str) -> tuple[str, str]:
    """Split a transport reference into scheme and remainder."""
    if "://" not in ref:
        raise ConfigError("Transport ref must include a scheme, e.g. file:// or oci://")
    scheme, remainder = ref.split("://", 1)
    if not scheme or not remainder:
        raise ConfigError("Transport ref must include both scheme and target")
    return scheme, remainder
