"""The kit directory: a manifest beside the exact bytes of every artifact it names.

::

    <kit>/
      cruxible-kit.json
      artifacts/claim-types/acme.account/seats.json
      ...
      providers/acme_parser-1.0.0-py3-none-any.whl     (bundled provider packages:
      providers/acme-parser-1.0.0.uv.lock               built wheels and their locks)

The directory is what a kit repository reviews. Distributed, a kit is an OCI
artifact (``application/vnd.cruxible.kit.v1``): the manifest as its config blob
and one deterministic tar of the artifacts (and, under ``providers/``, the
bundled provider files) as its layer, so rebuilding a release gives the same
digest. A kit can come from a directory, an OCI image layout or a registry
reference; either way the daemon receives the bundle, and the bundled provider
files reach it through its body store (``stage_kit_providers``).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from pydantic import ValidationError

from cruxible_client.artifacts import (
    ArtifactImage,
    ArtifactKind,
    Reference,
    RegistryClient,
    is_layout,
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
    KIT_PROVIDER_DIRECTORY,
    KitArtifactBytes,
    KitBundle,
    KitManifest,
    KitProviderFileBytes,
    KitStatus,
    kit_version_key,
)
from cruxible_client.contracts.validation_messages import validation_summary
from cruxible_client.errors import ConfigError

if TYPE_CHECKING:
    from cruxible_client.transport.http import CruxibleClient

_KIT_FORMS = (
    "a kit directory (holding cruxible-kit.json), an OCI image layout, or a registry "
    "reference such as project-state:1.0.0 or ghcr.io/acme/kits/foo@sha256:..."
)


class KitSourceError(ConfigError):
    """A kit source names no kit this client can read (a typed refusal, not a crash)."""

    error_code = "cruxible.kit.source_invalid"

    def __init__(self, detail: str) -> None:
        super().__init__(f"{self.error_code}: {detail}; a kit is {_KIT_FORMS}")


def kit_reference(source: str) -> Reference:
    """``source`` as a registry reference, or a typed refusal naming the kit forms."""

    try:
        return parse_reference(source)
    except ValueError as exc:
        raise KitSourceError(f"{source!r} is not a registry reference ({exc})") from exc


def read_kit_directory(root: Path) -> KitBundle:
    """Read one kit directory; every file under ``artifacts/`` must be in the manifest."""

    manifest = KitManifest.model_validate_json((root / KIT_MANIFEST_FILE).read_bytes())
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
        artifacts.append(KitArtifactBytes.of(name, path.read_bytes()))
    provider_root = root / KIT_PROVIDER_DIRECTORY
    bundled = sorted(manifest.provider_files())
    held = sorted(
        path.relative_to(provider_root).as_posix()
        for path in (provider_root.rglob("*") if provider_root.is_dir() else ())
        if path.is_file() or path.is_symlink()
    )
    if held != bundled:
        extra = sorted(set(held) - set(bundled))
        missing = sorted(set(bundled) - set(held))
        raise ValueError(
            f"kit directory {root} does not hold exactly its manifest's provider files "
            f"(unlisted: {extra or 'none'}; missing: {missing or 'none'})"
        )
    files = []
    for name in bundled:
        path = provider_root / name
        if path.is_symlink():
            raise ValueError(f"kit provider file {name} is a symbolic link")
        files.append(KitProviderFileBytes.of(name, path.read_bytes()))
    return KitBundle(manifest=manifest, artifacts=tuple(artifacts), provider_files=tuple(files))


def write_kit_directory(bundle: KitBundle, root: Path) -> None:
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
    if bundle.manifest.provider_files() and not bundle.provider_files:
        raise ValueError("this bundle names provider packages but carries none of their bytes")
    for provider_file in bundle.provider_files:
        target = root / KIT_PROVIDER_DIRECTORY / provider_file.filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(provider_file.content)


_PROVIDER_PREFIX = KIT_PROVIDER_DIRECTORY + "/"


def _pack_kit(bundle: KitBundle) -> tuple[bytes, tuple[bytes, ...]]:
    if bundle.manifest.provider_files() and not bundle.provider_files:
        raise ValueError("this bundle names provider packages but carries none of their bytes")
    config = pretty_canonical_bytes(bundle.manifest.model_dump(mode="json"))
    files = {
        **bundle.contents(),
        **{
            _PROVIDER_PREFIX + name: content for name, content in bundle.provider_contents().items()
        },
    }
    return config, (pack_files(files),)


def _unpack_kit(config: bytes, layers: tuple[bytes, ...]) -> KitBundle:
    if len(layers) != 1:
        raise ValueError("a kit artifact has exactly one layer")
    manifest = KitManifest.model_validate_json(config)
    files = unpack_files(layers[0])
    return KitBundle(
        manifest=manifest,
        artifacts=tuple(
            KitArtifactBytes.of(path, files[path])
            for path in sorted(files)
            if not path.startswith(_PROVIDER_PREFIX)
        ),
        provider_files=tuple(
            KitProviderFileBytes.of(path.removeprefix(_PROVIDER_PREFIX), files[path])
            for path in sorted(files)
            if path.startswith(_PROVIDER_PREFIX)
        ),
    )


def stage_kit_providers(client: CruxibleClient, instance_id: str, bundle: KitBundle) -> KitBundle:
    """Store each bundled provider file in the daemon's body store; the bundle without them.

    ``kit add`` reads the files there under their sha256, so the request carries
    only the manifest's references (the same transfer ``provider install WHEEL``
    makes).
    """

    if bundle.manifest.provider_files() and not bundle.provider_files:
        raise KitSourceError("the kit names provider packages but carries none of their bytes")
    for item in bundle.provider_files:
        stored = client.store_body(instance_id, item.content)
        if stored.digest != bundle.manifest.provider_files()[item.filename]:
            raise ConfigError(f"the daemon stored {item.filename} under another digest")
    return bundle.model_copy(update={"provider_files": ()})


KIT_ARTIFACT: ArtifactKind[KitBundle] = ArtifactKind(
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

    ref = kit_reference(source)
    client = registry if registry is not None else RegistryClient()
    try:
        image = client.pull(ref)
    finally:
        if registry is None:
            client.close()
    unpack_artifact(KIT_ARTIFACT, image)
    return image, str(ref.pinned(image.digest))


def resolve_kit(source: str, *, registry: RegistryClient | None = None) -> tuple[KitBundle, str]:
    """A bundle from a kit directory, an OCI layout, or a registry reference.

    Returns the bundle and where it came from; a registry source is reported
    pinned to the manifest digest actually pulled.
    """

    path = Path(source).expanduser()
    if path.is_dir():
        if is_layout(path):
            image = read_layout(path)
            return unpack_artifact(KIT_ARTIFACT, image), f"{path.name}@{image.digest}"
        if not (path / KIT_MANIFEST_FILE).is_file():
            raise KitSourceError(f"directory {path} holds no {KIT_MANIFEST_FILE}")
        try:
            return read_kit_directory(path), path.name
        except ValidationError as exc:
            raise KitSourceError(
                f"{path / KIT_MANIFEST_FILE} is not a kit manifest: {validation_summary(exc)}"
            ) from exc
        except ValueError as exc:
            raise KitSourceError(str(exc)) from exc
    if path.exists():
        raise KitSourceError(f"{path} is a file, not a kit directory or layout")
    image, origin = fetch_kit_image(source, registry=registry)
    return unpack_artifact(KIT_ARTIFACT, image), origin


#: How long the update check waits on a registry before reporting it unavailable.
UPDATE_CHECK_TIMEOUT = 5.0


def registry_source(source: str | None) -> bool:
    """Whether a recorded kit source is a registry reference (directory and layout
    sources are a bare name)."""

    return source is not None and "/" in source


def latest_release(tags: tuple[str, ...]) -> str | None:
    """The highest MAJOR.MINOR.PATCH tag; other tags (``latest``) are ignored."""

    versions = []
    for tag in tags:
        try:
            versions.append((kit_version_key(tag), tag))
        except ValueError:
            continue
    return None if not versions else max(versions)[1]


def check_kit_updates(
    status: KitStatus,
    *,
    offline: bool = False,
    registry: RegistryClient | None = None,
) -> KitStatus:
    """Fill each registry-sourced kit's latest available version from the registry's tags.

    Client-side and best effort: a registry that cannot answer within a short
    timeout reports ``unavailable``; ``offline`` skips the check. Kits from a
    directory or layout have nothing to check.
    """

    client = registry
    checked = []
    try:
        for kit in status.kits:
            if not registry_source(kit.source):
                checked.append(kit.model_copy(update={"update_check": "local_source"}))
                continue
            if offline:
                checked.append(kit.model_copy(update={"update_check": "offline"}))
                continue
            assert kit.source is not None
            try:
                ref = kit_reference(kit.source.partition("@")[0])
                if client is None:
                    client = RegistryClient(timeout=UPDATE_CHECK_TIMEOUT)
                latest = latest_release(client.list_tags(ref))
            except (OSError, ValueError, ConfigError, httpx.HTTPError):
                checked.append(kit.model_copy(update={"update_check": "unavailable"}))
                continue
            checked.append(
                kit.model_copy(update={"latest_available": latest, "update_check": "checked"})
            )
    finally:
        if registry is None and client is not None:
            client.close()
    return status.model_copy(update={"kits": tuple(checked)})
