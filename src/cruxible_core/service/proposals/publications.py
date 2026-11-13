"""The verbs and derived reads over the blocks an instance registers.

The fold itself lives in ``cruxible_core.authoring.registrations`` so claim
lowering can read it; it is re-exported here for every service-layer reader
that already imports it from this module. `block repin` declares a block and
`block depublish` releases it, keyed on the pair the page itself names.

The re-exported names are deliberately unused here: this module is the
service-layer door to that fold, and a reader that already imports through it
must keep working.
"""

from __future__ import annotations

import hashlib

from cruxible_client.contracts import (
    AcceptedCoordinate,
    BlockDeclareResult,
    BlockDepublishResult,
)
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.declared_blocks import ProjectionBlockStampAny
from cruxible_client.contracts.errors import FormatError
from cruxible_core.authoring.registrations import (
    DeclaredBlockRegistration,
    ProjectionBlockRegistration,
    projection_block_declarations,
    registered_projection_blocks,
    release_projection_block_declaration,
    released_projection_block_declaration,
    write_projection_block_declaration,
)
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.change_preview import ChangeMode, change_scope


def service_declare_playbill_block(
    instance: PlaybillInstance,
    *,
    actor_id: str,
    stamp: ProjectionBlockStampAny,
    declared_at: str,
) -> BlockDeclareResult:
    """Register one projection block the workspace just stamped.

    `next` asks of every marker it observes whether this instance stands behind
    it, and this declaration is the instance's answer.

    The declaration is protocol state and commits nothing about what the block
    SAYS -- the stamp in the page is that -- so it is idempotent by pair and a
    re-stamp simply replaces it.
    """

    instance.require_writable()
    coordinate = AcceptedCoordinate.model_validate(
        AcceptedCoordinate.from_internal(instance.accepted_coordinate()).model_dump(mode="json")
    )
    existing = projection_block_declarations(instance)
    if existing is None:
        raise FormatError(
            "cruxible.block.declaration_registry_unavailable: the block declaration store "
            "cannot be read; repair: restore the instance exhaust and retry"
        )
    known = any(
        item.source_id == stamp.source_id and item.block_id == stamp.block_id for item in existing
    )
    write_projection_block_declaration(
        instance,
        source_id=stamp.source_id,
        block_id=stamp.block_id,
        declared_generation=stamp.declared_generation,
        declared_coordinate=AcceptedCoordinate.model_validate(
            stamp.declared_coordinate.model_dump(mode="json")
        ),
        declared_by=actor_id,
        declared_at=declared_at,
        stamp_digest=projection_block_stamp_digest(stamp),
    )
    return BlockDeclareResult(
        source_id=stamp.source_id,
        block_id=stamp.block_id,
        outcome="redeclared" if known else "declared",
        declared_generation=stamp.declared_generation,
        coordinate=coordinate,
    )


def projection_block_stamp_digest(stamp: ProjectionBlockStampAny) -> str:
    """The declaration's fingerprint of the marker it was taken from."""

    return "sha256:" + hashlib.sha256(canonical_bytes(stamp.model_dump(mode="json"))).hexdigest()


def service_depublish_playbill_block(
    instance: PlaybillInstance,
    *,
    source_id: str,
    block_id: str,
    dry_run: bool | None = None,
    at: str | None = None,
) -> BlockDepublishResult:
    """Release the declaration that registers one page block.

    A registration nothing released kept `next` demanding the frame for a block
    a later ruling had removed, with the repair "restore it". Releasing it is the
    transition out. Idempotent by construction: a released declaration leaves a
    tombstone that says so, so a caller who asks twice is answered, not refused.

    ``dry_run`` runs every check and releases nothing (``would_depublish``, R12).
    """

    instance.require_writable()
    with change_scope(
        instance,
        dry_run=dry_run,
        at=at,
        kind="direct",
        operation="cruxible.block.depublish",
        describe=f"depublishing block {source_id}#{block_id}",
    ) as mode:
        return _depublish(instance, mode, source_id=source_id, block_id=block_id)


def _depublish(
    instance: PlaybillInstance,
    mode: ChangeMode,
    *,
    source_id: str,
    block_id: str,
) -> BlockDepublishResult:
    coordinate = AcceptedCoordinate.model_validate(
        AcceptedCoordinate.from_internal(instance.accepted_coordinate()).model_dump(mode="json")
    )
    declarations = projection_block_declarations(instance)
    if declarations is None:
        raise FormatError(
            "cruxible.block.declaration_registry_unavailable: the block declaration store "
            "cannot be read; repair: restore the instance exhaust and retry"
        )
    declared = any(
        item.source_id == source_id and item.block_id == block_id for item in declarations
    )
    if declared:
        if not mode.previewing:
            with mode.committing():
                release_projection_block_declaration(
                    instance,
                    source_id=source_id,
                    block_id=block_id,
                )
        return BlockDepublishResult(
            source_id=source_id,
            block_id=block_id,
            outcome="would_depublish" if mode.previewing else "depublished",
            coordinate=coordinate,
        )
    if released_projection_block_declaration(instance, source_id=source_id, block_id=block_id):
        # Releasing a registration is idempotent by contract, and a declaration
        # this instance once held and has already released must say so rather
        # than refuse.
        return BlockDepublishResult(
            source_id=source_id,
            block_id=block_id,
            outcome="already_depublished",
            coordinate=coordinate,
        )
    raise FormatError(
        f"cruxible.block.not_registered: this instance registers no block "
        f"{source_id}#{block_id}; repair: read the registered blocks with `cruxible next` "
        "before releasing one"
    )


__all__ = [
    "DeclaredBlockRegistration",
    "ProjectionBlockRegistration",
    "projection_block_declarations",
    "projection_block_stamp_digest",
    "registered_projection_blocks",
    "release_projection_block_declaration",
    "released_projection_block_declaration",
    "service_declare_playbill_block",
    "service_depublish_playbill_block",
    "write_projection_block_declaration",
]
