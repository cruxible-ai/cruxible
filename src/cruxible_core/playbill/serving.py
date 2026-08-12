"""Atomic local serving pointer for already durable immutable projections."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_core.playbill.canonical import (
    GenerationRoot,
    LogicalDigest,
    SemanticRoot,
    Sha256Value,
    canonical_bytes,
)
from cruxible_core.playbill.errors import ProjectionIntegrityError, ProjectionPublicationError
from cruxible_core.playbill.projection import (
    AcceptedProjectionCoordinate,
    AssemblerResult,
    ProjectionManifest,
    projection_manifest_name,
)
from cruxible_core.playbill.types import GitObjectFormat
from cruxible_core.storage.playbill_projection import ProjectionHandle, bind_projection

SERVING_MANIFEST_FILE = "serving.json"
_MANIFEST_RE = re.compile(r"^projection-[0-9a-f]{64}\.json$")
_OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class ServingCrashHook(Protocol):
    def __call__(self, checkpoint: str) -> None: ...


class ServingManifest(BaseModel):
    """The only local admission pointer; immutable builds alone are not visible."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-serving-manifest-v1"] = "playbill-serving-manifest-v1"
    instance_id: str
    git_object_format: GitObjectFormat
    git_oid: str
    semantic_root: str
    generation_root: str
    compiler_digest: str
    schema_version: int
    projection_manifest_name: str
    projection_manifest_digest: str
    logical_digest: str

    @field_validator("git_oid")
    @classmethod
    def _git_oid(cls, value: str) -> str:
        if not _OID_RE.fullmatch(value):
            raise ValueError("serving Git OID is malformed")
        return value

    @field_validator("semantic_root")
    @classmethod
    def _semantic_root(cls, value: str) -> str:
        SemanticRoot.from_tagged(value)
        return value

    @field_validator("generation_root")
    @classmethod
    def _generation_root(cls, value: str) -> str:
        GenerationRoot.from_tagged(value)
        return value

    @field_validator("compiler_digest", "projection_manifest_digest")
    @classmethod
    def _sha256(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("logical_digest")
    @classmethod
    def _logical_digest(cls, value: str) -> str:
        LogicalDigest.from_tagged(value)
        return value

    @field_validator("projection_manifest_name")
    @classmethod
    def _projection_manifest_name(cls, value: str) -> str:
        if not _MANIFEST_RE.fullmatch(value):
            raise ValueError("serving projection manifest name is malformed")
        return value

    @model_validator(mode="after")
    def _oid_format(self) -> "ServingManifest":
        expected = 40 if self.git_object_format == "sha1" else 64
        if len(self.git_oid) != expected:
            raise ValueError("serving Git OID differs from object format")
        if self.schema_version != 1:
            raise ValueError("serving projection schema version is unsupported")
        return self


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"sha256:{digest}"


def render_serving_manifest(manifest: ServingManifest) -> bytes:
    return canonical_bytes(manifest.model_dump(mode="json")) + b"\n"


def serving_manifest_for(result: AssemblerResult) -> ServingManifest:
    manifest_path = Path(result.manifest_path)
    if manifest_path.name != projection_manifest_name(result.manifest):
        raise ProjectionPublicationError("projection result manifest path is inconsistent")
    return ServingManifest(
        instance_id=result.manifest.instance_id,
        git_object_format=result.manifest.git_object_format,
        git_oid=result.manifest.git_oid,
        semantic_root=result.manifest.semantic_root,
        generation_root=result.manifest.generation_root,
        compiler_digest=result.manifest.compiler_digest,
        schema_version=result.manifest.schema_version,
        projection_manifest_name=manifest_path.name,
        projection_manifest_digest=_file_digest(manifest_path),
        logical_digest=result.manifest.logical_digest,
    )


def publish_serving_manifest(
    publication_directory: Path,
    result: AssemblerResult,
    *,
    crash_hook: ServingCrashHook | None = None,
) -> ServingManifest:
    """Atomically make one prebuilt projection visible to future admissions."""

    directory = publication_directory.resolve(strict=True)
    manifest = serving_manifest_for(result)
    content = render_serving_manifest(manifest)
    temporary = directory / f".serving-{secrets.token_hex(12)}.tmp"
    final = directory / SERVING_MANIFEST_FILE
    if crash_hook is not None:
        crash_hook("before:serving.publication")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ProjectionPublicationError("serving manifest write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, final)
    _fsync_directory(directory)
    if crash_hook is not None:
        crash_hook("after:serving.publication")
    return manifest


def load_serving_manifest(publication_directory: Path) -> ServingManifest:
    path = publication_directory / SERVING_MANIFEST_FILE
    if path.is_symlink() or not path.is_file():
        raise ProjectionIntegrityError("canonical serving manifest is absent")
    try:
        content = path.read_bytes()
        manifest = ServingManifest.model_validate_json(content)
    except Exception as exc:
        raise ProjectionIntegrityError("canonical serving manifest is malformed") from exc
    if render_serving_manifest(manifest) != content:
        raise ProjectionIntegrityError("canonical serving manifest is not canonical")
    return manifest


def _serving_matches_coordinate(
    manifest: ServingManifest,
    expected: AcceptedProjectionCoordinate,
) -> bool:
    return (
        manifest.instance_id == expected.instance_id
        and manifest.git_object_format == expected.git_object_format
        and manifest.git_oid == expected.git_oid
        and manifest.semantic_root == expected.semantic_root
        and manifest.generation_root == expected.generation_root
        and manifest.compiler_digest == expected.compiler.rule_digest
        and manifest.schema_version == expected.compiler.schema_version
    )


def bind_current_projection(
    publication_directory: Path,
    *,
    expected: AcceptedProjectionCoordinate,
) -> ProjectionHandle:
    """Admit only through the one coordinate-bound atomic serving pointer."""

    serving = load_serving_manifest(publication_directory)
    if not _serving_matches_coordinate(serving, expected):
        raise ProjectionIntegrityError("serving manifest differs from accepted coordinate")
    projection_path = publication_directory / serving.projection_manifest_name
    if _file_digest(projection_path) != serving.projection_manifest_digest:
        raise ProjectionIntegrityError("serving projection manifest digest mismatch")
    handle = bind_projection(projection_path, expected=expected)
    if handle.manifest.logical_digest != serving.logical_digest:
        handle.close()
        raise ProjectionIntegrityError("serving logical digest differs from immutable manifest")
    return handle


def remove_exact_projection_build(
    result: AssemblerResult,
    *,
    expected: ProjectionManifest,
) -> None:
    """Remove one proven losing build without scanning or pruning unrelated state."""

    if result.manifest != expected:
        raise ProjectionPublicationError("loser cleanup target differs from verified manifest")
    manifest_path = Path(result.manifest_path)
    if manifest_path.name != projection_manifest_name(expected):
        raise ProjectionPublicationError("loser cleanup manifest name is inconsistent")
    manifest_path.unlink(missing_ok=True)
    for piece in expected.pieces:
        (manifest_path.parent / piece.name).unlink(missing_ok=True)
    _fsync_directory(manifest_path.parent)


__all__ = [
    "SERVING_MANIFEST_FILE",
    "ServingManifest",
    "bind_current_projection",
    "load_serving_manifest",
    "publish_serving_manifest",
    "remove_exact_projection_build",
    "render_serving_manifest",
    "serving_manifest_for",
]
