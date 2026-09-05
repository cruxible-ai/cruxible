"""The registration fold over durable publication intents.

The AuthoringIntent stream is protocol state, not a second governed truth plane.
``next``, coverage, the detach refusal and claim lowering all read this one fold
so they cannot disagree about which blocks an instance registers. It lives under
``playbill/authoring`` rather than the service layer because lowering -- which
may not import a service module -- has to ask it which sources carry projection
blocks before it admits a citation into one.
"""

from __future__ import annotations

import json
import os
import re
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cruxible_client.contracts.authoring.models import PublicationPreparationV2
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import PlaybillError, PlaybillFormatError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.playbill.authoring.store import AuthoringIntentStore
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.memo import memo_get, memo_put


@dataclass(frozen=True)
class BoundPublicationRegistration:
    """The exact association established by one confirmed publication."""

    intent_id: str
    claim_identity: str
    claim_statement_digest: str
    preparation: PublicationPreparationV2


# The fold consumes validated current publication states. One `block sync --check` can
# ask the same question once per block, so its result is memoized separately
# from the store's event validation. The identity names the exact stream folded:
# every event file's directory, name, inode, size and both timestamps. Any
# append or rewrite moves it, so a changed stream is folded again.
_SAFE_SEGMENT = re.compile(r"[a-z][a-z0-9_.-]{0,127}")
_REGISTRATION_MEMO_CAPACITY = 4
_REGISTRATION_MEMO: "OrderedDict[tuple[object, ...], tuple[BoundPublicationRegistration, ...]]" = (
    OrderedDict()
)


def _intent_stream_identity(root: Path) -> tuple[object, ...] | None:
    """Name the durable event stream without reading a single event."""

    entries: list[tuple[object, ...]] = []
    try:
        for directory in sorted(root.glob("AIT-*"), key=lambda item: item.name):
            events = directory / "events"
            if not events.is_dir():
                continue
            for path in sorted(events.glob("*.json"), key=lambda item: item.name):
                metadata = os.lstat(path)
                entries.append(
                    (
                        directory.name,
                        path.name,
                        metadata.st_mode,
                        metadata.st_dev,
                        metadata.st_ino,
                        metadata.st_size,
                        metadata.st_mtime_ns,
                        metadata.st_ctime_ns,
                    )
                )
    except OSError:
        return None
    return (str(root), tuple(entries))


def reset_bound_publication_registration_memo() -> None:
    """Forget every in-process publication fold."""

    _REGISTRATION_MEMO.clear()


def bound_publication_registrations(
    instance: PlaybillInstance,
) -> tuple[BoundPublicationRegistration, ...] | None:
    """Fold latest intent events, or return ``None`` when the fold is unavailable."""

    exhaust_root = instance.root / instance.descriptor.storage.exhaust
    intent_root = exhaust_root / "authoring-intents"
    if not intent_root.is_dir():
        return ()
    identity = _intent_stream_identity(intent_root)
    if identity is not None:
        memoized = memo_get(_REGISTRATION_MEMO, identity)
        if memoized is not None:
            return memoized
    try:
        latest = AuthoringIntentStore(exhaust_root, read_only=True).publication_states()
    except (OSError, PlaybillError):
        return None
    # Every expectation the intent owns, not just the singular mirror: one intent
    # is one changeset, so a set that published three Claims registers three
    # blocks, and the two it did not fold read back as orphan markers.
    registrations = [
        BoundPublicationRegistration(
            intent_id=intent.intent_id,
            claim_identity=expectation.claim_identity,
            claim_statement_digest=expectation.claim_statement_digest,
            preparation=expectation.preparation,
        )
        for intent in latest
        for expectation in intent.insertion_expectations
        if expectation.state == "bound" and expectation.preparation is not None
    ]
    folded = tuple(
        sorted(
            registrations,
            key=lambda item: (
                item.preparation.source_id.encode("utf-8"),
                item.preparation.block_id.encode("ascii"),
                item.claim_identity.encode("ascii"),
                item.intent_id.encode("ascii"),
            ),
        )
    )
    if identity is not None:
        memo_put(_REGISTRATION_MEMO, identity, folded, capacity=_REGISTRATION_MEMO_CAPACITY)
    return folded


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
    """One registered block, whichever road declared it.

    The identity is the fold's own -- the pair the page names, a source and a
    block -- and not a string prefix. A block minted by the retired publication
    road carried `pub-` in its id and could be recognized by spelling; a block
    an agent declares chooses its own id, so the only honest answer to "is this
    marker sanctioned?" is the one the instance keeps.
    """

    source_id: str
    block_id: str
    origin: Literal["publication", "declaration"]
    publication: BoundPublicationRegistration | None = None
    declaration: DeclaredBlockRegistration | None = None

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
        raise PlaybillFormatError(
            "playbill.block.declaration_identity_invalid: a block declaration is addressed "
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
        raise PlaybillFormatError(
            "playbill.block.declaration_unreleasable: the block declaration store cannot "
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
    """Every block this instance registers, folded from both declaration roads.

    ``None`` when either road cannot be read: an unreadable registry is not an
    empty one, and every consumer of this fold refuses rather than concluding a
    marker is unsanctioned because its record could not be opened.
    """

    publications = bound_publication_registrations(instance)
    declarations = projection_block_declarations(instance)
    if publications is None or declarations is None:
        return None
    folded: dict[tuple[str, str], ProjectionBlockRegistration] = {}
    for declaration in declarations:
        key = (declaration.source_id, declaration.block_id)
        folded[key] = ProjectionBlockRegistration(
            source_id=declaration.source_id,
            block_id=declaration.block_id,
            origin="declaration",
            declaration=declaration,
        )
    # A publication registration is the older road and the stronger claim: it is
    # a confirmed insertion whose Claim the instance still holds, so it wins the
    # pair if both roads somehow name it.
    for publication in publications:
        key = (publication.preparation.source_id, publication.preparation.block_id)
        folded[key] = ProjectionBlockRegistration(
            source_id=key[0],
            block_id=key[1],
            origin="publication",
            publication=publication,
        )
    return folded


__all__ = [
    "PROJECTION_BLOCK_DECLARATION_DIRECTORY",
    "BoundPublicationRegistration",
    "DeclaredBlockRegistration",
    "ProjectionBlockRegistration",
    "bound_publication_registrations",
    "projection_block_declarations",
    "registered_projection_blocks",
    "release_projection_block_declaration",
    "released_projection_block_declaration",
    "reset_bound_publication_registration_memo",
    "write_projection_block_declaration",
]
