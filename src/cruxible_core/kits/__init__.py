"""Kits that ship inside core: the base kit (``base/``), installed when an instance is created.

A bundled kit is an ordinary kit directory (``cruxible-kit.json`` plus
``artifacts/``) shipped as package data. Core holds its version and content
digest in one constant and refuses to load a directory that differs from it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BundledKitPin:
    """The version and content digest core holds for one bundled kit."""

    kit_id: str
    version: str
    content_digest: str


__all__ = ["BundledKitPin"]
