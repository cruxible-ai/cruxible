"""The registration fold over declared projection blocks.

A block is registered by `block repin`, which declares it in daemon protocol
state (exhaust), not in a second governed truth plane. ``next``, the detach
refusal and claim lowering all read this one fold so they cannot disagree about
which blocks an instance registers. It lives under ``cruxible_core/authoring``
rather than the service layer because lowering -- which may not import a service
module -- has to ask it which sources carry projection blocks before it admits a
citation into one.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.runtime.instance import PlaybillInstance

_SAFE_SEGMENT = re.compile(r"[a-z][a-z0-9_.-]{0,127}")


@dataclass(frozen=True)
class DeclaredBlockRegistration:
    """One projection block an agent declared with `block repin`."""

    source_id: str
    block_id: str
    declared_generation: int
    declared_coordinate: AcceptedCoordinate
    declared_by: str
    declared_at: str
    stamp_digest: str


@dataclass(frozen=True)
class ProjectionBlockRegistration:
    """One registered block.

    The identity is the fold's own -- the pair the page names, a source and a
    block -- and not a string prefix: a block an agent declares chooses its own
    id, so the only honest answer to "is this marker sanctioned?" is the one the
    instance keeps.
    """

    source_id: str
    block_id: str
    declaration: DeclaredBlockRegistration

    @property
    def identity(self) -> tuple[str, str]:
        return (self.source_id, self.block_id)


PROJECTION_BLOCK_DECLARATION_DIRECTORY = "projection-blocks"
_DECLARATION_TOMBSTONE_SUFFIX = ".released"


def _declaration_root(instance: PlaybillInstance) -> Path:
    return (
        instance.root / instance.descriptor.storage.exhaust / PROJECTION_BLOCK_DECLARATION_DIRECTORY
    )


def _declaration_path(root: Path, source_id: str, block_id: str) -> Path:
    # Both ids are constrained to `[a-z][a-z0-9_.-]*` by the marker grammar, so
    # neither can be `.`, `..`, absolute, or carry a separator. The check is
    # still made here rather than assumed: this function names a file.
    if not _SAFE_SEGMENT.fullmatch(source_id) or not _SAFE_SEGMENT.fullmatch(block_id):
        raise FormatError(
            "cruxible.block.declaration_identity_invalid: a block declaration is addressed "
            "by a source id and a block id in the marker grammar's own alphabet"
        )
    return root / source_id / f"{block_id}.json"


def projection_block_declarations(
    instance: PlaybillInstance,
) -> tuple[DeclaredBlockRegistration, ...] | None:
    """Read every declared block, or ``None`` when the store cannot be read."""

    root = _declaration_root(instance)
    if not root.is_dir():
        return ()
    declarations: list[DeclaredBlockRegistration] = []
    try:
        for directory in sorted(root.iterdir(), key=lambda item: item.name):
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
                payload = json.loads(path.read_text(encoding="utf-8"))
                declarations.append(
                    DeclaredBlockRegistration(
                        source_id=str(payload["source_id"]),
                        block_id=str(payload["block_id"]),
                        declared_generation=int(payload["declared_generation"]),
                        declared_coordinate=AcceptedCoordinate.model_validate(
                            payload["declared_coordinate"]
                        ),
                        declared_by=str(payload["declared_by"]),
                        declared_at=str(payload["declared_at"]),
                        stamp_digest=str(payload["stamp_digest"]),
                    )
                )
    except (OSError, KeyError, TypeError, ValueError) as exc:
        del exc
        return None
    return tuple(
        sorted(
            declarations,
            key=lambda item: (item.source_id.encode("utf-8"), item.block_id.encode("ascii")),
        )
    )


def write_projection_block_declaration(
    instance: PlaybillInstance,
    *,
    source_id: str,
    block_id: str,
    declared_generation: int,
    declared_coordinate: AcceptedCoordinate,
    declared_by: str,
    declared_at: str,
    stamp_digest: str,
) -> DeclaredBlockRegistration:
    """Record one declared block, replacing any earlier declaration of the same pair."""

    root = _declaration_root(instance)
    path = _declaration_path(root, source_id, block_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = DeclaredBlockRegistration(
        source_id=source_id,
        block_id=block_id,
        declared_generation=declared_generation,
        declared_coordinate=declared_coordinate,
        declared_by=declared_by,
        declared_at=declared_at,
        stamp_digest=stamp_digest,
    )
    payload = {
        "tag": "playbill-projection-block-declaration-v1",
        "source_id": record.source_id,
        "block_id": record.block_id,
        "declared_generation": record.declared_generation,
        "declared_coordinate": record.declared_coordinate.model_dump(mode="json"),
        "declared_by": record.declared_by,
        "declared_at": record.declared_at,
        "stamp_digest": record.stamp_digest,
    }
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_bytes(canonical_bytes(payload))
    os.replace(temporary, path)
    # Declaring the pair again is the block coming back, so the tombstone that
    # said it had gone must not outlive it.
    path.with_name(f"{path.name}{_DECLARATION_TOMBSTONE_SUFFIX}").unlink(missing_ok=True)
    return record


def release_projection_block_declaration(
    instance: PlaybillInstance,
    *,
    source_id: str,
    block_id: str,
) -> bool:
    """Release one declared block; ``False`` when the instance never held it.

    The record is renamed rather than deleted. Releasing a registration is
    idempotent by contract, and a call that simply forgot could not tell a
    second release from a block it had never registered at all -- it would
    refuse the second one by naming a publication that never existed. The
    tombstone is not read by the fold (it does not end in ``.json``); it exists
    so the answer to "was this ever registered here?" survives the release.
    """

    path = _declaration_path(_declaration_root(instance), source_id, block_id)
    try:
        path.replace(path.with_name(f"{path.name}{_DECLARATION_TOMBSTONE_SUFFIX}"))
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise FormatError(
            "cruxible.block.declaration_unreleasable: the block declaration store cannot "
            f"be written: {exc}"
        ) from exc
    return True


def released_projection_block_declaration(
    instance: PlaybillInstance,
    *,
    source_id: str,
    block_id: str,
) -> bool:
    """Whether this instance once registered a block it has since released."""

    path = _declaration_path(_declaration_root(instance), source_id, block_id)
    return path.with_name(f"{path.name}{_DECLARATION_TOMBSTONE_SUFFIX}").is_file()


def registered_projection_blocks(
    instance: PlaybillInstance,
) -> dict[tuple[str, str], ProjectionBlockRegistration] | None:
    """Every block this instance registers.

    ``None`` when the declaration store cannot be read: an unreadable registry is
    not an empty one, and every consumer of this fold refuses rather than
    concluding a marker is unsanctioned because its record could not be opened.
    """

    declarations = projection_block_declarations(instance)
    if declarations is None:
        return None
    return {
        (declaration.source_id, declaration.block_id): ProjectionBlockRegistration(
            source_id=declaration.source_id,
            block_id=declaration.block_id,
            declaration=declaration,
        )
        for declaration in declarations
    }


__all__ = [
    "PROJECTION_BLOCK_DECLARATION_DIRECTORY",
    "DeclaredBlockRegistration",
    "ProjectionBlockRegistration",
    "projection_block_declarations",
    "registered_projection_blocks",
    "release_projection_block_declaration",
    "released_projection_block_declaration",
    "write_projection_block_declaration",
]
