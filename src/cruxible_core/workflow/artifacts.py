"""Shared artifact path helpers for workflow lock and runtime code."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse


def resolve_local_artifact_path(uri: str, config_base_path: Path) -> Path | None:
    """Resolve local artifact URIs relative to the config directory."""
    parsed = urlparse(uri)
    if parsed.scheme in {"", "file"}:
        if parsed.scheme == "file":
            raw_path = Path(parsed.path)
        else:
            raw_path = Path(uri)
        if not raw_path.is_absolute():
            raw_path = (config_base_path / raw_path).resolve()
        return raw_path
    return None
