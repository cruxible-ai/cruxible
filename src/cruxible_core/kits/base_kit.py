"""The base kit: the cross-kit capabilities every instance holds from creation (note §7.4).

Its directory (``base/``) is rendered by ``scripts/build_base_kit.py``, which
also writes :data:`~cruxible_core.kits.base_kit_pin.BASE_KIT_PIN`, the bundled
version and content digest. :func:`bundled_base_kit` reads the directory once
per process and refuses one whose manifest or bytes differ from that pin.

Creating an instance installs it through the ordinary ``kit add`` path with
source ``bundled``. Creation first records the step durably in the managed
root (:data:`BASE_KIT_PENDING_FILE`), before the descriptor makes the root
openable; every open that finds that record runs the add (idempotent: an
existing proposal or receipt is reused) and then removes it, so a process that
dies between genesis and the add leaves an instance the next open finishes.
An instance without the record is never given the kit: a base kit removed on
purpose, or a proposal pending or rejected, keeps its meaning.
``kit add base`` installs or upgrades from this copy, and
``kit status`` compares the installed ``base`` with it. Nothing here contacts a
registry or the network.
"""

from __future__ import annotations

import functools
import os
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import BootstrapError
from cruxible_client.contracts.kits import (
    KIT_ARTIFACT_DIRECTORY,
    KIT_MANIFEST_FILE,
    KitArtifactBytes,
    KitBundle,
    KitManifest,
)
from cruxible_core.kits import BundledKitPin
from cruxible_core.kits.base_kit_pin import BASE_KIT_PIN

if TYPE_CHECKING:
    from cruxible_client.contracts.kits import KitChangeResult
    from cruxible_core.runtime.instance import PlaybillInstance

#: The reference ``kit add`` reads as the bundled base kit, before any registry.
BASE_KIT_REFERENCE = "base"
#: The source a bundled install records in its kit receipt.
BUNDLED_KIT_SOURCE = "bundled"
#: In the managed root while creation's base kit add has not produced its
#: proposal or receipt; the next open resumes the add and removes it.
BASE_KIT_PENDING_FILE = "base-kit-pending.json"


class PendingBaseKit(BaseModel):
    """Creation's unfinished base kit step: who adds it, and at what time."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["cruxible-base-kit-pending-v1"] = "cruxible-base-kit-pending-v1"
    actor_id: str
    #: Creation's genesis time, the add's proposal time (canonical candidate form).
    at: str


def base_kit_directory() -> Path:
    """Where the bundled base kit directory is in this installation."""

    return Path(str(files("cruxible_core.kits").joinpath("base")))


def load_bundled_kit(directory: Path, pin: BundledKitPin) -> KitBundle:
    """Read one bundled kit directory, refusing any that differs from ``pin``.

    The manifest must name ``pin``'s kit id, version and content digest, and
    every artifact's bytes must reproduce the digest the manifest names for it.
    """

    from pydantic import ValidationError

    from cruxible_core.claims.closure import parse_dependency_artifact

    try:
        bundle = _read_directory(directory)
    except (OSError, ValueError, ValidationError) as exc:
        raise BootstrapError(f"the bundled kit at {directory} cannot be read: {exc}") from exc
    manifest = bundle.manifest
    found = (manifest.kit_id, manifest.version, manifest.content_digest)
    if found != (pin.kit_id, pin.version, pin.content_digest):
        raise BootstrapError(
            f"the bundled kit at {directory} is {found[0]} {found[1]} ({found[2]}), but this "
            f"core ships {pin.kit_id} {pin.version} ({pin.content_digest}); rebuild it with "
            "scripts/build_base_kit.py"
        )
    expected = manifest.digests()
    for path, content in bundle.contents().items():
        state = parse_dependency_artifact(path, content)
        if state is None or state.artifact_digest != expected[path]:
            raise BootstrapError(
                f"the bundled kit's {path} differs from the digest its manifest names; "
                "rebuild it with scripts/build_base_kit.py"
            )
    return bundle


def _read_directory(directory: Path) -> KitBundle:
    """A definitions-only kit directory: its manifest and exactly the artifacts it names."""

    manifest = KitManifest.model_validate_json((directory / KIT_MANIFEST_FILE).read_bytes())
    root = directory / KIT_ARTIFACT_DIRECTORY
    named = [item.path for item in manifest.artifacts]
    present = sorted(
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    )
    if present != sorted(named):
        raise ValueError(
            f"its artifacts do not match its manifest (unlisted: "
            f"{sorted(set(present) - set(named)) or 'none'}; missing: "
            f"{sorted(set(named) - set(present)) or 'none'})"
        )
    return KitBundle(
        manifest=manifest,
        artifacts=tuple(KitArtifactBytes.of(path, (root / path).read_bytes()) for path in named),
    )


@functools.cache
def bundled_base_kit() -> KitBundle:
    """The base kit this core ships, read and checked once per process."""

    return load_bundled_kit(base_kit_directory(), BASE_KIT_PIN)


def bundled_base_artifact_digest(path: str) -> str | None:
    """The digest the bundled base kit holds at ``path``, or ``None``."""

    return bundled_base_kit().manifest.digests().get(path)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def record_pending_base_kit(root: Path, *, actor_id: str, at: datetime) -> None:
    """Durably record, in a new managed root, that its base kit add has not run yet."""

    from cruxible_client.contracts.candidates import canonical_candidate_timestamp

    pending = PendingBaseKit(actor_id=actor_id, at=canonical_candidate_timestamp(at))
    path = root / BASE_KIT_PENDING_FILE
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, canonical_bytes(pending.model_dump(mode="json")) + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(root)


def resume_pending_base_kit(instance: PlaybillInstance) -> KitChangeResult | None:
    """Finish creation's base kit add when its record is present; else do nothing.

    Costs one failed open of the record when there is none. With the record it
    runs the ordinary add, which reuses a proposal already open at its target or
    answers ``unchanged`` over an accepted receipt, then removes the record.
    """

    from cruxible_client.contracts.temporal import parse_datetime

    path = instance.root / BASE_KIT_PENDING_FILE
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        pending = PendingBaseKit.model_validate_json(raw)
    except ValueError as exc:
        raise BootstrapError(f"{path} does not record a pending base kit add") from exc
    at = parse_datetime(pending.at)
    assert at is not None
    result = install_bundled_base_kit(instance, actor_id=pending.actor_id, at=at)
    clear_pending_base_kit(instance.root)
    return result


def clear_pending_base_kit(root: Path) -> None:
    """Remove the record once the add has produced its proposal or receipt."""

    (root / BASE_KIT_PENDING_FILE).unlink(missing_ok=True)
    _fsync_directory(root)


def install_bundled_base_kit(
    instance: PlaybillInstance, *, actor_id: str, at: datetime
) -> KitChangeResult:
    """Add the bundled base kit as ``actor_id``: creation's first change after genesis.

    The ordinary ``kit add`` commit, pinned at the instance's head, proposed at
    ``at`` (creation's genesis time). It lands at once under
    ``self_approval_allowed`` and stops at ``proposed`` under
    ``independent_approval_required``, like every kit add. Run again over
    its own proposal it returns that proposal; over its receipt, ``unchanged``.
    """

    from cruxible_client.contracts.candidates import canonical_candidate_timestamp
    from cruxible_client.contracts.kits import KitAddRequest
    from cruxible_core.service.kits import service_add_kit

    timestamp = canonical_candidate_timestamp(at)
    result = service_add_kit(
        instance,
        KitAddRequest(
            bundle=bundled_base_kit(),
            source=BUNDLED_KIT_SOURCE,
            dry_run=False,
            at=instance.accepted_coordinate().git_oid,
        ),
        actor_id=actor_id,
        timestamp=timestamp,
    )
    if result.status not in {"accepted", "proposed", "unchanged"}:
        raise BootstrapError(
            f"creation could not add the bundled base kit ({result.status}): "
            f"{result.detail or 'no detail'}"
        )
    return result


def bundled_kit_reference(reference: str) -> tuple[KitBundle, str] | None:
    """The bundle and recorded source ``reference`` names when it is the bundled base kit.

    ``base`` resolves here before any directory or registry; any other
    reference is the adapter's ordinary kit resolution.
    """

    if reference != BASE_KIT_REFERENCE:
        return None
    return bundled_base_kit(), BUNDLED_KIT_SOURCE


__all__ = [
    "BASE_KIT_PENDING_FILE",
    "BASE_KIT_PIN",
    "BASE_KIT_REFERENCE",
    "BUNDLED_KIT_SOURCE",
    "base_kit_directory",
    "PendingBaseKit",
    "bundled_base_artifact_digest",
    "bundled_base_kit",
    "bundled_kit_reference",
    "clear_pending_base_kit",
    "install_bundled_base_kit",
    "load_bundled_kit",
    "record_pending_base_kit",
    "resume_pending_base_kit",
]
