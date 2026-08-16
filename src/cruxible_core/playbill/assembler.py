"""Instrumented Python reference assembler and crash-safe immutable publication."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Final, Protocol, TypeVar

from cruxible_core.playbill.cas import BodyProjectionProtocol
from cruxible_core.playbill.compiler import projection_registry_for_compiler
from cruxible_core.playbill.errors import (
    ProjectionCoordinateError,
    ProjectionPublicationError,
)
from cruxible_core.playbill.projection import (
    AcceptedProjectionCoordinate,
    AssemblerRequest,
    AssemblerResult,
    BuildInstrumentation,
    CandidateGenerationProjectionCoordinate,
    ProjectionManifest,
    ProjectionPiece,
    projection_manifest_name,
    projection_piece_name,
    render_projection_manifest,
)
from cruxible_core.playbill.projection_artifacts import ParsedProjectionTree, parse_projection_tree
from cruxible_core.playbill.projection_extensions import ProjectionExtensionRegistry
from cruxible_core.playbill.projection_tree import read_registered_tree
from cruxible_core.playbill.protocols import LedgerRepositoryProtocol
from cruxible_core.storage.playbill_projection import (
    initialize_projection_database,
    physical_file_digest,
    projection_logical_digest,
)

try:  # `resource` is absent on Windows; instrumentation is optional there.
    import resource as _resource
except ImportError:  # pragma: no cover - platform-specific
    _resource = None  # type: ignore[assignment]

PROJECTION_PREBUILD: Final = "projection.prebuild"
PROJECTION_PIECE_FILE_FSYNC: Final = "projection.piece_file_fsync"
PROJECTION_PIECE_DIRECTORY_FSYNC: Final = "projection.piece_directory_fsync"
PROJECTION_MANIFEST_WRITE: Final = "projection.manifest_write"
PROJECTION_MANIFEST_PUBLICATION: Final = "projection.manifest_publication"

PYTHON_REFERENCE_ASSEMBLER: Final = "python-reference"

PROJECTION_CRASH_POINTS: Final = (
    PROJECTION_PREBUILD,
    PROJECTION_PIECE_FILE_FSYNC,
    PROJECTION_PIECE_DIRECTORY_FSYNC,
    PROJECTION_MANIFEST_WRITE,
    PROJECTION_MANIFEST_PUBLICATION,
)

_PHASES: Final = (
    "git_traversal",
    "parse_normalize",
    "sort",
    "sqlite_load",
    "logical_export_digest",
    "fsync",
    "publication",
)

T = TypeVar("T")


class ProjectionCrashHook(Protocol):
    def __call__(self, checkpoint: str) -> None: ...


def _checkpoint(point: str, phase: str, hook: ProjectionCrashHook | None) -> None:
    if point not in PROJECTION_CRASH_POINTS:
        raise ProjectionPublicationError(f"unknown projection crash point: {point}")
    if phase not in {"before", "after"}:
        raise ProjectionPublicationError(f"unknown projection crash phase: {phase}")
    if hook is not None:
        hook(f"{phase}:{point}")


def _timed(timings: dict[str, int], phase: str, operation: Callable[[], T]) -> T:
    started = time.perf_counter_ns()
    try:
        return operation()
    finally:
        timings[phase] += time.perf_counter_ns() - started


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:  # pragma: no cover - defensive OS contract
            raise ProjectionPublicationError("projection write made no progress")
        view = view[written:]


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _exclusive_durable_write(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    try:
        _write_all(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(path, 0o400)
    _fsync_directory(path.parent)


def _high_water_memory_bytes() -> int | None:
    if _resource is None:
        return None
    try:
        maximum = _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss
    except (OSError, ValueError):  # pragma: no cover - platform-specific
        return None
    # Darwin reports bytes; Linux and the other supported Unix targets report KiB.
    return int(maximum if sys.platform == "darwin" else maximum * 1024)


class ProjectionAssembler:
    """Compile one previously verified ledger coordinate into an immutable build."""

    def __init__(
        self,
        repository: LedgerRepositoryProtocol,
        *,
        accepted: AcceptedProjectionCoordinate | CandidateGenerationProjectionCoordinate,
        publication_directory: Path,
        registry: ProjectionExtensionRegistry | None = None,
        bodies: BodyProjectionProtocol | None = None,
    ) -> None:
        if publication_directory.is_symlink() or not publication_directory.is_dir():
            raise ProjectionPublicationError(
                "projection publication directory must be an existing regular directory"
            )
        self._repository = repository
        self.accepted = accepted
        self.publication_directory = publication_directory.resolve(strict=True)
        self.registry = registry or projection_registry_for_compiler(accepted.compiler)
        self.bodies = bodies

    def request(self, *, output_staging_directory: Path) -> AssemblerRequest:
        """Create the exact serializable request for this verified coordinate."""

        return AssemblerRequest(
            instance_id=self.accepted.instance_id,
            repository_path=self.accepted.repository_path,
            git_object_format=self.accepted.git_object_format,
            git_oid=self.accepted.git_oid,
            semantic_root=self.accepted.semantic_root,
            generation_root=self.accepted.generation_root,
            compiler_digest=self.accepted.compiler.rule_digest,
            schema_version=self.accepted.compiler.schema_version,
            output_staging_directory=str(output_staging_directory),
        )

    def _verify_request(self, request: AssemblerRequest) -> Path:
        expected_fields = {
            "instance_id": self.accepted.instance_id,
            "repository_path": self.accepted.repository_path,
            "git_object_format": self.accepted.git_object_format,
            "git_oid": self.accepted.git_oid,
            "semantic_root": self.accepted.semantic_root,
            "generation_root": self.accepted.generation_root,
            "compiler_digest": self.accepted.compiler.rule_digest,
            "schema_version": self.accepted.compiler.schema_version,
        }
        actual_fields = {
            "instance_id": request.instance_id,
            "repository_path": request.repository_path,
            "git_object_format": request.git_object_format,
            "git_oid": request.git_oid,
            "semantic_root": request.semantic_root,
            "generation_root": request.generation_root,
            "compiler_digest": request.compiler_digest,
            "schema_version": request.schema_version,
        }
        mismatches = [
            name for name in expected_fields if expected_fields[name] != actual_fields[name]
        ]
        if mismatches:
            raise ProjectionCoordinateError(
                "assembler request differs from the verified coordinate: " + ", ".join(mismatches)
            )
        repository_path = Path(self._repository.path).resolve(strict=True)
        if repository_path != Path(request.repository_path).resolve(strict=True):
            raise ProjectionCoordinateError(
                "repository object differs from request repository_path"
            )
        staging = Path(request.output_staging_directory)
        if staging.parent.resolve(strict=True) != self.publication_directory:
            raise ProjectionPublicationError(
                "output staging directory must be a direct child of publication_directory"
            )
        if not staging.name.startswith(".stage-") or staging.name in {".stage-", ".", ".."}:
            raise ProjectionPublicationError("output staging directory needs a .stage-* name")
        if staging.exists() or staging.is_symlink():
            raise ProjectionPublicationError("output staging directory must not already exist")
        if self._repository.object_format() != request.git_object_format:
            raise ProjectionCoordinateError("repository object format differs from request")
        if isinstance(self.accepted, AcceptedProjectionCoordinate):
            if self._repository.read_main() != request.git_oid:
                raise ProjectionCoordinateError(
                    "accepted projection coordinate is not the repository main ref"
                )
        else:
            if self._repository.read_main() != self.accepted.base_git_oid:
                raise ProjectionCoordinateError(
                    "candidate projection base moved before durable prebuild"
                )
            if self._repository.parent_of(request.git_oid) != self.accepted.base_git_oid:
                raise ProjectionCoordinateError(
                    "candidate generation parent differs from its verified base"
                )
        if not self._repository.verify_commit(request.git_oid):
            raise ProjectionCoordinateError("requested generation commit signature does not verify")
        return staging

    def assemble(
        self,
        request: AssemblerRequest,
        *,
        crash_hook: ProjectionCrashHook | None = None,
    ) -> AssemblerResult:
        """Build, verify, and publish a complete one-piece projection manifest."""

        # This comparison is deliberately complete and precedes list_tree/read_blob.
        staging = self._verify_request(request)
        timings = {phase: 0 for phase in _PHASES}
        _checkpoint(PROJECTION_PREBUILD, "before", crash_hook)
        staging.mkdir(mode=0o700)
        os.chmod(staging, 0o700)
        _fsync_directory(self.publication_directory)
        _checkpoint(PROJECTION_PREBUILD, "after", crash_hook)

        blobs = _timed(
            timings,
            "git_traversal",
            lambda: read_registered_tree(
                self._repository,
                request.git_oid,
                limits=request.limits,
            ),
        )
        blob_map = {blob.path: blob.content for blob in blobs}
        parsed = _timed(
            timings,
            "parse_normalize",
            lambda: parse_projection_tree(
                blob_map,
                registry=self.registry,
                bodies=self.bodies,
                coordinate=request,
            ),
        )
        parsed = _timed(timings, "sort", lambda: _sorted_projection_tree(parsed))

        piece_name = projection_piece_name(request)
        staged_piece = staging / piece_name
        row_counts = _timed(
            timings,
            "sqlite_load",
            lambda: initialize_projection_database(
                staged_piece,
                request=request,
                parsed=parsed,
                registry=self.registry,
                assembler_implementation=PYTHON_REFERENCE_ASSEMBLER,
            ),
        )
        os.chmod(staged_piece, 0o400)

        def fsync_piece() -> None:
            _checkpoint(PROJECTION_PIECE_FILE_FSYNC, "before", crash_hook)
            _fsync_file(staged_piece)
            _checkpoint(PROJECTION_PIECE_FILE_FSYNC, "after", crash_hook)

        _timed(timings, "fsync", fsync_piece)
        logical = _timed(
            timings,
            "logical_export_digest",
            lambda: projection_logical_digest(staged_piece),
        )
        physical = physical_file_digest(staged_piece)
        byte_length = staged_piece.stat().st_size

        final_piece = self.publication_directory / piece_name

        def publish_piece() -> None:
            _checkpoint(PROJECTION_PIECE_DIRECTORY_FSYNC, "before", crash_hook)
            try:
                os.link(staged_piece, final_piece)
            except FileExistsError as exc:
                raise ProjectionPublicationError(
                    "immutable projection piece already exists for this coordinate"
                ) from exc
            _fsync_directory(self.publication_directory)
            _checkpoint(PROJECTION_PIECE_DIRECTORY_FSYNC, "after", crash_hook)

        _timed(timings, "fsync", publish_piece)
        manifest = ProjectionManifest(
            instance_id=request.instance_id,
            git_object_format=request.git_object_format,
            git_oid=request.git_oid,
            semantic_root=request.semantic_root,
            generation_root=request.generation_root,
            compiler_digest=request.compiler_digest,
            schema_version=request.schema_version,
            pieces=(
                ProjectionPiece(
                    ordinal=0,
                    name=piece_name,
                    byte_length=byte_length,
                    physical_digest=physical.tagged,
                ),
            ),
            logical_digest=logical.tagged,
            row_counts=row_counts,
        )
        manifest_name = projection_manifest_name(request)
        staged_manifest = staging / manifest_name
        final_manifest = self.publication_directory / manifest_name

        def write_manifest() -> None:
            _checkpoint(PROJECTION_MANIFEST_WRITE, "before", crash_hook)
            _exclusive_durable_write(staged_manifest, render_projection_manifest(manifest))
            _checkpoint(PROJECTION_MANIFEST_WRITE, "after", crash_hook)

        _timed(timings, "publication", write_manifest)

        def publish_manifest() -> None:
            _checkpoint(PROJECTION_MANIFEST_PUBLICATION, "before", crash_hook)
            try:
                os.link(staged_manifest, final_manifest)
            except FileExistsError as exc:
                raise ProjectionPublicationError(
                    "immutable projection manifest already exists for this coordinate"
                ) from exc
            _fsync_directory(self.publication_directory)
            _checkpoint(PROJECTION_MANIFEST_PUBLICATION, "after", crash_hook)

        _timed(timings, "publication", publish_manifest)

        # Successful-build temporary names are not retention candidates. On every
        # failure the stage and any unreferenced piece remain for explicit detection.
        staged_manifest.unlink()
        staged_piece.unlink()
        staging.rmdir()
        _fsync_directory(self.publication_directory)

        return AssemblerResult(
            manifest_path=str(final_manifest),
            manifest=manifest,
            git_oid=request.git_oid,
            semantic_root=request.semantic_root,
            generation_root=request.generation_root,
            logical_digest=logical.tagged,
            row_counts=row_counts,
            instrumentation=BuildInstrumentation(
                phase_nanoseconds=timings,
                high_water_memory_bytes=_high_water_memory_bytes(),
            ),
        )


def _sorted_projection_tree(parsed: ParsedProjectionTree) -> ParsedProjectionTree:
    """Make the sort phase explicit in the reference assembler profile."""

    return ParsedProjectionTree(
        envelopes=tuple(sorted(parsed.envelopes, key=lambda row: row.identity.encode("utf-8"))),
        pins=tuple(
            sorted(
                parsed.pins,
                key=lambda row: (
                    row.source_identity.encode("utf-8"),
                    row.target_identity.encode("utf-8"),
                ),
            )
        ),
        retired_identities=tuple(
            sorted(parsed.retired_identities, key=lambda value: value.encode("utf-8"))
        ),
        semantic_facts=tuple(
            sorted(
                parsed.semantic_facts,
                key=lambda fact: (
                    fact.schema_id.encode("utf-8"),
                    fact.schema_version,
                    fact.subject_identity.encode("utf-8"),
                    fact.fact_key.encode("utf-8"),
                ),
            )
        ),
        presentation_facts=tuple(
            sorted(
                parsed.presentation_facts,
                key=lambda fact: (
                    fact.schema_id.encode("utf-8"),
                    fact.schema_version,
                    fact.subject_identity.encode("utf-8"),
                    fact.fact_key.encode("utf-8"),
                ),
            )
        ),
    )


__all__ = [
    "PROJECTION_CRASH_POINTS",
    "PROJECTION_MANIFEST_PUBLICATION",
    "PROJECTION_MANIFEST_WRITE",
    "PROJECTION_PIECE_DIRECTORY_FSYNC",
    "PROJECTION_PIECE_FILE_FSYNC",
    "PROJECTION_PREBUILD",
    "PYTHON_REFERENCE_ASSEMBLER",
    "ProjectionAssembler",
    "ProjectionCrashHook",
]
