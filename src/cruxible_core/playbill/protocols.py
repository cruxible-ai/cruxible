"""Narrow structural seams for later Playbill batches."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from cruxible_core.playbill.canonical import Sha256Value
from cruxible_core.playbill.types import GitObjectFormat, PlaybillInspection


@runtime_checkable
class LedgerRepositoryProtocol(Protocol):
    """Read/verify operations needed independently of system Git orchestration."""

    def object_format(self) -> GitObjectFormat: ...
    def read_main(self) -> str: ...
    def parent_of(self, oid: str) -> str | None: ...
    def read_tree(self, oid: str) -> dict[str, bytes]: ...
    def verify_commit(self, oid: str) -> bool: ...


@runtime_checkable
class CanonicalDigesterProtocol(Protocol):
    """Domain-separated digest service used by roots and future artifacts."""

    def digest(self, domain: str, payload: Mapping[str, object]) -> Sha256Value: ...


@runtime_checkable
class GenerationCoordinateInspectorProtocol(Protocol):
    """Credential-safe inspection seam for service and future public surfaces."""

    def inspect(self) -> PlaybillInspection: ...


__all__ = [
    "CanonicalDigesterProtocol",
    "GenerationCoordinateInspectorProtocol",
    "LedgerRepositoryProtocol",
]
