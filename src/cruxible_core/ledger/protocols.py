"""Narrow structural seams for later Playbill batches."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from cruxible_client.contracts.types import GitObjectFormat
from cruxible_core.ledger.git import GitTreeEntry


@runtime_checkable
class LedgerRepositoryProtocol(Protocol):
    """Read/verify operations needed independently of system Git orchestration."""

    path: Path

    def object_format(self) -> GitObjectFormat: ...
    def read_main(self) -> str: ...
    def parent_of(self, oid: str) -> str | None: ...
    def list_tree(self, oid: str) -> tuple[GitTreeEntry, ...]: ...
    def list_tree_with_sizes(self, oid: str) -> tuple[GitTreeEntry, ...]: ...
    def read_blob(self, oid: str) -> bytes: ...
    def read_blobs(self, oids: Sequence[str]) -> dict[str, bytes]: ...
    def read_tree(self, oid: str) -> dict[str, bytes]: ...
    def verify_commit(self, oid: str) -> bool: ...


__all__ = [
    "LedgerRepositoryProtocol",
]
