"""Local projection manifests: the immutable declarations a compact page references.

A compact projection block names its declaration by digest; the manifest bytes
live beside the workspace under ``.cruxible/manifests``. They are derived local
files, not governed retention.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from cruxible_client.contracts.declared_blocks import (
    ProjectionMarkerError,
    check_projection_processing_bytes,
    projection_manifest_refs,
    projection_processing_policy,
    read_projection_source,
    resolve_projection_manifest_digest,
)
from cruxible_client.contracts.workspace_layout import workspace_path


def load_projection_manifests(workspace: Path, content: bytes) -> dict[str, bytes]:
    root = workspace.resolve()
    result = {}
    total = 0
    directory = workspace_path(root, "manifests")
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
                body = stream.read(projection_processing_policy().max_bytes - total + 1)
        except OSError as exc:
            raise ProjectionMarkerError(f"projection manifest is unavailable: {digest}") from exc
        total += len(body)
        check_projection_processing_bytes(total + len(content))
        if "sha256:" + hashlib.sha256(body).hexdigest() != digest:
            raise ProjectionMarkerError("projection manifest digest does not reproduce")
        result[digest] = body
    return result


def retain_local_manifests(workspace: Path, manifests: Mapping[str, bytes]) -> None:
    if not manifests:
        return
    root = workspace.resolve()
    directory = workspace_path(root, "manifests")
    if not directory.resolve().is_relative_to(root):
        raise ProjectionMarkerError("projection manifest directory escapes its workspace")
    directory.mkdir(parents=True, exist_ok=True)
    for digest, content in manifests.items():
        if digest != "sha256:" + hashlib.sha256(content).hexdigest():
            raise ProjectionMarkerError("projection manifest digest does not reproduce")
        path = directory / (digest.removeprefix("sha256:") + ".json")
        if path.exists() or path.is_symlink():
            if path.is_symlink() or read_projection_source(path) != content:
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
