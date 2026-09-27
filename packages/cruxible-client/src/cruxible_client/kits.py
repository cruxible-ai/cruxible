"""The kit directory: a manifest beside the exact bytes of every artifact it names.

::

    <kit>/
      cruxible-kit.json
      artifacts/claim-types/acme.account/seats.json
      ...

The directory is what a kit repository reviews. Distributed, a kit is an OCI
artifact (``application/vnd.cruxible.kit.v1``): the manifest as its config blob
and one deterministic tar of the artifacts as its layer, so rebuilding a release
gives the same digest. A kit can come from a directory, an OCI image layout or a
registry reference; either way the daemon only ever receives the bundle.
"""

from __future__ import annotations

from pathlib import Path

from cruxible_client.artifacts import (
    ArtifactImage,
    ArtifactKind,
    Reference,
    RegistryClient,
    is_layout,
    pack_artifact,
    pack_files,
    parse_reference,
    read_layout,
    unpack_artifact,
    unpack_files,
)
from cruxible_client.contracts.canonical import pretty_canonical_bytes
from cruxible_client.contracts.kits import (
    KIT_ARTIFACT_DIRECTORY,
    KIT_MANIFEST_FILE,
    KitArtifactBytesV1,
    KitBundleV1,
    KitManifestV1,
)


def read_kit_directory(root: Path) -> KitBundleV1:
    """Read one kit directory; every file under ``artifacts/`` must be in the manifest."""

    manifest = KitManifestV1.model_validate_json((root / KIT_MANIFEST_FILE).read_bytes())
    artifact_root = root / KIT_ARTIFACT_DIRECTORY
    present = sorted(
        path.relative_to(artifact_root).as_posix()
        for path in artifact_root.rglob("*")
        if path.is_file() or path.is_symlink()
    )
    named = [item.path for item in manifest.artifacts]
    if present != named:
        extra = sorted(set(present) - set(named))
        missing = sorted(set(named) - set(present))
        raise ValueError(
            f"kit directory {root} does not match its manifest "
            f"(unlisted: {extra or 'none'}; missing: {missing or 'none'})"
        )
    artifacts = []
    for name in named:
        path = artifact_root / name
        if path.is_symlink():
            raise ValueError(f"kit artifact {name} is a symbolic link")
        artifacts.append(KitArtifactBytesV1.of(name, path.read_bytes()))
    return KitBundleV1(manifest=manifest, artifacts=tuple(artifacts))


def write_kit_directory(bundle: KitBundleV1, root: Path) -> None:
    """Write a bundle as a new kit directory; an existing directory is refused."""

    root.mkdir(parents=True, exist_ok=False)
    (root / KIT_MANIFEST_FILE).write_bytes(
        pretty_canonical_bytes(bundle.manifest.model_dump(mode="json"))
    )
    artifact_root = (root / KIT_ARTIFACT_DIRECTORY).resolve()
    for item in bundle.artifacts:
        target = artifact_root / item.path
        # The contract already refuses traversal; containment is checked again
        # here because this helper writes wherever a caller points it.
        if not target.resolve().is_relative_to(artifact_root):
            raise ValueError(f"kit artifact {item.path} escapes the kit directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(item.content)


def _pack_kit(bundle: KitBundleV1) -> tuple[bytes, tuple[bytes, ...]]:
    config = pretty_canonical_bytes(bundle.manifest.model_dump(mode="json"))
    return config, (pack_files(bundle.contents()),)


def _unpack_kit(config: bytes, layers: tuple[bytes, ...]) -> KitBundleV1:
    if len(layers) != 1:
        raise ValueError("a kit artifact has exactly one layer")
    manifest = KitManifestV1.model_validate_json(config)
    files = unpack_files(layers[0])
    return KitBundleV1(
        manifest=manifest,
        artifacts=tuple(KitArtifactBytesV1.of(path, files[path]) for path in sorted(files)),
    )


KIT_ARTIFACT: ArtifactKind[KitBundleV1] = ArtifactKind(
    name="kit",
    artifact_type="application/vnd.cruxible.kit.v1",
    config_media_type="application/vnd.cruxible.kit.manifest.v1+json",
    layer_media_type="application/vnd.cruxible.kit.artifacts.v1.tar",
    pack=_pack_kit,
    unpack=_unpack_kit,
)


def fetch_kit_image(
    source: str, *, registry: RegistryClient | None = None
) -> tuple[ArtifactImage, str]:
    """The verified kit artifact at a registry reference, exactly as published."""

    ref = parse_reference(source)
    client = registry if registry is not None else RegistryClient()
    try:
        image = client.pull(ref)
    finally:
        if registry is None:
            client.close()
    unpack_artifact(KIT_ARTIFACT, image)
    return image, str(ref.pinned(image.digest))


def resolve_kit(source: str, *, registry: RegistryClient | None = None) -> tuple[KitBundleV1, str]:
    """A bundle from a kit directory, an OCI layout, or a registry reference.

    Returns the bundle and where it came from; a registry source is reported
    pinned to the manifest digest actually pulled.
    """

    path = Path(source).expanduser()
    if path.is_dir():
        if is_layout(path):
            image = read_layout(path)
            return unpack_artifact(KIT_ARTIFACT, image), f"{path.name}@{image.digest}"
        return read_kit_directory(path), path.name
    image, origin = fetch_kit_image(source, registry=registry)
    return unpack_artifact(KIT_ARTIFACT, image), origin


def push_kit(bundle: KitBundleV1, ref: Reference, *, registry: RegistryClient) -> str:
    """Publish ``bundle`` at ``ref``; returns the manifest digest consumers pin."""

    return registry.push(pack_artifact(KIT_ARTIFACT, bundle), ref)
