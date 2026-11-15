"""The append-only record of every base kit a published release shipped.

Instances keep the base kit artifacts they accepted, and replay re-checks those
acceptances under whatever core opens them. So once a release ships a base kit,
its artifacts are fixed: a later bundle may add artifacts and new versions, but
never change or drop a released one, and it may change content only under a new
kit version. :func:`release_problems` states those rules;
``scripts/build_base_kit.py --check`` and
``tests/test_guardrails/test_base_kit_releases.py`` apply them to the bundle.

Rules for the record:

- Entries are written by ``scripts/build_base_kit.py --release`` when a release
  is cut, and never edited or removed after.
- A released artifact at a versioned path (a Capability's or a Contract's
  ``.../v<N>.json``) keeps its digest in every later bundle. A CaptureKind's path
  carries no version: a later kit version may ship its successor there.
- A kit version that was released keeps its content digest.

Empty: nothing is released yet.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class ReleasedBaseKit:
    """One released base kit: its version, content digest and artifact digests."""

    version: str
    content_digest: str
    #: ``(artifact path, digest)``, sorted by path.
    artifacts: tuple[tuple[str, str], ...]

    def digests(self) -> dict[str, str]:
        return dict(self.artifacts)


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

    import json
    from pathlib import Path

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
    "ReleasedBaseKit",
    "release_problems",
    "released_artifact_digests",
    "render_releases_module",
]
