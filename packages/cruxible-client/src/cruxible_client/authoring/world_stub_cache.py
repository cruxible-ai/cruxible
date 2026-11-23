"""One cached world stub per instance, in the per-user Cruxible directory.

`cruxible python --check` type-checks a script against the instance's world
stub from any repository or worktree, so the stub lives beside the CLI context
file (``CRUXIBLE_CLI_CONTEXT_PATH``, else ``~/.cruxible/client-context.json``),
at ``stubs/<instance>/world.pyi``, never in the caller's checkout. Its module
name is ``world``, as `cruxible stub` documents importing it::

    if TYPE_CHECKING:
        from world import World

The cache moves only when the vocabulary does. Each use reads the accepted head
(the cheapest read there is); an unchanged head reuses the stub with no other
read. A moved head re-reads the vocabulary listing alone (the ``claim_types``
and ``subject_kinds`` orient sections, O(vocabulary), without the per-Claim
counters) and re-renders only when that listing changed. A stub recorded for
another daemon endpoint, or one whose file no longer carries the recorded
digest, renders again.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, Any, Literal

from cruxible_client.authoring.world_stub import render_world_stub_for, stub_vocabulary_digest

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cruxible_client.contracts import OrientSection
    from cruxible_client.transport.http import CruxibleClient

#: The module name the stub is imported as (``from world import World``).
WORLD_STUB_MODULE = "world"
STUB_CACHE_FORMAT = "cruxible-world-stub-cache-v1"

_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
#: Per-Claim counters the claim_types section carries; they move with every
#: Claim write and say nothing about the vocabulary.
_COUNTER_FIELDS = frozenset({"live_claims", "subjects_with_value", "evidence_counts"})

StubCacheStatus = Literal["reused", "revalidated", "rendered"]


@dataclass(frozen=True)
class CachedWorldStub:
    """Where an instance's cached stub is, and what this use did to keep it current."""

    instance_id: str
    path: Path
    status: StubCacheStatus
    vocabulary_digest: str | None

    @property
    def directory(self) -> Path:
        """The directory to put on a type checker's path for ``from world import World``."""

        return self.path.parent


def user_cruxible_directory(environ: Mapping[str, str] | None = None) -> Path:
    """The per-user Cruxible directory: the CLI context file's own directory."""

    env = os.environ if environ is None else environ
    raw = env.get("CRUXIBLE_CLI_CONTEXT_PATH")
    context = Path(raw).expanduser() if raw else Path.home() / ".cruxible" / "client-context.json"
    return context.resolve().parent


def world_stub_cache_directory(instance_id: str, environ: Mapping[str, str] | None = None) -> Path:
    """``<user dir>/stubs/<instance>``; an id unsafe as a path component is hashed."""

    component = (
        instance_id
        if _SAFE_COMPONENT.fullmatch(instance_id) and instance_id not in {".", ".."}
        else "sha256-" + hashlib.sha256(instance_id.encode("utf-8")).hexdigest()[:32]
    )
    return user_cruxible_directory(environ) / "stubs" / component


def vocabulary_fingerprint(client: CruxibleClient, instance_id: str, *, at: Any) -> str:
    """sha256 over the live ClaimType and SubjectKind listing at ``at``, counters left out."""

    from cruxible_client.contracts import ORIENT_MAX_LIMIT

    listing: dict[str, list[Any]] = {"claim_types": [], "subject_kinds": []}
    sections: tuple[OrientSection, ...] = ("claim_types", "subject_kinds")
    for section in sections:
        cursor: str | None = None
        while True:
            page = client.orient(
                instance_id,
                section=section,
                limit=ORIENT_MAX_LIMIT,
                cursor=cursor,
                at=at,
                surface="sdk",
            )
            for row in getattr(page, section) or ():
                listing[section].append(row.model_dump(mode="json", exclude=_COUNTER_FIELDS))
            if not page.truncated or page.next_cursor is None:
                break
            cursor = page.next_cursor
    encoded = json.dumps(listing, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _read_state(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(loaded, dict) or loaded.get("format") != STUB_CACHE_FORMAT:
        return None
    return loaded


def _stub_digest(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8") as handle:
            head = "".join(handle.readline() for _ in range(4))
    except OSError:
        return None
    return stub_vocabulary_digest(head)


def _write_atomic(path: Path, text: str) -> None:
    with NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), delete=False, prefix=f".{path.name}.tmp."
    ) as handle:
        handle.write(text)
        staged = Path(handle.name)
    staged.replace(path)


def cached_world_stub(
    client: CruxibleClient,
    instance_id: str,
    *,
    transport: str | None,
    environ: Mapping[str, str] | None = None,
    render: Callable[[CruxibleClient, str], str] | None = None,
) -> CachedWorldStub:
    """The instance's cached stub, re-rendered only when its vocabulary moved.

    ``transport`` is the daemon endpoint as the CLI context compares it (a URL
    or ``unix://<realpath>``): instance IDs are not unique across daemons, so a
    stub recorded for another endpoint is rendered again. ``render`` defaults to
    `render_world_stub_for`.
    """

    directory = world_stub_cache_directory(instance_id, environ)
    stub_path = directory / f"{WORLD_STUB_MODULE}.pyi"
    state_path = directory / "state.json"
    head = client.head(instance_id)
    git_oid = head.coordinate.git_oid
    state = _read_state(state_path)
    digest = _stub_digest(stub_path) if state is not None else None
    usable = (
        state is not None
        and state.get("instance") == instance_id
        and state.get("transport") == transport
        and digest is not None
        and state.get("vocabulary_digest") == digest
    )
    if usable and state is not None and state.get("git_oid") == git_oid:
        return CachedWorldStub(instance_id, stub_path, "reused", digest)
    fingerprint = vocabulary_fingerprint(client, instance_id, at=head.coordinate)
    status: StubCacheStatus
    if usable and state is not None and state.get("fingerprint") == fingerprint:
        status = "revalidated"
    else:
        rendered = (
            render(client, instance_id)
            if render is not None
            else render_world_stub_for(client, instance_id, workspace=Path.cwd())
        )
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        _write_atomic(stub_path, rendered)
        digest = stub_vocabulary_digest(rendered)
        status = "rendered"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_atomic(
        state_path,
        json.dumps(
            {
                "format": STUB_CACHE_FORMAT,
                "instance": instance_id,
                "transport": transport,
                "git_oid": git_oid,
                "fingerprint": fingerprint,
                "vocabulary_digest": digest,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    return CachedWorldStub(instance_id, stub_path, status, digest)


__all__ = [
    "STUB_CACHE_FORMAT",
    "WORLD_STUB_MODULE",
    "CachedWorldStub",
    "StubCacheStatus",
    "cached_world_stub",
    "user_cruxible_directory",
    "vocabulary_fingerprint",
    "world_stub_cache_directory",
]
