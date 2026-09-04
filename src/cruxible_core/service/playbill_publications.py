"""The verbs and derived reads over the blocks an instance registers.

The folds themselves live in ``cruxible_core.playbill.authoring.registrations``
so claim lowering can read them; they are re-exported here for every
service-layer reader that already imports them from this module. Two roads
declare a block -- the retired publication road, folded from durable intents,
and `block repin`, which records a declaration -- and both answer one verb and
one fold, keyed on the pair the page itself names.

The re-exported names are deliberately unused here: this module is the
service-layer door to that fold, and a reader that already imports through it
must keep working.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from cruxible_client.contracts import (
    PlaybillAcceptedCoordinate,
    PlaybillBlockDeclareResultV1,
    PlaybillBlockDepublishResultV1,
)
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.declared_blocks import ProjectionBlockStampV1
from cruxible_client.contracts.errors import PlaybillError, PlaybillFormatError
from cruxible_core.playbill.authoring.registrations import (
    BoundPublicationRegistration,
    DeclaredBlockRegistration,
    ProjectionBlockRegistration,
    bound_publication_registrations,
    projection_block_declarations,
    registered_projection_blocks,
    release_projection_block_declaration,
    released_projection_block_declaration,
    reset_bound_publication_registration_memo,
    write_projection_block_declaration,
)
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import AcceptedCoordinate

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.playbill.proposals import AuthenticatedActor


def service_declare_playbill_block(
    instance: PlaybillInstance,
    *,
    actor_id: str,
    stamp: ProjectionBlockStampV1,
    declared_at: str,
) -> PlaybillBlockDeclareResultV1:
    """Register one projection block the workspace just stamped.

    `next` asks of every marker it observes whether this instance stands behind
    it. For a block minted by the retired publication road the answer came from
    a fold over durable intents; for a block an agent declared, there was no
    record at all, so the question was answered by whether the block id happened
    to start with `pub-`. It is answered by the instance now, for both roads.

    The declaration is protocol state and commits nothing about what the block
    SAYS -- the stamp in the page is that -- so it is idempotent by pair and a
    re-stamp simply replaces it.
    """

    instance.require_writable()
    coordinate = PlaybillAcceptedCoordinate.model_validate(
        AcceptedCoordinate.from_internal(instance.accepted_coordinate()).model_dump(mode="json")
    )
    existing = projection_block_declarations(instance)
    if existing is None:
        raise PlaybillFormatError(
            "playbill.block.declaration_registry_unavailable: the block declaration store "
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
    return PlaybillBlockDeclareResultV1(
        source_id=stamp.source_id,
        block_id=stamp.block_id,
        outcome="redeclared" if known else "declared",
        declared_generation=stamp.declared_generation,
        coordinate=coordinate,
    )


def projection_block_stamp_digest(stamp: ProjectionBlockStampV1) -> str:
    """The declaration's fingerprint of the marker it was taken from."""

    return "sha256:" + hashlib.sha256(canonical_bytes(stamp.model_dump(mode="json"))).hexdigest()


def service_depublish_playbill_block(
    instance: PlaybillInstance,
    *,
    coordinator: "AuthoringIntentCoordinator",
    actor: "AuthenticatedActor",
    source_id: str,
    block_id: str,
) -> PlaybillBlockDepublishResultV1:
    """Release the bound publication registration that demands one page block.

    A registration is folded from a `bound` insertion expectation and nothing
    ever released it, so `next` demanded the frame for a block a later ruling
    had removed, and the repair it named was to restore it. Abandoning the
    expectation is the transition out; the expectation keeps its preparation, so
    the record still says which block was published and which was taken down.

    Idempotent by construction, on both roads: a released publication no longer
    folds and the expectation it released says so, and a released declaration
    leaves a tombstone that says the same. Neither mints an identity, and
    neither refuses a caller who asks twice.
    """

    instance.require_writable()
    coordinate = PlaybillAcceptedCoordinate.model_validate(
        AcceptedCoordinate.from_internal(instance.accepted_coordinate()).model_dump(mode="json")
    )
    registrations = bound_publication_registrations(instance)
    if registrations is None:
        raise PlaybillFormatError(
            "playbill.block.publication_registry_unavailable: the durable publication "
            "stream cannot be read; repair: restore the instance exhaust and retry"
        )
    declarations = projection_block_declarations(instance)
    if declarations is None:
        raise PlaybillFormatError(
            "playbill.block.declaration_registry_unavailable: the block declaration store "
            "cannot be read; repair: restore the instance exhaust and retry"
        )
    # A declared block has no intent to abandon: releasing it is forgetting the
    # declaration. Both roads answer the same verb, because the page names a
    # source and a block and knows nothing about which road registered it.
    declared = any(
        item.source_id == source_id and item.block_id == block_id for item in declarations
    )
    if declared:
        release_projection_block_declaration(
            instance,
            source_id=source_id,
            block_id=block_id,
        )
        return PlaybillBlockDepublishResultV1(
            source_id=source_id,
            block_id=block_id,
            origin="declaration",
            outcome="depublished",
            coordinate=coordinate,
        )
    if released_projection_block_declaration(instance, source_id=source_id, block_id=block_id):
        # Releasing a registration is idempotent by contract, and a declaration
        # this instance once held and has already released must say so rather
        # than refuse by naming a publication that never existed.
        return PlaybillBlockDepublishResultV1(
            source_id=source_id,
            block_id=block_id,
            origin="declaration",
            outcome="already_depublished",
            coordinate=coordinate,
        )
    matched = tuple(
        item
        for item in registrations
        if item.preparation.source_id == source_id and item.preparation.block_id == block_id
    )
    if not matched:
        released = _released_publication_expectation(instance, source_id, block_id)
        if released is None:
            raise PlaybillFormatError(
                f"playbill.block.not_registered: this instance registers no block "
                f"{source_id}#{block_id}, by declaration or by publication; repair: read "
                "the registered blocks with `cruxible playbill next` before releasing one"
            )
        intent_id, expectation_id, claim_identity = released
        return PlaybillBlockDepublishResultV1(
            source_id=source_id,
            block_id=block_id,
            intent_id=intent_id,
            expectation_id=expectation_id,
            outcome="already_depublished",
            claim_identity=claim_identity,
            coordinate=coordinate,
        )
    if len(matched) > 1:
        raise PlaybillFormatError(
            f"playbill.block.publication_registration_ambiguous: {len(matched)} bound "
            f"publications register {source_id}#{block_id}; repair: abandon each intent "
            "through `cruxible playbill authoring abandon-insertion`"
        )
    registration = matched[0]
    result = coordinator.abandon_insertion(
        registration.intent_id,
        actor=actor,
        expectation_id=registration.preparation.expectation_id,
    )
    return PlaybillBlockDepublishResultV1(
        source_id=source_id,
        block_id=block_id,
        intent_id=registration.intent_id,
        expectation_id=result.expectation.expectation_id,
        outcome="depublished",
        claim_identity=registration.claim_identity,
        coordinate=coordinate,
    )


def _released_publication_expectation(
    instance: PlaybillInstance,
    source_id: str,
    block_id: str,
) -> tuple[str, str, str] | None:
    """Find an expectation that once published this block and no longer registers it."""

    exhaust_root = instance.root / instance.descriptor.storage.exhaust
    try:
        latest = {
            event.intent.intent_id: event.intent
            for event in AuthoringIntentStore(exhaust_root, read_only=True).events()
        }
    except (OSError, PlaybillError):
        return None
    for intent in latest.values():
        for expectation in intent.insertion_expectations:
            preparation = expectation.preparation
            if preparation is None:
                continue
            if preparation.source_id != source_id or preparation.block_id != block_id:
                continue
            if expectation.state == "bound":
                continue
            return (intent.intent_id, expectation.expectation_id, expectation.claim_identity)
    return None


__all__ = [
    "BoundPublicationRegistration",
    "DeclaredBlockRegistration",
    "ProjectionBlockRegistration",
    "bound_publication_registrations",
    "projection_block_declarations",
    "projection_block_stamp_digest",
    "registered_projection_blocks",
    "release_projection_block_declaration",
    "released_projection_block_declaration",
    "reset_bound_publication_registration_memo",
    "service_declare_playbill_block",
    "service_depublish_playbill_block",
    "write_projection_block_declaration",
]
