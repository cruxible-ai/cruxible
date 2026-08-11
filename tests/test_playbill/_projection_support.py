"""In-memory exact-tree repository helpers for PB-B tests."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path

from cruxible_core.playbill.canonical import (
    ArtifactDigest,
    GenerationRoot,
    SemanticRoot,
    canonical_bytes,
)
from cruxible_core.playbill.git import GitTreeEntry
from cruxible_core.playbill.projection import AcceptedProjectionCoordinate
from cruxible_core.playbill.projection_artifacts import (
    FixtureArtifact,
    FixturePin,
    FixturePresentation,
)
from cruxible_core.playbill.projection_extensions import ProjectionFact
from cruxible_core.playbill.types import CompilerCoordinate, GitObjectFormat

COMPILER_DIGEST = "sha256:cc2ec0b2922da1c83a65734be3f911c782a7b2ab34ce2e4a5f006e47aa52b2d4"
SEMANTIC_ROOT = SemanticRoot("11" * 32).tagged


def fixture_bytes(
    artifact_id: str,
    value: object,
    *,
    revision: int = 1,
    predecessor_digest: str | None = None,
    pins: tuple[FixturePin, ...] = (),
    schema_id: str = "playbill.fixture.fact",
    schema_version: int = 1,
    fact_key: str = "value",
) -> bytes:
    artifact = FixtureArtifact(
        artifact_id=artifact_id,
        revision=revision,
        predecessor_digest=predecessor_digest,
        pins=pins,
        extension_facts=(
            ProjectionFact(
                schema_id=schema_id,
                schema_version=schema_version,
                subject_identity=artifact_id,
                fact_key=fact_key,
                value=value,
            ),
        ),
    )
    return canonical_bytes(artifact.model_dump(mode="json")) + b"\n"


def presentation_bytes(subject_identity: str, label: str) -> bytes:
    presentation = FixturePresentation(subject_identity=subject_identity, label=label)
    return canonical_bytes(presentation.model_dump(mode="json")) + b"\n"


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
            blob_oid = digest(b"blob\0" + content).hexdigest()
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
        assert oid == self._oid
        self.list_calls += 1
        digest = hashlib.sha1 if self._format == "sha1" else hashlib.sha256
        return tuple(
            GitTreeEntry(
                path=path,
                mode=self._modes.get(path, ("100644", "blob"))[0],
                object_type=self._modes.get(path, ("100644", "blob"))[1],
                oid=digest(b"blob\0" + content).hexdigest(),
                size=self._listed_sizes.get(path, len(content)),
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


def predecessor_digest() -> str:
    return ArtifactDigest("33" * 32).tagged


__all__ = [
    "COMPILER_DIGEST",
    "MemoryLedger",
    "SEMANTIC_ROOT",
    "accepted_coordinate",
    "fixture_bytes",
    "predecessor_digest",
    "presentation_bytes",
]
