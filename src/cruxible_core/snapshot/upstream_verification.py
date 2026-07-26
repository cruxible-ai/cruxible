"""Digest verification for the materialized upstream a pullable overlay tracks.

An overlay's ``.cruxible/upstream/current/`` directory is pulled state: it is
supposed to stay byte-identical to what the publisher released. Nothing on the
local machine is authorized to edit it, so every read of it is a read of content
whose provenance is a digest, not a filesystem.

The daemon already refuses to start against an active config whose digest does
not match what was recorded. This module carries the same doctrine to the
materialized upstream: each member is verified immediately before it is
consumed -- by config reload, by ownership resolution, by pull preview, and by
pull apply -- so an out-of-band edit surfaces as a refusal naming the member,
both digests, and the recovery, instead of silently re-scoping composition,
ownership, or the pull delta.

Verification is per-member on purpose. Composition reads only ``config.yaml``;
hashing a large ``graph.json`` on every read surface would be a cost paid for
nothing. Callers name the members they are about to consume.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from cruxible_core.errors import ConfigError
from cruxible_core.snapshot.types import UpstreamMetadata

UpstreamMember = Literal["manifest.json", "graph.json", "config.yaml", "cruxible.lock.yaml"]

ALL_UPSTREAM_MEMBERS: tuple[UpstreamMember, ...] = (
    "manifest.json",
    "graph.json",
    "config.yaml",
    "cruxible.lock.yaml",
)

_MEMBER_FIELDS: dict[UpstreamMember, tuple[str, str]] = {
    "manifest.json": ("manifest_path", "manifest_digest"),
    "graph.json": ("graph_path", "graph_digest"),
    "config.yaml": ("upstream_config_path", "upstream_config_digest"),
    "cruxible.lock.yaml": ("lock_path", "upstream_lock_digest"),
}


def sha256_file(path: Path) -> str | None:
    """Return the ``sha256:``-prefixed digest of a file, or None if it is absent."""
    if not path.exists():
        return None
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def verify_tracked_upstream(
    root: Path,
    upstream: UpstreamMetadata,
    *,
    members: tuple[UpstreamMember, ...] = ALL_UPSTREAM_MEMBERS,
) -> None:
    """Verify materialized upstream members against the digests tracking recorded.

    A member whose digest was never recorded (overlays created before that
    member was pinned) is skipped rather than failed: there is nothing to
    compare it against, and refusing would break overlays that were never
    tampered with. A member that *was* pinned and no longer matches is refused.
    """
    for member in members:
        path_field, digest_field = _MEMBER_FIELDS[member]
        expected = getattr(upstream, digest_field)
        if expected is None:
            continue
        relative_path = getattr(upstream, path_field)
        path = root / relative_path
        if not path.exists():
            raise ConfigError(
                f"Tracked upstream release {upstream.state_id}:{upstream.release_id} is "
                f"missing its materialized '{member}' at {relative_path}, which upstream "
                f"tracking pins at {expected}. Re-pull the release in REPAIR mode "
                "(`cruxible state pull-preview --repair` then "
                "`cruxible state pull-apply --repair --apply-digest ...`) or re-create the "
                "overlay from the published release; nothing may be read from a missing "
                "upstream. Repair preserves claim ids -- a plain re-pull of the release "
                "already tracked is refused as a no-op."
            )
        actual = sha256_file(path)
        if actual != expected:
            raise ConfigError(
                f"Tracked upstream release {upstream.state_id}:{upstream.release_id} no "
                f"longer matches its recorded '{member}' digest: expected {expected}, "
                f"found {actual} at {relative_path}. The materialized upstream was edited "
                "locally, and pulled state must stay byte-identical to what was published. "
                "Restore the file from the published release -- re-pull it in REPAIR mode "
                "(`cruxible state pull-preview --repair` then "
                "`cruxible state pull-apply --repair --apply-digest ...`) or re-create the "
                "overlay -- then retry. Repair preserves claim ids; a plain re-pull of the "
                "release already tracked is refused as a no-op."
            )
