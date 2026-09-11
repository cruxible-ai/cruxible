"""Concrete immutable SQLite storage for Playbill projection contracts."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import stat
from collections import OrderedDict
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from cruxible_client.contracts.canonical import (
    LogicalDigest,
    Sha256Value,
    typed_digest,
)
from cruxible_client.contracts.errors import ProjectionIntegrityError
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_core.compiler.projection_artifacts import ArtifactEnvelopeRow, ParsedProjectionTree
from cruxible_core.derived.memo import memo_get, memo_put
from cruxible_core.documents.projection_documents import (
    DocumentProjectionView,
    document_projection_view,
)
from cruxible_core.indexes.acquisition import DatabasePathChangedError, guard_database_path
from cruxible_core.indexes.claims.projection_claims import (
    ClaimProjectionView,
    claim_projection_view,
)
from cruxible_core.indexes.claims.projection_subjects import (
    SubjectProjectionView,
    subject_projection_view,
)
from cruxible_core.indexes.projection import (
    AcceptedProjectionCoordinate,
    AssemblerRequest,
    ProjectionManifest,
    ProjectionOrphan,
    projection_manifest_name,
    render_projection_manifest,
)
from cruxible_core.storage.cas import BodyAccessContext

# A bound piece is verified whole — a physical SHA-256 over the file, a
# ``PRAGMA integrity_check`` page scan and a canonical logical re-export — and
# that cost is paid per bind, not per generation. The identity below names the
# exact bytes that were verified: same device and inode, same size, same
# modification and inode-change timestamps, under the same accepted coordinate
# and the same manifest digests. Any write to the piece moves st_mtime_ns and
# st_ctime_ns, so a tampered file misses the memo and is verified again.
_VERIFIED_PIECE_CAPACITY = 8
_VERIFIED_PIECES: "OrderedDict[tuple[object, ...], str]" = OrderedDict()


def _verified_piece_identity(
    piece_path: Path,
    metadata: "os.stat_result",
    *,
    expected: AcceptedProjectionCoordinate,
    manifest: ProjectionManifest,
    physical_digest: str,
) -> tuple[object, ...]:
    return (
        str(piece_path),
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        expected.instance_id,
        expected.git_object_format,
        expected.git_oid,
        expected.semantic_root,
        expected.generation_root,
        expected.compiler.rule_digest,
        expected.compiler.schema_version,
        manifest.logical_digest,
        physical_digest,
    )


def _piece_already_verified(identity: tuple[object, ...]) -> bool:
    return memo_get(_VERIFIED_PIECES, identity) is not None


def _record_verified_piece(
    identity: tuple[object, ...], *, source_authenticated: bool = False
) -> None:
    previous = memo_get(_VERIFIED_PIECES, identity)
    value = (
        "source-authenticated"
        if source_authenticated or previous == "source-authenticated"
        else "verified"
    )
    memo_put(_VERIFIED_PIECES, identity, value, capacity=_VERIFIED_PIECE_CAPACITY)


def record_source_built_piece(path: Path, *, accepted: Any, manifest: ProjectionManifest) -> None:
    """Mark only the assembler's completed exact source-derived output ready."""
    before = path.stat()
    digest = physical_file_digest(path).tagged
    after = path.stat()
    if before != after or digest != manifest.pieces[0].physical_digest:
        raise ProjectionIntegrityError(
            "source-built projection changed before readiness publication"
        )
    identity = _verified_piece_identity(
        path, after, expected=accepted, manifest=manifest, physical_digest=digest
    )
    _record_verified_piece(identity, source_authenticated=True)


def reset_projection_verification_memo() -> None:
    """Forget every in-process piece verification.

    Verification is memoized on file identity, so a test that simulates
    corruption without writing to the piece (patching a digest function, say)
    needs an explicit reset to make the next bind pay the full check again.
    """

    _VERIFIED_PIECES.clear()


_PIECE_RE = re.compile(r"^piece-[0-9a-f]{64}-[0-9]{4}\.sqlite$")
_MANIFEST_RE = re.compile(r"^projection-[0-9a-f]{64}\.json$")
_ASSEMBLER_IMPLEMENTATION_RE = re.compile(r"^[a-z][a-z0-9.-]{0,63}$")


def update_projection_database(
    path: Path,
    *,
    parent: ProjectionHandle,
    request: AssemblerRequest,
    parsed: ParsedProjectionTree,
    changed_paths: frozenset[str],
    sources: Mapping[str, bytes],
    bodies: Any = None,
    resolve_digest: Callable[[str], tuple[str, ...]] | None = None,
) -> dict[str, int]:
    """Copy one verified typed publication, then replace changed owners atomically."""
    from cruxible_core.compiler.compiler import SUPPORTED_COMPILERS, artifact_codec_for_compiler
    from cruxible_core.indexes.typed_sqlite import update

    compiler = next(
        item for item in SUPPORTED_COMPILERS if item.rule_digest == request.compiler_digest
    )
    return update(
        path,
        parent=parent._connection,
        request=request,
        parsed=parsed,
        sources=sources,
        changed_paths=changed_paths,
        codec=artifact_codec_for_compiler(compiler),
        bodies=bodies,
        resolve_digest=resolve_digest,
    )


def initialize_projection_database(
    path: Path,
    *,
    request: AssemblerRequest,
    parsed: ParsedProjectionTree,
    assembler_implementation: str,
    sources: Mapping[str, bytes] | None = None,
    bodies: Any = None,
    resolve_digest: Callable[[str], tuple[str, ...]] | None = None,
) -> dict[str, int]:
    """Create and populate the complete PB-B one-piece SQLite projection."""

    from cruxible_core.compiler.compiler import SUPPORTED_COMPILERS, artifact_codec_for_compiler
    from cruxible_core.indexes.typed_sqlite import initialize

    compiler = next(
        item for item in SUPPORTED_COMPILERS if item.rule_digest == request.compiler_digest
    )
    if sources is None:
        sources = _source_repository(request.repository_path).read_tree(request.git_oid)
    return initialize(
        path,
        request=request,
        parsed=parsed,
        sources=sources,
        codec=artifact_codec_for_compiler(compiler),
        assembler_implementation=assembler_implementation,
        bodies=bodies,
        resolve_digest=resolve_digest,
    )


def _verify_projection_schema(connection: sqlite3.Connection) -> None:
    if connection.execute("PRAGMA user_version").fetchone()[0] != 3:
        raise ProjectionIntegrityError("projection SQLite schema version is unsupported")
    from cruxible_core.indexes.typed_sqlite import verify_schema

    verify_schema(connection)


def _canonical_connection_export(connection: sqlite3.Connection) -> dict[str, object]:
    _verify_projection_schema(connection)
    from cruxible_core.indexes.typed_sqlite import logical_export

    return logical_export(connection)


def canonical_logical_export(source: Path | sqlite3.Connection) -> dict[str, object]:
    """Export logical tables independent of page layout and binding metadata."""
    try:
        if isinstance(source, sqlite3.Connection):
            return _canonical_connection_export(source)
        connection = sqlite3.connect(f"{source.as_uri()}?mode=ro&immutable=1", uri=True)
        try:
            return _canonical_connection_export(connection)
        finally:
            connection.close()
    except (OSError, sqlite3.DatabaseError) as exc:
        raise ProjectionIntegrityError(
            "projection cannot produce a canonical logical export"
        ) from exc


def projection_logical_digest(source: Path | sqlite3.Connection) -> LogicalDigest:
    exported = canonical_logical_export(source)
    return typed_digest(
        LogicalDigest,
        "playbill-projection-logical-v3",
        exported,
    )


def physical_file_digest(path: Path) -> Sha256Value:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ProjectionIntegrityError("projection piece cannot be read") from exc
    return Sha256Value(digest.hexdigest())


def _descriptor_digest(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while chunk := os.read(descriptor, 1024 * 1024):
        digest.update(chunk)
    return Sha256Value(digest.hexdigest()).tagged


def _descriptor_uri(descriptor: int) -> str | None:
    """Use a descriptor alias so SQLite opens the verified inode, not its old name."""
    expected = os.fstat(descriptor)
    for directory in ("/dev/fd", "/proc/self/fd"):
        alias = Path(directory) / str(descriptor)
        try:
            duplicate = os.open(alias, os.O_RDONLY)
        except OSError:
            continue
        try:
            # Darwin's devfs alias reports a synthetic st_dev through stat;
            # fstat of the opened alias reports the underlying file identity.
            metadata = os.fstat(duplicate)
            if (metadata.st_dev, metadata.st_ino) == (expected.st_dev, expected.st_ino):
                return f"{alias.as_uri()}?mode=ro&immutable=1"
        finally:
            os.close(duplicate)
    return None


def load_projection_manifest(path: Path) -> ProjectionManifest:
    if path.is_symlink() or not path.is_file():
        raise ProjectionIntegrityError("projection manifest must be a regular file")
    try:
        raw = path.read_bytes()
        manifest = ProjectionManifest.model_validate_json(raw)
    except Exception as exc:
        raise ProjectionIntegrityError("projection manifest is missing or malformed") from exc
    if render_projection_manifest(manifest) != raw:
        raise ProjectionIntegrityError("projection manifest is not canonical")
    if path.name != projection_manifest_name(manifest):
        raise ProjectionIntegrityError("projection manifest name does not match its coordinates")
    return manifest


def _manifest_matches_coordinate(
    manifest: ProjectionManifest,
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


def _source_repository(path: str) -> Any:
    from cruxible_core.ledger.git import GitLedger

    # Exact blob reads do not consult signing custody; credentials are deliberately
    # unusable on this standalone read adapter. Instance callers attach their reader.
    return GitLedger(
        Path(path),
        signing_key_path=Path(path) / ".read-only",
        allowed_signers_path=Path(path) / ".read-only",
    )


class ProjectionHandle:
    """An immutable read handle whose complete build was verified exactly once."""

    def __init__(
        self,
        *,
        manifest_path: Path,
        manifest: ProjectionManifest,
        piece_paths: tuple[Path, ...],
        connection: sqlite3.Connection,
        accepted: AcceptedProjectionCoordinate,
        source_descriptor: int | None = None,
    ) -> None:
        self._source_descriptor = source_descriptor
        self.manifest_path = manifest_path
        self.manifest = manifest
        self.piece_paths = piece_paths
        self._connection = connection
        self.accepted = accepted
        self._closed = False
        self._verification_identity: tuple[object, ...] | None = None
        from cruxible_core.indexes.typed_state import TypedStateReader

        self.typed = TypedStateReader(
            connection, accepted, _source_repository(accepted.repository_path)
        )

    def attach_sources(self, repository: Any, *, bodies: Any, history: Any) -> ProjectionHandle:
        self.typed.repository, self.typed.bodies, self.typed.history = (
            repository,
            bodies,
            history,
        )
        try:
            self.require_source_authentication(repository=repository)
        except BaseException:
            self.close()
            raise
        return self

    def require_source_authentication(self, *, repository: Any = None) -> None:
        """Authenticate static typed rows before they can select acceptance inputs.

        Immutable hashes authenticate a file's self-consistency, not its claimed
        source. An unrecognized persisted piece pays a full Git/typed-row parity
        check once. Live citation envelopes remain outside this static boundary.
        """
        if self._closed or self._verification_identity is None:
            raise ProjectionIntegrityError(
                "source authentication requires a bound typed projection"
            )
        current_identity = _verified_piece_identity(
            self.index_path,
            self.index_path.stat(),
            expected=self.accepted,
            manifest=self.manifest,
            physical_digest=self.manifest.pieces[0].physical_digest,
        )
        if current_identity != self._verification_identity:
            raise ProjectionIntegrityError("bound typed projection file identity changed")
        if memo_get(_VERIFIED_PIECES, self._verification_identity) == "source-authenticated":
            return
        from cruxible_core.indexes.typed_sqlite import authenticate_source_rows

        authenticate_source_rows(self, repository=repository or self.typed.repository)
        _record_verified_piece(self._verification_identity, source_authenticated=True)

    @property
    def citations(self) -> Any:
        if self._closed:
            raise ProjectionIntegrityError("projection handle is closed")
        from cruxible_core.indexes.evidence.citation_sql import CitationReader

        return CitationReader(self._connection, self.typed.member_bytes)

    @property
    def index_path(self) -> Path:
        if len(self.piece_paths) != 1:
            raise ProjectionIntegrityError("PB-B can query exactly one physical piece")
        return self.piece_paths[0]

    def artifact_envelopes(
        self, *, paths: tuple[str, ...] | None = None
    ) -> tuple[ArtifactEnvelopeRow, ...]:
        """Read typed artifact metadata, optionally for an exact changed-path set."""
        if self._closed:
            raise ProjectionIntegrityError("projection handle is closed")
        return self.typed.envelopes(paths=paths)

    def document(
        self,
        identity: str,
        *,
        access: BodyAccessContext,
    ) -> DocumentProjectionView | None:
        """Read one canonical Document; proposal refs are outside this bound handle."""

        if self._closed:
            raise ProjectionIntegrityError("projection handle is closed")
        envelope = self.typed.envelope(identity)
        if envelope is None or envelope.kind != "document":
            return None
        return document_projection_view(
            envelope,
            self.typed.facts(identity=identity),
            coordinate=self.accepted,
            access=access,
        )

    def list_documents(
        self,
        *,
        access: BodyAccessContext,
    ) -> tuple[DocumentProjectionView, ...]:
        """List canonical Documents in stable identity order."""

        if self._closed:
            raise ProjectionIntegrityError("projection handle is closed")
        return tuple(
            view
            for row in self.typed.envelopes(kind="document")
            if (view := self.document(row.identity, access=access)) is not None
        )

    def subject(self, identity: str) -> SubjectProjectionView | None:
        """Read one canonical identity-only Subject at this accepted coordinate."""

        if self._closed:
            raise ProjectionIntegrityError("projection handle is closed")
        envelope = self.typed.envelope(identity)
        if envelope is None or envelope.kind != "subject":
            return None
        return subject_projection_view(
            envelope, self.typed.facts(identity=identity), coordinate=self.accepted
        )

    def list_subjects(self) -> tuple[SubjectProjectionView, ...]:
        """List canonical Subjects in stable kind-qualified identity order."""

        if self._closed:
            raise ProjectionIntegrityError("projection handle is closed")
        return tuple(
            view
            for row in self.typed.envelopes(kind="subject")
            if (view := self.subject(row.identity)) is not None
        )

    def claim(self, identity: str) -> ClaimProjectionView | None:
        """Read one canonical first-class Claim at this accepted coordinate."""

        if self._closed:
            raise ProjectionIntegrityError("projection handle is closed")
        envelope = self.typed.envelope(identity)
        if envelope is None or envelope.kind != "claim":
            return None
        return claim_projection_view(
            envelope, self.typed.facts(identity=identity), coordinate=self.accepted
        )

    def select_claim_identities(
        self,
        *,
        subject_paths: tuple[str, ...],
        predicates: tuple[str, ...],
        include_retired: bool,
        after: str,
        limit: int,
    ) -> tuple[str, ...]:
        """Select a bounded page without materializing unrelated Claim views."""
        if self._closed:
            raise ProjectionIntegrityError("projection handle is closed")
        clauses = [
            "identity>?",
            "subject_path IN (" + ",".join("?" for _ in subject_paths) + ")",
        ]
        values: list[object] = [after, *subject_paths]
        if predicates:
            clauses.append("predicate IN (" + ",".join("?" for _ in predicates) + ")")
            values.extend(predicates)
        if not include_retired:
            clauses.append("lifecycle='live'")
        values.append(limit)
        return tuple(
            row[0]
            for row in self._connection.execute(
                "SELECT identity FROM claims WHERE "
                + " AND ".join(clauses)
                + " ORDER BY identity LIMIT ?",
                values,
            )
        )

    def list_claims(
        self,
        *,
        subject: SemanticAddress | None = None,
        predicate: str | None = None,
        include_retired: bool = True,
    ) -> tuple[ClaimProjectionView, ...]:
        """Select exact Claim keys before materializing their canonical views."""

        if self._closed:
            raise ProjectionIntegrityError("projection handle is closed")
        values: list[object] = []
        clauses: list[str] = []
        sql = "SELECT identity FROM claims"
        if subject is not None:
            clauses.extend(
                ("subject_path=?", "subject_selector_scheme=?", "subject_selector_value=?")
            )
            values.extend((subject.artifact_path, subject.selector.scheme, subject.selector.value))
        if predicate is not None:
            clauses.append("predicate=?")
            values.append(predicate)
        if not include_retired:
            clauses.append("lifecycle='live'")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        identities = self._connection.execute(sql + " ORDER BY identity", values).fetchall()
        return tuple(
            view
            for row in identities
            if (view := self.claim(cast(str, row["identity"]))) is not None
        )

    def close(self) -> None:
        if not self._closed:
            try:
                self._connection.close()
            finally:
                if self._source_descriptor is not None:
                    os.close(self._source_descriptor)
                    self._source_descriptor = None
                self._closed = True

    def __enter__(self) -> "ProjectionHandle":
        if self._closed:
            raise ProjectionIntegrityError("projection handle is closed")
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def bind_projection(
    manifest_path: Path,
    *,
    expected: AcceptedProjectionCoordinate,
) -> ProjectionHandle:
    """Verify one complete manifest and return a handle that does no per-read rehash."""

    manifest = load_projection_manifest(manifest_path)
    if not _manifest_matches_coordinate(manifest, expected):
        raise ProjectionIntegrityError("projection manifest differs from the accepted coordinate")
    if len(manifest.pieces) != 1:
        raise ProjectionIntegrityError("PB-B supports one-piece serving only")

    pieces: list[Path] = []
    identities: list[tuple[object, ...]] = []
    already_verified = True
    for piece in manifest.pieces:
        path = manifest_path.parent / piece.name
        if path.is_symlink() or not path.is_file():
            raise ProjectionIntegrityError(f"projection piece is missing: {piece.name}")
        resolved = path.resolve(strict=True)
        if resolved.parent != manifest_path.parent.resolve(strict=True):
            raise ProjectionIntegrityError("projection piece escapes its publication directory")
        metadata = resolved.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != piece.byte_length:
            raise ProjectionIntegrityError(f"projection piece size mismatch: {piece.name}")
        identity = _verified_piece_identity(
            resolved,
            metadata,
            expected=expected,
            manifest=manifest,
            physical_digest=piece.physical_digest,
        )
        identities.append(identity)
        if _piece_already_verified(identity):
            pieces.append(resolved)
            continue
        already_verified = False
        pieces.append(resolved)

    index_path = pieces[0]
    connection: sqlite3.Connection | None = None
    descriptor: int | None = None
    try:
        for attempt in range(3):
            try:
                # Some SQLite VFS implementations resolve descriptor aliases
                # back to pathnames. Guard that short acquisition too, including
                # an ancestor swapped away and restored before connect returns.
                with guard_database_path(index_path):
                    descriptor = os.open(index_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    opened_identity = _verified_piece_identity(
                        index_path,
                        os.fstat(descriptor),
                        expected=expected,
                        manifest=manifest,
                        physical_digest=manifest.pieces[0].physical_digest,
                    )
                    if opened_identity != identities[0]:
                        raise ProjectionIntegrityError(
                            "projection piece changed during acquisition"
                        )
                    descriptor_uri = _descriptor_uri(descriptor)
                    connection = sqlite3.connect(
                        descriptor_uri or f"{index_path.as_uri()}?mode=ro&immutable=1",
                        uri=True,
                    )
                    # Make SQLite acquire its actual file before ending the
                    # namespace proof. Full cold scans use this connection later.
                    connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
                break
            except DatabasePathChangedError:
                if connection is not None:
                    connection.close()
                    connection = None
                if descriptor is not None:
                    os.close(descriptor)
                    descriptor = None
                if attempt == 2:
                    raise
        assert descriptor is not None and connection is not None
        if (
            not already_verified
            and _descriptor_digest(descriptor) != manifest.pieces[0].physical_digest
        ):
            raise ProjectionIntegrityError(
                f"projection piece digest mismatch: {manifest.pieces[0].name}"
            )
        if descriptor_uri is None:
            # Without a descriptor namespace there is no pathname-only proof of
            # which inode SQLite opened. This portable fallback charges a full
            # opened-snapshot hash per bind, never certifying it from a warm memo.
            actual = Sha256Value(hashlib.sha256(connection.serialize()).hexdigest()).tagged
            if actual != manifest.pieces[0].physical_digest:
                raise ProjectionIntegrityError("opened projection snapshot digest mismatch")
        connection.row_factory = sqlite3.Row
        _verify_projection_schema(connection)
        # A piece already verified in this process under this exact file
        # identity does not re-run the page scan. Record that the check did not
        # run rather than synthesizing a passing result for it: a forged "ok"
        # would read, here and to anything that later surfaced it, as a check
        # that ran.
        if connection.execute("PRAGMA user_version").fetchone()[0] != 3:
            raise ProjectionIntegrityError("manifest and SQLite storage versions differ")
        integrity_ok = already_verified
        if not already_verified:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            integrity_ok = integrity is not None and tuple(integrity) == ("ok",)
        metadata_row = connection.execute(
            "SELECT instance_id,git_object_format,git_oid,semantic_root,generation_root "
            "FROM generation_metadata WHERE singleton = 1"
        ).fetchone()
        compiler_row = connection.execute(
            "SELECT schema_version,compiler_digest FROM compiler_coordinates WHERE singleton = 1"
        ).fetchone()
        assembler_row = connection.execute(
            "SELECT implementation,contract_version FROM assembler_metadata WHERE singleton = 1"
        ).fetchone()
        if already_verified:
            # Counts were checked under this exact immutable file identity.
            counts = manifest.row_counts
        else:
            from cruxible_core.indexes.typed_sqlite import row_counts

            counts = row_counts(connection)
        expected_metadata = (
            manifest.instance_id,
            manifest.git_object_format,
            manifest.git_oid,
            manifest.semantic_root,
            manifest.generation_root,
        )
        expected_compiler = (
            expected.compiler.schema_version,
            expected.compiler.rule_digest,
        )
        binding_metadata = tuple(metadata_row) if metadata_row is not None else None
        compiler = tuple(compiler_row) if compiler_row is not None else None
        assembler = tuple(assembler_row) if assembler_row is not None else None
        assembler_valid = (
            assembler is not None
            and len(assembler) == 2
            and isinstance(assembler[0], str)
            and _ASSEMBLER_IMPLEMENTATION_RE.fullmatch(assembler[0]) is not None
            and assembler[1] == 1
        )
        if (
            not integrity_ok
            or binding_metadata != expected_metadata
            or compiler != expected_compiler
            or not assembler_valid
        ):
            raise ProjectionIntegrityError("projection internal binding metadata is inconsistent")
        sorted_counts = dict(sorted(counts.items(), key=lambda item: item[0].encode("utf-8")))
        if sorted_counts != manifest.row_counts:
            raise ProjectionIntegrityError("projection row counts differ from the manifest")
        if (
            not already_verified
            and projection_logical_digest(connection).tagged != manifest.logical_digest
        ):
            raise ProjectionIntegrityError("projection canonical logical digest mismatch")
        final_identity = _verified_piece_identity(
            index_path,
            os.fstat(descriptor),
            expected=expected,
            manifest=manifest,
            physical_digest=manifest.pieces[0].physical_digest,
        )
        if final_identity != opened_identity:
            raise ProjectionIntegrityError("projection piece changed while binding")
        for identity in identities:
            _record_verified_piece(identity)
        handle = ProjectionHandle(
            manifest_path=manifest_path.resolve(strict=True),
            manifest=manifest,
            piece_paths=tuple(pieces),
            connection=connection,
            accepted=expected,
            source_descriptor=descriptor,
        )
        handle._verification_identity = identities[0]
        return handle
    except (OSError, sqlite3.DatabaseError) as exc:
        if connection is not None:
            connection.close()
        if descriptor is not None:
            os.close(descriptor)
        raise ProjectionIntegrityError(
            "projection piece is not a valid PB-B SQLite database"
        ) from exc
    except BaseException:
        if connection is not None:
            connection.close()
        if descriptor is not None:
            os.close(descriptor)
        raise


def detect_projection_orphans(publication_directory: Path) -> tuple[ProjectionOrphan, ...]:
    """Report deterministic cleanup candidates without deleting any bytes."""

    if not publication_directory.is_dir():
        raise ProjectionIntegrityError("projection publication directory is absent")
    orphans: list[ProjectionOrphan] = []
    referenced: set[str] = set()
    manifests = sorted(
        (path for path in publication_directory.iterdir() if _MANIFEST_RE.fullmatch(path.name)),
        key=lambda path: path.name.encode("utf-8"),
    )
    for path in manifests:
        try:
            manifest = load_projection_manifest(path)
        except ProjectionIntegrityError as exc:
            orphans.append(
                ProjectionOrphan(kind="malformed-manifest", path=str(path), detail=str(exc))
            )
            continue
        for piece in manifest.pieces:
            referenced.add(piece.name)
            piece_path = publication_directory / piece.name
            if not piece_path.is_file() or piece_path.is_symlink():
                orphans.append(
                    ProjectionOrphan(
                        kind="missing-piece",
                        path=str(piece_path),
                        detail=f"referenced by {path.name}",
                    )
                )
    for path in sorted(publication_directory.iterdir(), key=lambda item: item.name.encode("utf-8")):
        if path.name.startswith(".stage-"):
            orphans.append(
                ProjectionOrphan(
                    kind="staging-build",
                    path=str(path),
                    detail="private staging output was not fully retired",
                )
            )
        elif _PIECE_RE.fullmatch(path.name) and path.name not in referenced:
            orphans.append(
                ProjectionOrphan(
                    kind="unreferenced-piece",
                    path=str(path),
                    detail="immutable piece is not referenced by a valid manifest",
                )
            )
    return tuple(
        sorted(
            orphans,
            key=lambda item: (item.kind.encode("utf-8"), item.path.encode("utf-8")),
        )
    )


__all__ = [
    "ProjectionHandle",
    "bind_projection",
    "canonical_logical_export",
    "detect_projection_orphans",
    "initialize_projection_database",
    "load_projection_manifest",
    "physical_file_digest",
    "projection_logical_digest",
    "reset_projection_verification_memo",
]
