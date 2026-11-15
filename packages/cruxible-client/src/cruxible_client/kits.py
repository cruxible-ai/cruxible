"""The kit directory: a manifest beside the exact bytes of every artifact it names.

::

    <kit>/
      cruxible-kit.json
      artifacts/claim-types/acme.account/seats.json
      ...
      providers/acme_parser-1.0.0-py3-none-any.whl     (bundled provider packages:
      providers/acme-parser-1.0.0.uv.lock               built wheels and their locks)
      README.md                                         (inert files the manifest
      examples/...                                       lists: README, reference,
      skills/<name>/SKILL.md                             examples and agent skills)

The directory is what a kit repository reviews. Distributed, a kit is an OCI
artifact (``application/vnd.cruxible.kit.v1``): the manifest as its config blob
and one deterministic tar of the artifacts (under ``providers/``, the bundled
provider files; under ``files/``, the inert files) as its layer, so rebuilding a
release gives the same digest. Inert files stay out of the release identity and
never reach the daemon: ``kit pull`` writes them into the kit directory and
``write_kit_docs`` / ``install_kit_skills`` put them where a reader or agent
looks, and ``kit add`` returns the automation template's text
(``kit_automation_template``). A kit can come from a directory, an OCI image layout or a registry
reference; either way the daemon receives the bundle, and the bundled provider
files reach it through its body store (``stage_kit_providers``).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from pydantic import ValidationError

from cruxible_client._safe_files import ContainmentError, HeldRoot, SafeFileReadError
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
from cruxible_client.artifacts.registry import RegistryRefusal
from cruxible_client.contracts.canonical import Sha256Value, pretty_canonical_bytes
from cruxible_client.contracts.kits import (
    KIT_ARTIFACT_DIRECTORY,
    KIT_AUTOMATION_TEMPLATE_MAX_BYTES,
    KIT_DOCUMENT_BODY_DIRECTORY,
    KIT_FILE_DIRECTORY,
    KIT_MANIFEST_FILE,
    KIT_PROVIDER_DIRECTORY,
    KitArtifactBytes,
    KitAutomationTemplate,
    KitBundle,
    KitChangeResult,
    KitDocumentBody,
    KitFileBytes,
    KitManifest,
    KitProviderFileBytes,
    KitStatus,
    kit_document_body_digests,
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

    if source.startswith((".", "/", "~")):
        # A path, never a registry name. Say what is actually on disk: nothing at
        # all, or a local kit handed to a caller that only fetches from a registry.
        if Path(source).expanduser().exists():
            raise KitSourceError(
                f"{source} is a local path, not a registry reference; read it as a kit "
                "directory or layout (resolve_kit) instead of fetching it"
            )
        raise KitSourceError(f"no kit directory or layout at {source} (nothing exists there)")
    try:
        return parse_reference(source)
    except ValueError as exc:
        raise KitSourceError(f"{source!r} is not a registry reference ({exc})") from exc


def read_kit_directory(root: Path) -> KitBundle:
    """Read one kit directory; every file under ``artifacts/`` must be in the manifest.

    Every read and listing walks beneath the directory from held descriptors
    (``HeldRoot``): a symbolic link anywhere inside the kit is refused, and a
    parent swapped for one mid-read is never followed.
    """

    with HeldRoot(root) as held:
        manifest = KitManifest.model_validate_json(held.read(KIT_MANIFEST_FILE))
        present = held.files(KIT_ARTIFACT_DIRECTORY, missing_ok=True)
        named = [item.path for item in manifest.artifacts]
        if present != sorted(named):
            extra = sorted(set(present) - set(named))
            missing = sorted(set(named) - set(present))
            raise ValueError(
                f"kit directory {root} does not match its manifest "
                f"(unlisted: {extra or 'none'}; missing: {missing or 'none'})"
            )
        artifacts = [
            KitArtifactBytes.of(name, held.read(f"{KIT_ARTIFACT_DIRECTORY}/{name}"))
            for name in named
        ]
        bundled = sorted(manifest.provider_files())
        provided = held.files(KIT_PROVIDER_DIRECTORY, missing_ok=True)
        if provided != bundled:
            extra = sorted(set(provided) - set(bundled))
            missing = sorted(set(bundled) - set(provided))
            raise ValueError(
                f"kit directory {root} does not hold exactly its manifest's provider files "
                f"(unlisted: {extra or 'none'}; missing: {missing or 'none'})"
            )
        files = [
            KitProviderFileBytes.of(name, held.read(f"{KIT_PROVIDER_DIRECTORY}/{name}"))
            for name in bundled
        ]
        return KitBundle(
            manifest=manifest,
            artifacts=tuple(artifacts),
            provider_files=tuple(files),
            files=_read_kit_files(held, manifest),
            document_bodies=_read_document_bodies(
                held, {item.path: item.content for item in artifacts}
            ),
        )


def _read_document_bodies(
    held: HeldRoot, artifacts: Mapping[str, bytes]
) -> tuple[KitDocumentBody, ...]:
    """Each governed Document's body, read at ``bodies/<sha256 hex>``."""

    wanted = sorted(set(kit_document_body_digests(artifacts).values()))
    bodies = []
    for digest in wanted:
        name = digest.partition(":")[2]
        try:
            content = held.read(f"{KIT_DOCUMENT_BODY_DIRECTORY}/{name}")
        except (FileNotFoundError, SafeFileReadError) as exc:
            raise ValueError(f"kit directory {held.root} lacks the Document body {digest}") from exc
        body = KitDocumentBody.of(content)
        if body.digest != digest:
            raise ValueError(f"kit Document body {name} differs from its digest")
        bodies.append(body)
    return tuple(bodies)


def _read_kit_files(held: HeldRoot, manifest: KitManifest) -> tuple[KitFileBytes, ...]:
    """The inert files the manifest lists, read at their own paths under the kit.

    Other files beside them (a kit repository's sources and tests) are not kit
    content and are never read.
    """

    found = []
    missing = []
    for item in manifest.files:
        try:
            content = held.read(item.path)
        except ContainmentError as exc:
            raise ValueError(f"kit file {item.path} is (or sits under) a symbolic link") from exc
        except (FileNotFoundError, SafeFileReadError):
            missing.append(item.path)
            continue
        found.append(KitFileBytes.of(item.path, content))
    if missing:
        raise ValueError(f"kit directory {held.root} lacks the manifest's kit files {missing}")
    return tuple(found)


def _write_new_file(root: Path, relative: str, content: bytes) -> None:
    """Write one file under ``root`` (contained, never following a link, never replacing)."""

    resolved_root = root.resolve()
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.parent.resolve().is_relative_to(resolved_root):
        raise ValueError(f"{relative} escapes {root}")
    # ``x`` refuses an existing file and a dangling link alike.
    with target.open("xb") as handle:
        handle.write(content)


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
    if bundle.manifest.files and not bundle.files:
        raise ValueError("this bundle names kit files but carries none of their bytes")
    for kit_file in bundle.files:
        _write_new_file(root, kit_file.path, kit_file.content)
    _require_document_bodies(bundle)
    for body in bundle.document_bodies:
        _write_new_file(
            root, f"{KIT_DOCUMENT_BODY_DIRECTORY}/{body.digest.partition(':')[2]}", body.content
        )


def _require_document_bodies(bundle: KitBundle) -> None:
    if bundle.document_body_digests() and not bundle.document_bodies:
        raise ValueError("this bundle names governed Documents but carries none of their bodies")


def write_kit_docs(bundle: KitBundle, out: Path, *, skills: bool = True) -> tuple[Path, ...]:
    """Write the kit's inert files into a new directory ``out``, at their own paths.

    ``skills=False`` leaves the agent skills out. Returns the files written.
    """

    if bundle.manifest.files and not bundle.files:
        raise KitSourceError("the kit names inert files but carries none of their bytes")
    selected = [item for item in bundle.manifest.files if skills or item.role != "skill"]
    contents = bundle.file_contents()
    out.mkdir(parents=True, exist_ok=False)
    for item in selected:
        _write_new_file(out, item.path, contents[item.path])
    return tuple(out / item.path for item in selected)


def kit_skill_directory_name(kit_id: str, skill: str) -> str:
    """Where a kit's skill installs: ``<kit>-<skill>``, unless the skill already
    carries the kit's name as its prefix."""

    return skill if skill.startswith(f"{kit_id}-") else f"{kit_id}-{skill}"


def install_kit_skills(bundle: KitBundle, skills_root: Path) -> tuple[Path, ...]:
    """Install each agent skill the kit carries as ``skills_root/<kit>-<skill>/``.

    A skill directory that already exists is refused before anything is written,
    so an install never mixes two releases' files. Returns the skill directories.
    """

    if bundle.manifest.files and not bundle.files:
        raise KitSourceError("the kit names inert files but carries none of their bytes")
    kit_id = bundle.manifest.kit_id
    by_skill: dict[str, list[tuple[str, bytes]]] = {}
    contents = bundle.file_contents()
    for item in bundle.manifest.files:
        if item.skill is None:
            continue
        inside = item.path.split("/", 2)[2]
        by_skill.setdefault(item.skill, []).append((inside, contents[item.path]))
    targets = {
        skill: skills_root / kit_skill_directory_name(kit_id, skill) for skill in sorted(by_skill)
    }
    taken = sorted(str(path) for path in targets.values() if path.exists() or path.is_symlink())
    if taken:
        raise KitSourceError(f"skill directories already exist: {', '.join(taken)}")
    skills_root.mkdir(parents=True, exist_ok=True)
    for skill, target in targets.items():
        target.mkdir()
        for inside, content in by_skill[skill]:
            _write_new_file(target, inside, content)
    return tuple(targets.values())


_PROVIDER_PREFIX = KIT_PROVIDER_DIRECTORY + "/"
_FILE_PREFIX = KIT_FILE_DIRECTORY + "/"
_BODY_PREFIX = KIT_DOCUMENT_BODY_DIRECTORY + "/"


def _pack_kit(bundle: KitBundle) -> tuple[bytes, tuple[bytes, ...]]:
    if bundle.manifest.provider_files() and not bundle.provider_files:
        raise ValueError("this bundle names provider packages but carries none of their bytes")
    config = pretty_canonical_bytes(bundle.manifest.model_dump(mode="json"))
    if bundle.manifest.files and not bundle.files:
        raise ValueError("this bundle names kit files but carries none of their bytes")
    files = {
        **bundle.contents(),
        **{
            _PROVIDER_PREFIX + name: content for name, content in bundle.provider_contents().items()
        },
        **{_FILE_PREFIX + path: content for path, content in bundle.file_contents().items()},
        **{
            _BODY_PREFIX + body.digest.partition(":")[2]: body.content
            for body in bundle.document_bodies
        },
    }
    _require_document_bodies(bundle)
    return config, (pack_files(files),)


def _unpack_kit(config: bytes, layers: tuple[bytes, ...]) -> KitBundle:
    if len(layers) != 1:
        raise ValueError("a kit artifact has exactly one layer")
    manifest = KitManifest.model_validate_json(config)
    files = unpack_files(layers[0])
    carried = sorted(
        path.removeprefix(_FILE_PREFIX) for path in files if path.startswith(_FILE_PREFIX)
    )
    if carried != sorted(manifest.kit_files()):
        raise ValueError("a kit artifact's layer must hold exactly its manifest's kit files")
    return KitBundle(
        manifest=manifest,
        artifacts=tuple(
            KitArtifactBytes.of(path, files[path])
            for path in sorted(files)
            if not path.startswith((_PROVIDER_PREFIX, _FILE_PREFIX, _BODY_PREFIX))
        ),
        document_bodies=tuple(
            sorted(
                (
                    KitDocumentBody.of(files[path])
                    for path in files
                    if path.startswith(_BODY_PREFIX)
                ),
                key=lambda body: body.digest,
            )
        ),
        provider_files=tuple(
            KitProviderFileBytes.of(path.removeprefix(_PROVIDER_PREFIX), files[path])
            for path in sorted(files)
            if path.startswith(_PROVIDER_PREFIX)
        ),
        files=tuple(
            KitFileBytes.of(path.removeprefix(_FILE_PREFIX), files[path])
            for path in sorted(files)
            if path.startswith(_FILE_PREFIX)
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
    if bundle.document_body_digests() and not bundle.document_bodies:
        raise KitSourceError("the kit names governed Documents but carries none of their bodies")
    for item in bundle.provider_files:
        stored = client.store_body(instance_id, item.content)
        if stored.digest != bundle.manifest.provider_files()[item.filename]:
            raise ConfigError(f"the daemon stored {item.filename} under another digest")
    # Each governed Document's body rides the same staging (kit add reads it there).
    for body in bundle.document_bodies:
        if client.store_body(instance_id, body.content).digest != body.digest:
            raise ConfigError(f"the daemon stored Document body {body.digest} under another digest")
    return bundle.model_copy(update={"provider_files": (), "document_bodies": ()})


def kit_automation_template(bundle: KitBundle) -> KitAutomationTemplate | None:
    """The kit's automation template with its text, checked against the manifest.

    ``None`` when the kit names no template. Refuses
    ``cruxible.kit.source_invalid`` when a declared template's bytes are absent
    from the bundle (an inline bundle that dropped its files), differ from the
    manifest's sha256, exceed the template cap, or are not UTF-8. ``kit add``
    adapters call it on the incoming bundle before the daemon, so a bad or
    missing template refuses before anything is proposed.
    """

    path = bundle.manifest.automation_template
    if path is None:
        return None
    content = next((item.content for item in bundle.files if item.path == path), None)
    if content is None:
        raise KitSourceError(
            f"the kit declares the automation template {path}, but the bundle carries no "
            "bytes for it; send the bundle with its files, or add the kit by reference"
        )
    if len(content) > KIT_AUTOMATION_TEMPLATE_MAX_BYTES:
        raise KitSourceError(
            f"the automation template {path} is over {KIT_AUTOMATION_TEMPLATE_MAX_BYTES} bytes"
        )
    if Sha256Value(hashlib.sha256(content).hexdigest()).tagged != bundle.manifest.kit_files()[path]:
        raise KitSourceError(f"the automation template {path} differs from its manifest sha256")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise KitSourceError(f"the automation template {path} is not UTF-8 text") from exc
    return KitAutomationTemplate(path=path, content=text)


def with_automation_template(
    result: KitChangeResult, template: KitAutomationTemplate | None
) -> KitChangeResult:
    """``result`` carrying the template's path and text, when the adapter read one."""

    if template is None:
        return result
    return result.model_copy(
        update={
            "automation_template": template.path,
            "automation_template_content": template.content,
        }
    )


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


def _local_kit(path: Path) -> tuple[KitBundle, ArtifactImage | None, str] | None:
    """A kit read from a local directory or layout (with the layout's image), or
    ``None`` when ``path`` names nothing on disk."""

    if path.is_dir():
        if is_layout(path):
            try:
                image = read_layout(path)
            except ContainmentError as exc:
                raise KitSourceError(f"OCI layout {path}: {exc}") from exc
            return unpack_artifact(KIT_ARTIFACT, image), image, f"{path.name}@{image.digest}"
        if not (path / KIT_MANIFEST_FILE).is_file():
            raise KitSourceError(f"directory {path} holds no {KIT_MANIFEST_FILE}")
        try:
            return read_kit_directory(path), None, path.name
        except ValidationError as exc:
            raise KitSourceError(
                f"{path / KIT_MANIFEST_FILE} is not a kit manifest: {validation_summary(exc)}"
            ) from exc
        except ValueError as exc:
            raise KitSourceError(str(exc)) from exc
    if path.exists():
        raise KitSourceError(f"{path} is a file, not a kit directory or layout")
    return None


def resolve_kit(source: str, *, registry: RegistryClient | None = None) -> tuple[KitBundle, str]:
    """A bundle from a kit directory, an OCI layout, or a registry reference.

    Returns the bundle and where it came from; a registry source is reported
    pinned to the manifest digest actually pulled.
    """

    local = _local_kit(Path(source).expanduser())
    if local is not None:
        bundle, _, origin = local
        return bundle, origin
    image, origin = _fetch_after_local_miss(source, registry=registry)
    return unpack_artifact(KIT_ARTIFACT, image), origin


def _fetch_after_local_miss(
    source: str, *, registry: RegistryClient | None
) -> tuple[ArtifactImage, str]:
    """Fetch ``source`` from its registry once nothing on disk answered it.

    A registry miss on a source that also reads as a relative path (``kits/foo``)
    says both places were tried, so a mistyped directory is not reported as an
    unpublished release alone.
    """

    try:
        return fetch_kit_image(source, registry=registry)
    except RegistryRefusal as exc:
        if exc.reason != "not_found" or "/" not in source:
            raise
        raise RegistryRefusal(
            "not_found",
            f"{str(exc).removeprefix(exc.error_code + ': ')}; no local kit directory "
            f"or layout exists at {source} either",
            status=exc.status,
        ) from exc


def resolve_kit_image(
    source: str, *, registry: RegistryClient | None = None
) -> tuple[ArtifactImage, str]:
    """The kit artifact a source names, read the way :func:`resolve_kit` reads it.

    A layout keeps its exact bytes; a kit directory is packed deterministically
    (the same digest its release has); a registry reference is pulled and verified.
    """

    local = _local_kit(Path(source).expanduser())
    if local is None:
        return _fetch_after_local_miss(source, registry=registry)
    bundle, image, origin = local
    return (image if image is not None else pack_artifact(KIT_ARTIFACT, bundle)), origin


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
    directory or layout have nothing to check. A kit the daemon already compared
    (``checked``: the base kit against the one core bundles) is kept as it is.
    """

    client = registry
    checked = []
    try:
        for kit in status.kits:
            if kit.update_check == "checked":
                # The daemon compared it locally (the base kit against the one
                # core bundles); no registry is asked.
                checked.append(kit)
                continue
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
