"""Portable authored views: compact page plus immutable declaration manifests.

Local installation is derived, not governed retention. ``retention_value`` is
an exact-content value for a normal reviewed Claim; accepting that Claim is
what makes the complete package recoverable from the ledger and its CAS.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from cruxible_client.authoring.sdk_types import ExactContent
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.declared_blocks import (
    MAX_PROJECTION_SOURCE_BYTES,
    MAX_PROJECTION_STAMP_BYTES,
    ProjectionMarkerError,
    discover_projection_blocks,
    projection_manifest_refs,
    resolve_projection_manifest_digest,
)


def load_projection_manifests(workspace: Path, content: bytes) -> dict[str, bytes]:
    root = workspace.resolve()
    result = {}
    total = 0
    directory = root / ".playbill/manifests"
    for ref in projection_manifest_refs(content):
        if ref.startswith("sha256:"):
            digest = ref
        else:
            digest = resolve_projection_manifest_digest(
                ref, ("sha256:" + path.stem for path in directory.glob(ref + "*.json"))
            )
        path = directory / (digest.removeprefix("sha256:") + ".json")
        try:
            if not path.resolve(strict=True).is_relative_to(root) or path.is_symlink():
                raise ProjectionMarkerError("projection manifest path escapes its workspace")
            with path.open("rb") as stream:
                body = stream.read(MAX_PROJECTION_STAMP_BYTES + 1)
        except OSError as exc:
            raise ProjectionMarkerError(f"projection manifest is unavailable: {digest}") from exc
        total += len(body)
        if len(body) > MAX_PROJECTION_STAMP_BYTES or total > MAX_PROJECTION_SOURCE_BYTES:
            raise ProjectionMarkerError("projection manifest package exceeds its byte ceiling")
        if "sha256:" + hashlib.sha256(body).hexdigest() != digest:
            raise ProjectionMarkerError("projection manifest digest does not reproduce")
        result[digest] = body
    return result


def retain_local_manifests(workspace: Path, manifests: Mapping[str, bytes]) -> None:
    if not manifests:
        return
    root = workspace.resolve()
    directory = root / ".playbill/manifests"
    if not directory.resolve().is_relative_to(root):
        raise ProjectionMarkerError("projection manifest directory escapes its workspace")
    directory.mkdir(parents=True, exist_ok=True)
    for digest, content in manifests.items():
        if digest != "sha256:" + hashlib.sha256(content).hexdigest():
            raise ProjectionMarkerError("projection manifest digest does not reproduce")
        path = directory / (digest.removeprefix("sha256:") + ".json")
        if path.exists() or path.is_symlink():
            if path.is_symlink() or path.read_bytes() != content:
                raise ProjectionMarkerError("existing immutable projection manifest is corrupt")
            continue
        fd, temporary = tempfile.mkstemp(prefix=".manifest-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)


@dataclass(frozen=True)
class ProjectionPackage:
    content: bytes
    manifests: Mapping[str, bytes]

    def __post_init__(self) -> None:
        # Resolve before exposing a usable package; missing manifests never
        # downgrade a declaration into bootstrap prose.
        blocks = discover_projection_blocks(self.content, manifests=self.manifests)
        if any(
            block.stamp is None or block.stamp.body_digest != block.body_digest for block in blocks
        ):
            raise ProjectionMarkerError("projection package body differs from its declaration")
        referenced = {
            resolve_projection_manifest_digest(ref, self.manifests)
            for ref in projection_manifest_refs(self.content)
        }
        if set(self.manifests) != referenced:
            raise ProjectionMarkerError("projection package contains unrelated manifests")
        object.__setattr__(self, "manifests", MappingProxyType(dict(self.manifests)))

    @classmethod
    def read(cls, workspace: str | Path, path: str | Path) -> ProjectionPackage:
        root = Path(workspace).resolve()
        source = (root / path).resolve(strict=True)
        if not source.is_relative_to(root):
            raise ProjectionMarkerError("projection page escapes its workspace")
        with source.open("rb") as stream:
            content = stream.read(MAX_PROJECTION_SOURCE_BYTES + 1)
        return cls(content, load_projection_manifests(root, content))

    def to_bytes(self) -> bytes:
        # Validate again because mappings supplied by callers may be mutable.
        discover_projection_blocks(self.content, manifests=self.manifests)
        return canonical_bytes(
            {
                "tag": "playbill-projection-package-v1",
                "content_base64": base64.b64encode(self.content).decode("ascii"),
                "manifests": {
                    key: base64.b64encode(body).decode("ascii")
                    for key, body in self.manifests.items()
                },
            }
        )

    @classmethod
    def from_bytes(cls, content: bytes) -> ProjectionPackage:
        if len(content) > 12 * 1024 * 1024:
            raise ProjectionMarkerError("projection package archive exceeds its byte ceiling")
        try:
            value = json.loads(content)
            if (
                canonical_bytes(value) != content
                or set(value) != {"tag", "content_base64", "manifests"}
                or value["tag"] != "playbill-projection-package-v1"
            ):
                raise ValueError("noncanonical package")
            package = cls(
                base64.b64decode(value["content_base64"], validate=True),
                {
                    key: base64.b64decode(body, validate=True)
                    for key, body in value["manifests"].items()
                },
            )
            if package.to_bytes() != content:
                raise ValueError("noncanonical package encoding")
            return package
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ProjectionMarkerError("projection package is malformed") from exc

    def retention_value(self) -> ExactContent:
        """Stage this in a reviewed exact-content Claim to obtain ledger retention."""
        return ExactContent(self.to_bytes())

    def install(self, workspace: str | Path, path: str | Path) -> Path:
        from cruxible_client.authoring.insertions import replace_publication_file

        root = Path(workspace).resolve()
        target = root / path
        if target.is_symlink() or not target.resolve().is_relative_to(root):
            raise ProjectionMarkerError("projection page escapes its workspace")
        discover_projection_blocks(self.content, manifests=self.manifests)
        retain_local_manifests(root, self.manifests)
        # A workspace may already retain another manifest with this short prefix.
        load_projection_manifests(root, self.content)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            # Exclusive creation preserves an independently authored page.
            with target.open("xb") as stream:
                stream.write(self.content)
        else:
            replace_publication_file(target, expected=target.read_bytes(), replacement=self.content)
        return target
