"""Cruxible artifacts as OCI artifacts, and the on-disk OCI image layout.

An artifact is a manifest naming one config blob and its layer blobs, each by
sha256 digest. The manifest carries no timestamps or annotations of its own, so
the same content always has the same manifest digest -- the digest a consumer
pins. Which kind of artifact it is (a kit today; an instance snapshot later) is
declared by an ``ArtifactKind``; nothing here knows any kind's contents.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar

MANIFEST_MEDIA_TYPE = "application/vnd.oci.image.manifest.v1+json"
INDEX_MEDIA_TYPE = "application/vnd.oci.image.index.v1+json"
LAYOUT_FILE = "oci-layout"
REF_ANNOTATION = "org.opencontainers.image.ref.name"

T = TypeVar("T")


def sha256_digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


@dataclass(frozen=True)
class Blob:
    media_type: str
    content: bytes

    @property
    def digest(self) -> str:
        return sha256_digest(self.content)

    def descriptor(self) -> dict[str, object]:
        return {"mediaType": self.media_type, "digest": self.digest, "size": len(self.content)}


@dataclass(frozen=True)
class ArtifactKind(Generic[T]):
    """How one kind of Cruxible artifact packs into a config blob and layers.

    ``unpack`` receives bytes whose digests are already verified and must
    verify everything about its own content that it relies on.
    """

    name: str
    artifact_type: str
    config_media_type: str
    layer_media_type: str
    pack: Callable[[T], tuple[bytes, tuple[bytes, ...]]]
    unpack: Callable[[bytes, tuple[bytes, ...]], T]


# Reserved for the instance snapshot a deploy will move (ledger, exhaust journal,
# definitions); no kind is registered under it yet.
INSTANCE_ARTIFACT_TYPE = "application/vnd.cruxible.instance.v1"


@dataclass(frozen=True)
class ArtifactImage:
    """One manifest and every blob it names, all digest-checked."""

    manifest: bytes
    config: Blob
    layers: tuple[Blob, ...]

    @property
    def digest(self) -> str:
        return sha256_digest(self.manifest)

    @property
    def artifact_type(self) -> str:
        value = json.loads(self.manifest).get("artifactType")
        return value if isinstance(value, str) else ""

    def blobs(self) -> tuple[Blob, ...]:
        return (self.config, *self.layers)


def pack_artifact(kind: ArtifactKind[T], value: T) -> ArtifactImage:
    config, layers = kind.pack(value)
    config_blob = Blob(kind.config_media_type, config)
    layer_blobs = tuple(Blob(kind.layer_media_type, layer) for layer in layers)
    manifest = canonical_json(
        {
            "schemaVersion": 2,
            "mediaType": MANIFEST_MEDIA_TYPE,
            "artifactType": kind.artifact_type,
            "config": config_blob.descriptor(),
            "layers": [blob.descriptor() for blob in layer_blobs],
        }
    )
    return ArtifactImage(manifest=manifest, config=config_blob, layers=layer_blobs)


def manifest_descriptors(manifest: bytes) -> tuple[dict[str, Any], tuple[dict[str, Any], ...]]:
    """The config and layer descriptors of one manifest; any other shape refuses."""

    value = json.loads(manifest)
    if not isinstance(value, dict) or value.get("schemaVersion") != 2:
        raise ValueError("artifact manifest is not an OCI image manifest")
    if value.get("mediaType") != MANIFEST_MEDIA_TYPE:
        raise ValueError("artifact manifest has an unexpected media type")
    config = value.get("config")
    layers = value.get("layers")
    if not isinstance(config, dict) or not isinstance(layers, list):
        raise ValueError("artifact manifest lacks config or layers")
    for descriptor in (config, *layers):
        if not isinstance(descriptor, dict) or not all(
            isinstance(descriptor.get(key), expected)
            for key, expected in (("mediaType", str), ("digest", str), ("size", int))
        ):
            raise ValueError("artifact manifest has a malformed descriptor")
        if not str(descriptor["digest"]).startswith("sha256:"):
            raise ValueError("artifact blobs must be sha256-addressed")
    return config, tuple(layers)


def assemble_image(manifest: bytes, fetch: Callable[[dict[str, Any]], bytes]) -> ArtifactImage:
    """Fetch every blob a manifest names and check each against its descriptor."""

    config, layers = manifest_descriptors(manifest)

    def blob(descriptor: dict[str, Any]) -> Blob:
        content = fetch(descriptor)
        if len(content) != descriptor["size"] or sha256_digest(content) != descriptor["digest"]:
            raise ValueError(f"blob {descriptor['digest']} does not match its descriptor")
        return Blob(descriptor["mediaType"], content)

    return ArtifactImage(
        manifest=manifest, config=blob(config), layers=tuple(blob(item) for item in layers)
    )


def unpack_artifact(kind: ArtifactKind[T], image: ArtifactImage) -> T:
    if image.artifact_type != kind.artifact_type:
        raise ValueError(
            f"artifact is {image.artifact_type or 'untyped'}, not a {kind.name} "
            f"({kind.artifact_type})"
        )
    if image.config.media_type != kind.config_media_type or any(
        layer.media_type != kind.layer_media_type for layer in image.layers
    ):
        raise ValueError(f"artifact media types do not match a {kind.name}")
    return kind.unpack(image.config.content, tuple(layer.content for layer in image.layers))


def _blob_path(root: Path, digest: str) -> Path:
    algorithm, _, value = digest.partition(":")
    if algorithm != "sha256" or len(value) != 64 or not all(c in "0123456789abcdef" for c in value):
        raise ValueError(f"{digest} is not a sha256 digest")
    return root / "blobs" / "sha256" / value


def _write_atomic(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def write_layout(image: ArtifactImage, root: Path, *, ref: str | None = None) -> None:
    """Write ``image`` as an OCI image layout directory (the offline transport)."""

    root.mkdir(parents=True, exist_ok=True)
    _write_atomic(root / LAYOUT_FILE, canonical_json({"imageLayoutVersion": "1.0.0"}))
    for blob in (*image.blobs(), Blob(MANIFEST_MEDIA_TYPE, image.manifest)):
        _write_atomic(_blob_path(root, blob.digest), blob.content)
    descriptor: dict[str, object] = {
        "mediaType": MANIFEST_MEDIA_TYPE,
        "digest": image.digest,
        "size": len(image.manifest),
        "artifactType": image.artifact_type,
    }
    if ref is not None:
        descriptor["annotations"] = {REF_ANNOTATION: ref}
    _write_atomic(
        root / "index.json",
        canonical_json(
            {"schemaVersion": 2, "mediaType": INDEX_MEDIA_TYPE, "manifests": [descriptor]}
        ),
    )


def read_layout(root: Path) -> ArtifactImage:
    """Read the one artifact in an OCI image layout, checking every digest."""

    if not (root / LAYOUT_FILE).is_file():
        raise ValueError(f"{root} is not an OCI image layout")
    index = json.loads((root / "index.json").read_bytes())
    manifests = index.get("manifests") if isinstance(index, dict) else None
    if not isinstance(manifests, list) or len(manifests) != 1:
        raise ValueError(f"{root} must hold exactly one artifact")
    descriptor = manifests[0]
    manifest = _blob_path(root, str(descriptor.get("digest"))).read_bytes()
    if sha256_digest(manifest) != descriptor.get("digest"):
        raise ValueError(f"{root} manifest does not match its index digest")
    return assemble_image(manifest, lambda item: _blob_path(root, str(item["digest"])).read_bytes())


def is_layout(path: Path) -> bool:
    return (path / LAYOUT_FILE).is_file()
