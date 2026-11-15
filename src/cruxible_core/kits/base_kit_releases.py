"""The append-only record of every base kit a published release shipped.

Instances keep the base kit artifacts they accepted, and replay re-checks those
acceptances under whatever core opens them. So once a release ships a base kit,
its artifacts are fixed: a later bundle may add artifacts and new versions, but
never change or drop a released one, and it may change content only under a new
kit version. :func:`release_problems` states those rules;
``scripts/build_base_kit.py --check`` and
``tests/test_guardrails/test_base_kit_releases.py`` apply them to the bundle.

The record is itself anchored, the way the genesis seed history is: the pins
file ``tests/test_guardrails/base_kit_release_pins.json`` holds each entry's
digest and ``frozen_through``, the highest ordinal a published release shipped.
:func:`record_problems` refuses a frozen entry that was edited, reordered or
removed, and an entry without its pin, before any bundle is compared, so an
edited or truncated record cannot re-admit a rewritten released artifact.

Rules for the record:

- Entries are written by ``scripts/build_base_kit.py --release`` when a release
  is cut, each with its pin, and never edited or removed after. The cut then
  raises ``frozen_through`` in the pins file to the new ordinal.
- A released artifact at a versioned path (a Capability's or a Contract's
  ``.../v<N>.json``) keeps its digest in every later bundle. A CaptureKind's path
  carries no version: a later kit version may ship its successor there.
- A kit version that was released keeps its content digest.

Empty: nothing is released yet.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from cruxible_client.contracts.canonical import Sha256Value, typed_digest


@dataclass(frozen=True)
class ReleasedBaseKit:
    """One released base kit: its version, content digest and artifact digests."""

    version: str
    content_digest: str
    #: ``(artifact path, digest)``, sorted by path.
    artifacts: tuple[tuple[str, str], ...]

    def digests(self) -> dict[str, str]:
        return dict(self.artifacts)

    def entry_digest(self) -> str:
        """The digest that names this entry (pinned in the pins file)."""

        return typed_digest(
            Sha256Value,
            "cruxible-base-kit-release-v1",
            {
                "version": self.version,
                "content_digest": self.content_digest,
                "artifacts": [{"path": path, "digest": digest} for path, digest in self.artifacts],
            },
        ).tagged


class ReleasePin(BaseModel):
    """One record entry's pin: its ordinal (1-based position), version and digest."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ordinal: int = Field(ge=1)
    version: str
    entry_digest: str


class ReleasePins(BaseModel):
    """The pins file: every entry's pin and the highest ordinal a release shipped."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    frozen_through: int = Field(default=0, ge=0)
    entries: tuple[ReleasePin, ...] = ()


#: The pins file, beside the guardrail that pins the record by hand.
RELEASE_PINS_RELATIVE = Path("tests/test_guardrails/base_kit_release_pins.json")


def load_release_pins(path: Path) -> ReleasePins:
    return ReleasePins.model_validate_json(path.read_bytes())


def render_release_pins(pins: ReleasePins) -> str:
    return json.dumps(pins.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def pin_of(ordinal: int, release: ReleasedBaseKit) -> ReleasePin:
    return ReleasePin(ordinal=ordinal, version=release.version, entry_digest=release.entry_digest())


#: Every base kit a published release shipped, oldest first. Append only; see above.
BASE_KIT_RELEASES: tuple[ReleasedBaseKit, ...] = ()


def _versioned(path: str) -> bool:
    """Whether the path names one version (a Capability's or a Contract's)."""

    return path.startswith(("capabilities/", "contracts/"))


def released_artifact_digests(
    path: str, releases: tuple[ReleasedBaseKit, ...] | None = None
) -> frozenset[str]:
    """Every digest a released base kit held at ``path``."""

    held = BASE_KIT_RELEASES if releases is None else releases
    return frozenset(
        digest for release in held for item, digest in release.artifacts if item == path
    )


def record_problems(
    pins: ReleasePins, releases: tuple[ReleasedBaseKit, ...] | None = None
) -> list[str]:
    """How the record breaks its pins: checked before any bundle is compared.

    A frozen entry (ordinal at or below ``frozen_through``) is present at its
    ordinal, unedited; every pinned entry is present; every entry has its pin; an
    unfrozen entry (not shipped yet) may be replaced together with its pin. Each
    problem names the entry and the repair.
    """

    held = BASE_KIT_RELEASES if releases is None else releases
    problems: list[str] = []
    ordinals = [pin.ordinal for pin in pins.entries]
    if ordinals != list(range(1, len(ordinals) + 1)):
        problems.append(
            f"{RELEASE_PINS_RELATIVE.name} pins ordinals {ordinals}: pins run 1..N, once each"
        )
    if pins.frozen_through > len(pins.entries):
        problems.append(
            f"{RELEASE_PINS_RELATIVE.name} freezes through entry {pins.frozen_through}, but pins "
            f"only {len(pins.entries)}: restore the missing pins"
        )
    pinned = {pin.ordinal: pin for pin in pins.entries}
    for ordinal, pin in sorted(pinned.items()):
        label = f"entry {ordinal} (base {pin.version})"
        if ordinal > len(held):
            problems.append(
                f"Released {label} is missing from BASE_KIT_RELEASES: entries are never "
                "removed; restore it"
            )
            continue
        release = held[ordinal - 1]
        if (release.version, release.entry_digest()) == (pin.version, pin.entry_digest):
            continue
        if ordinal <= pins.frozen_through:
            problems.append(
                f"Released {label} was edited, reordered or removed: BASE_KIT_RELEASES holds "
                f"base {release.version} ({release.entry_digest()}) there, pinned "
                f"{pin.entry_digest}. Restore it; a published release's entry never changes"
            )
        else:
            problems.append(
                f"{label} changed and is not frozen yet (frozen_through = "
                f"{pins.frozen_through}): set its pin to "
                f"{pin_of(ordinal, release).model_dump_json()}"
            )
    for ordinal, release in enumerate(held, start=1):
        if ordinal not in pinned:
            problems.append(
                f"entry {ordinal} (base {release.version}) has no pin: add "
                f"{pin_of(ordinal, release).model_dump_json()} to {RELEASE_PINS_RELATIVE.name}"
            )
    return problems


def release_problems(
    version: str,
    content_digest: str,
    artifacts: Mapping[str, str],
    releases: tuple[ReleasedBaseKit, ...] | None = None,
) -> list[str]:
    """How a bundle (``version``, ``content_digest``, path -> digest) breaks the record.

    Each problem names the artifact or version and the repair.
    """

    held = BASE_KIT_RELEASES if releases is None else releases
    problems: list[str] = []
    released_versions = {release.version: release.content_digest for release in held}
    if version in released_versions and released_versions[version] != content_digest:
        problems.append(
            f"base kit {version} was released with content {released_versions[version]}, and "
            f"the bundle now holds {content_digest}: bump the kit version in "
            "scripts/base_kit/kit.json"
        )
    reported: set[str] = set()
    for release in held:
        for path, digest in release.artifacts:
            if path in reported:
                continue
            if path not in artifacts:
                reported.add(path)
                problems.append(
                    f"{path} was released in base kit {release.version} and is missing from the "
                    "bundle: a released artifact is never dropped; restore it"
                )
            elif artifacts[path] != digest and (_versioned(path) or version in released_versions):
                reported.add(path)
                problems.append(
                    f"{path} was released in base kit {release.version} as {digest} and the "
                    f"bundle holds {artifacts[path]}: released versions never change; restore it "
                    "and add the change as a new version"
                )
    return problems


def render_releases_module(releases: tuple[ReleasedBaseKit, ...]) -> str:
    """This module's text with ``releases`` as its record (``build_base_kit.py --release``)."""

    text = Path(__file__).read_text(encoding="utf-8")
    head, marker, rest = text.partition("BASE_KIT_RELEASES: tuple[ReleasedBaseKit, ...] = ")
    _record, _sep, tail = rest.partition("\n\n\n")
    if not marker or not tail:
        raise ValueError("base_kit_releases.py lost its BASE_KIT_RELEASES assignment")
    if not releases:
        body = "()"
    else:
        lines = ["("]
        for release in releases:
            lines.append("    ReleasedBaseKit(")
            lines.append(f"        version={json.dumps(release.version)},")
            lines.append(f"        content_digest={json.dumps(release.content_digest)},")
            lines.append("        artifacts=(")
            for path, digest in release.artifacts:
                lines.append("            (")
                lines.append(f"                {json.dumps(path)},")
                lines.append(f"                {json.dumps(digest)},")
                lines.append("            ),")
            lines.append("        ),")
            lines.append("    ),")
        lines.append(")")
        body = "\n".join(lines)
    return f"{head}{marker}{body}\n\n\n{tail}"


__all__ = [
    "BASE_KIT_RELEASES",
    "RELEASE_PINS_RELATIVE",
    "ReleasePin",
    "ReleasePins",
    "ReleasedBaseKit",
    "load_release_pins",
    "pin_of",
    "record_problems",
    "release_problems",
    "render_release_pins",
    "released_artifact_digests",
    "render_releases_module",
]
