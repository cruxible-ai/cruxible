"""The kit directory: a manifest beside the exact bytes of every artifact it names.

::

    <kit>/
      cruxible-kit.json
      artifacts/claim-types/acme.account/seats.json
      ...

The directory is what a kit repository reviews and what the OCI layer packs; the
daemon only ever receives the bundle read from it.
"""

from __future__ import annotations

from pathlib import Path

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
