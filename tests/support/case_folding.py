"""Detect whether a directory lives on a case-folding volume."""

from __future__ import annotations

from pathlib import Path

__all__ = ["volume_folds_case"]


def volume_folds_case(directory: Path) -> bool:
    """Return whether the volume resolves a case variant to the same entry."""

    probe = directory / "workspace-case-probe"
    probe.write_bytes(b"probe")
    try:
        return (directory / "WORKSPACE-CASE-PROBE").exists()
    finally:
        probe.unlink()
