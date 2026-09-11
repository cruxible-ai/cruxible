"""In-memory exact-tree repository helpers for PB-B tests."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.canonical import (
    GenerationRoot,
    SemanticRoot,
)
from cruxible_client.contracts.subjects import SubjectShell, render_subject
from cruxible_client.contracts.types import CompilerCoordinate, GitObjectFormat
from cruxible_core.compiler.compiler import P2_B5_COMPILER
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.ledger.git import GitTreeEntry

COMPILER_DIGEST = P2_B5_COMPILER.rule_digest
SEMANTIC_ROOT = SemanticRoot("11" * 32).tagged


def subject_bytes(name: str, *, retired: bool = False) -> bytes:
    return render_subject(
        SubjectShell(
            identity=ArtifactIdentity(kind="Subject", name=f"project.work_item/{name}"),
            subject_kind="project.work_item",
            subject_id=name,
            lifecycle=ArtifactLifecycle(state="retired" if retired else "live"),
        )
    )


class MemoryLedger:
    """A counted LedgerRepositoryProtocol implementation with exact entry metadata."""

    def __init__(
        self,
        path: Path,
        tree: dict[str, bytes],
        *,
        object_format: GitObjectFormat = "sha256",
        oid_seed: str = "generation",
        modes: dict[str, tuple[str, str]] | None = None,
        listed_sizes: dict[str, int | None] | None = None,
        verified: bool = True,
    ) -> None:
        path.mkdir(parents=True)
        self.path = path.resolve(strict=True)
        self._tree = dict(tree)
        self._format = object_format
        self._modes = modes or {}
        self._listed_sizes = listed_sizes or {}
        self._verified = verified
        digest = hashlib.sha1 if object_format == "sha1" else hashlib.sha256
        self._oid = digest(oid_seed.encode()).hexdigest()
        self._blob_by_oid: dict[str, bytes] = {}
        self.list_calls = 0
        self.read_calls = 0
        for content in tree.values():
            blob_oid = digest(
                b"blob " + str(len(content)).encode("ascii") + b"\0" + content
            ).hexdigest()
            self._blob_by_oid[blob_oid] = content

    @property
    def oid(self) -> str:
        return self._oid

    def object_format(self) -> GitObjectFormat:
        return self._format

    def read_main(self) -> str:
        return self._oid

    def parent_of(self, oid: str) -> str | None:
        assert oid == self._oid
        return None

    def list_tree(self, oid: str) -> tuple[GitTreeEntry, ...]:
        return self._list_tree(oid, with_sizes=False)

    def list_tree_with_sizes(self, oid: str) -> tuple[GitTreeEntry, ...]:
        return self._list_tree(oid, with_sizes=True)

    def _list_tree(self, oid: str, *, with_sizes: bool) -> tuple[GitTreeEntry, ...]:
        assert oid == self._oid
        self.list_calls += 1
        digest = hashlib.sha1 if self._format == "sha1" else hashlib.sha256
        return tuple(
            GitTreeEntry(
                path=path,
                mode=self._modes.get(path, ("100644", "blob"))[0],
                object_type=self._modes.get(path, ("100644", "blob"))[1],
                oid=digest(
                    b"blob " + str(len(content)).encode("ascii") + b"\0" + content
                ).hexdigest(),
                size=self._listed_sizes.get(path, len(content)) if with_sizes else None,
            )
            for path, content in self._tree.items()
        )

    def read_blob(self, oid: str) -> bytes:
        self.read_calls += 1
        return self._blob_by_oid[oid]

    def read_blobs(self, oids: Sequence[str]) -> dict[str, bytes]:
        self.read_calls += 1
        return {oid: self._blob_by_oid[oid] for oid in dict.fromkeys(oids)}

    def read_tree(self, oid: str) -> dict[str, bytes]:
        assert oid == self._oid
        return dict(self._tree)

    def verify_commit(self, oid: str) -> bool:
        return oid == self._oid and self._verified


def accepted_coordinate(
    repository: MemoryLedger,
    *,
    generation_byte: str = "22",
    semantic_root: str = SEMANTIC_ROOT,
) -> AcceptedProjectionCoordinate:
    return AcceptedProjectionCoordinate(
        instance_id="inst_projection_test",
        repository_path=str(repository.path),
        git_object_format=repository.object_format(),
        git_oid=repository.oid,
        semantic_root=semantic_root,
        generation_root=GenerationRoot(generation_byte * 32).tagged,
        compiler=CompilerCoordinate(rule_digest=COMPILER_DIGEST),
    )


__all__ = [
    "COMPILER_DIGEST",
    "MemoryLedger",
    "SEMANTIC_ROOT",
    "accepted_coordinate",
    "subject_bytes",
]
