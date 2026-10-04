"""The floor v6 manifest and the floor delta: one shape on both sides of the wire.

The floor is a pure function of an accepted coordinate. Its root
``manifest.json`` binds every file to its content digest and to ``changed_at``,
the latest accepted generation that touched any of the file's inputs. Because
the stamp is ledger-derived, the files a client at ``base`` lacks are exactly
those with ``changed_at > base`` plus the paths the floor dropped since then,
and a delta between two generations is a deterministic function of the pair.

Both sides derive ``manifest.json`` from the same model and the same renderer,
so the manifest is never shipped inside a delta: the receiver rebuilds it from
its own base manifest plus the delta, and the head manifest digest proves the
result before anything is written.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_client.contracts.primitives import pretty_json
from cruxible_client.contracts.projection import AcceptedCoordinate

FLOOR_FORMAT = "playbill-floor-export-v6"
FLOOR_MANIFEST_TAG = "playbill-floor-manifest-v6"
FLOOR_MANIFEST_PATH = "manifest.json"
# Local metadata the workspace writer adds outside the daemon-verified manifest:
# `.gitignore` ignores the floor itself; the indexes join workspace bindings:
# `sources/INDEX` is the daemon's `sources/LEDGER` with workspace paths joined
# in, and `projections/INDEX` binds workspace files to accepted refs.
FLOOR_LOCAL_PATHS = frozenset({".gitignore", "projections/INDEX", "sources/INDEX"})

_SHA256 = r"^sha256:[0-9a-f]{64}$"
_OID = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"


class _StrictFloorModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# The name a crash-interrupted apply leaves behind; never a floor file's name.
FLOOR_STAGING_NAME = re.compile(r"\.floor-[0-9a-f]{16}\.tmp")


def safe_floor_path(value: str) -> str:
    """A floor-relative POSIX path that cannot leave the floor directory."""

    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or "\x00" in value
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError(f"floor path escapes its root: {value!r}")
    if any(FLOOR_STAGING_NAME.fullmatch(part) for part in path.parts):
        raise ValueError(f"floor path takes the apply's staging name: {value!r}")
    return value


def floor_path_key(value: str) -> str:
    """The name a case- and normalization-insensitive filesystem sees for ``value``.

    Two paths with one key address one file on APFS, HFS+ or NTFS, so a floor
    may never hold both.
    """

    folded = unicodedata.normalize("NFC", value).casefold()
    return unicodedata.normalize("NFC", folded)


_RESERVED_KEYS = frozenset(
    floor_path_key(path) for path in (FLOOR_MANIFEST_PATH, *FLOOR_LOCAL_PATHS)
)


def floor_path_reserved(value: str) -> bool:
    """Whether ``value`` names, on some filesystem, the manifest or a client-owned file."""

    return floor_path_key(value) in _RESERVED_KEYS


def _directory_keys(path: str) -> Iterable[str]:
    parts = path.split("/")
    for end in range(1, len(parts)):
        yield floor_path_key("/".join(parts[:end]))


def check_floor_paths(paths: Iterable[str], *, label: str) -> None:
    """Refuse any set of floor paths a real filesystem could not hold apart.

    Every path is safe and unreserved; no two share a filesystem key; and no
    path is, on some filesystem, a directory another path lives under -- the
    reserved paths included, so no floor file can shadow the client's own.
    """

    keys: dict[str, str] = {}
    for path in paths:
        safe_floor_path(path)
        if floor_path_reserved(path):
            raise ValueError(f"{label} may not name {path!r}: it is reserved")
        key = floor_path_key(path)
        if key in keys:
            raise ValueError(f"{label} names one file twice: {keys[key]!r} and {path!r}")
        keys[key] = path
    directories = {
        key
        for path in (*keys.values(), FLOOR_MANIFEST_PATH, *FLOOR_LOCAL_PATHS)
        for key in _directory_keys(path)
    }
    for key, path in keys.items():
        if key in directories:
            raise ValueError(f"{label} names {path!r} as a file and as a directory")
    # Nothing may live under a reserved file either: the manifest and the
    # client's own files stay files.
    for key in _RESERVED_KEYS & directories:
        raise ValueError(f"{label} names a path under the reserved file {key!r}")


class FloorEntry(_StrictFloorModel):
    """One manifest row: a file's digest, size and the generation it last changed."""

    path: str
    content_digest: str = Field(pattern=_SHA256)
    byte_length: int = Field(ge=0)
    changed_at: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if floor_path_reserved(value):
            raise ValueError(f"floor manifest may not list {value}")
        return safe_floor_path(value)


def floor_inventory_digest(
    files: tuple[FloorEntry, ...] | list[dict[str, object]],
) -> str:
    """The root digest over the byte-sorted file inventory."""

    rows = [item.model_dump(mode="json") if isinstance(item, BaseModel) else item for item in files]
    return typed_digest(Sha256Value, FLOOR_FORMAT, {"files": rows}).tagged


class FloorManifest(_StrictFloorModel):
    """The root manifest: the coordinate, its generation, the renderer and every file.

    ``renderer`` names the rendering rule (format revision and compiler digest).
    Two floors with the same renderer and generation are the same bytes.
    """

    tag: Literal["playbill-floor-manifest-v6"] = "playbill-floor-manifest-v6"
    format: Literal["playbill-floor-export-v6"] = "playbill-floor-export-v6"
    renderer: str = Field(pattern=_SHA256)
    coordinate: AcceptedCoordinate
    generation: int = Field(ge=0)
    # Which review notes the change rationale was read from: the digest of every
    # rationale the floor's changes/ files show (``floor_notes_digest``).
    notes_digest: str = Field(pattern=_SHA256)
    files: tuple[FloorEntry, ...]
    floor_digest: str = Field(pattern=_SHA256)

    @model_validator(mode="after")
    def _inventory(self) -> FloorManifest:
        paths = [item.path for item in self.files]
        if paths != sorted(set(paths), key=lambda item: item.encode("utf-8")):
            raise ValueError("floor manifest inventory must be byte-sorted and unique")
        check_floor_paths(paths, label="a floor manifest")
        if any(item.changed_at > self.generation for item in self.files):
            raise ValueError("a floor file cannot change after the floor's generation")
        if self.floor_digest != floor_inventory_digest(self.files):
            raise ValueError("floor manifest root digest differs from its inventory")
        return self


def floor_notes_digest(rationales: Mapping[int, Iterable[str]]) -> str:
    """The identity of the review notes a floor read: every rationale it shows.

    A notes commit moves with every evaluation; this moves only when a
    rationale the floor shows does, which is the one notes change a floor
    can see.
    """

    return typed_digest(
        Sha256Value,
        "playbill-floor-notes-v1",
        {"rationales": [[sequence, list(rationales[sequence])] for sequence in sorted(rationales)]},
    ).tagged


def build_floor_manifest(
    *,
    renderer: str,
    coordinate: BaseModel,
    generation: int,
    notes_digest: str,
    files: dict[str, tuple[str, int, int]],
) -> FloorManifest:
    """Build the manifest from ``path -> (content_digest, byte_length, changed_at)``.

    ``coordinate`` is any accepted-coordinate model of the one wire shape.
    """

    entries = tuple(
        FloorEntry(path=path, content_digest=digest, byte_length=size, changed_at=changed)
        for path in sorted(files, key=lambda item: item.encode("utf-8"))
        for digest, size, changed in (files[path],)
    )
    return FloorManifest(
        renderer=renderer,
        coordinate=AcceptedCoordinate.model_validate(coordinate.model_dump(mode="json")),
        generation=generation,
        notes_digest=notes_digest,
        files=entries,
        floor_digest=floor_inventory_digest(entries),
    )


def render_floor_manifest(manifest: FloorManifest) -> bytes:
    """The exact ``manifest.json`` bytes both sides derive from the model."""

    value = json.loads(canonical_bytes(manifest.model_dump(mode="json")))
    return pretty_json(value).encode("utf-8") + b"\n"


def content_digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def floor_manifest_digest(manifest: FloorManifest) -> str:
    """The digest a delta names a floor by: the digest of its manifest bytes."""

    return content_digest(render_floor_manifest(manifest))


# -- the delta ------------------------------------------------------------------


class FloorHead(_StrictFloorModel):
    """The accepted coordinate a delta brings a floor to, and its generation."""

    git_oid: str = Field(pattern=_OID)
    generation: int = Field(ge=0)
    semantic_root: str = Field(pattern=_SHA256)
    generation_root: str = Field(pattern=_SHA256)
    compiler_digest: str = Field(pattern=_SHA256)
    # The review notes the head floor's change rationale was read from.
    notes_digest: str = Field(pattern=_SHA256)

    def coordinate(self) -> AcceptedCoordinate:
        return AcceptedCoordinate(
            git_oid=self.git_oid,
            semantic_root=self.semantic_root,
            generation_root=self.generation_root,
            compiler_digest=self.compiler_digest,
        )


class FloorDeltaFile(_StrictFloorModel):
    """One file the base lacks or holds at different bytes."""

    path: str
    content_b64: str
    sha256: str = Field(pattern=_SHA256)
    changed_at: int = Field(ge=0)

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        if floor_path_reserved(value):
            raise ValueError(f"a floor delta may not carry {value}")
        return safe_floor_path(value)

    def content(self) -> bytes:
        try:
            content = base64.b64decode(self.content_b64, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"floor delta file is not base64: {self.path}") from exc
        if content_digest(content) != self.sha256:
            raise ValueError(f"floor delta file differs from its digest: {self.path}")
        return content


class FloorDelta(_StrictFloorModel):
    """What brings a floor at ``base_generation`` to ``head``.

    ``kind="full"`` carries every file and no base; a receiver replaces its
    floor. ``kind="delta"`` carries the files whose ``changed_at`` is after the
    base, and ``tombstones``, the base's paths the head no longer has. Both are
    a deterministic function of (head, base) for one renderer.
    """

    tag: Literal["playbill-floor-delta-v1"] = "playbill-floor-delta-v1"
    kind: Literal["delta", "full"]
    renderer: str = Field(pattern=_SHA256)
    base_generation: int | None = Field(default=None, ge=0)
    head: FloorHead
    head_manifest_digest: str = Field(pattern=_SHA256)
    base_manifest_digest: str | None = Field(default=None, pattern=_SHA256)
    files: tuple[FloorDeltaFile, ...]
    tombstones: tuple[str, ...] = ()
    delta_digest: str = Field(pattern=_SHA256)

    @field_validator("tombstones")
    @classmethod
    def _tombstones(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for path in value:
            safe_floor_path(path)
            if floor_path_reserved(path):
                raise ValueError(f"a floor delta may not remove {path!r}: it is reserved")
        if list(value) != sorted(set(value), key=lambda item: item.encode("utf-8")):
            raise ValueError("floor delta tombstones must be byte-sorted and unique")
        return value

    @model_validator(mode="after")
    def _shape(self) -> FloorDelta:
        paths = [item.path for item in self.files]
        if paths != sorted(set(paths), key=lambda item: item.encode("utf-8")):
            raise ValueError("floor delta files must be byte-sorted and unique")
        # Writes and removals together name distinct files, none of them a
        # directory of another, on every filesystem.
        check_floor_paths((*paths, *self.tombstones), label="a floor delta")
        if self.kind == "full":
            if self.base_generation is not None or self.base_manifest_digest is not None:
                raise ValueError("a full floor names no base")
            if self.tombstones:
                raise ValueError("a full floor carries no tombstones")
        else:
            if self.base_generation is None or self.base_manifest_digest is None:
                raise ValueError("a floor delta names its base generation and manifest digest")
            if self.base_generation >= self.head.generation and (self.files or self.tombstones):
                raise ValueError("a floor delta from its own head changes nothing")
        if any(item.changed_at > self.head.generation for item in self.files):
            raise ValueError("a floor file cannot change after the delta's head")
        if self.delta_digest != floor_delta_digest(self):
            raise ValueError("floor delta digest differs from its content")
        return self


def floor_delta_digest(delta: FloorDelta | dict[str, object]) -> str:
    """The digest over everything in a delta except the digest itself."""

    payload = (
        delta.model_dump(mode="json", exclude={"delta_digest"})
        if isinstance(delta, BaseModel)
        else {key: value for key, value in delta.items() if key != "delta_digest"}
    )
    payload.pop("tag", None)
    return typed_digest(Sha256Value, "playbill-floor-delta-v1", payload).tagged


def seal_floor_delta(payload: dict[str, object]) -> FloorDelta:
    """Validate a delta payload after stamping its digest."""

    complete: dict[str, object] = {
        "base_generation": None,
        "base_manifest_digest": None,
        "tombstones": [],
        **payload,
    }
    return FloorDelta.model_validate({**complete, "delta_digest": floor_delta_digest(complete)})


class FloorApplyResult(_StrictFloorModel):
    """What one apply did to a floor directory.

    ``base_mismatch``: the directory does not hold the delta's base; nothing was
    written, and the caller asks for a full floor instead.
    """

    tag: Literal["playbill-floor-apply-result-v1"] = "playbill-floor-apply-result-v1"
    status: Literal["applied", "unchanged", "base_mismatch"]
    kind: Literal["delta", "full"]
    generation: int = Field(ge=0)
    manifest_digest: str | None = Field(default=None, pattern=_SHA256)
    floor_digest: str | None = Field(default=None, pattern=_SHA256)
    written: int = Field(default=0, ge=0)
    removed: int = Field(default=0, ge=0)
    file_count: int = Field(default=0, ge=0)
    message: str | None = None


__all__ = [
    "FLOOR_STAGING_NAME",
    "FLOOR_FORMAT",
    "FLOOR_LOCAL_PATHS",
    "FLOOR_MANIFEST_PATH",
    "FLOOR_MANIFEST_TAG",
    "FloorApplyResult",
    "FloorApplyResult",
    "FloorDeltaFile",
    "FloorDelta",
    "FloorEntry",
    "FloorHead",
    "FloorManifest",
    "build_floor_manifest",
    "content_digest",
    "floor_delta_digest",
    "floor_inventory_digest",
    "floor_manifest_digest",
    "floor_notes_digest",
    "render_floor_manifest",
    "check_floor_paths",
    "floor_path_key",
    "floor_path_reserved",
    "safe_floor_path",
    "seal_floor_delta",
]
