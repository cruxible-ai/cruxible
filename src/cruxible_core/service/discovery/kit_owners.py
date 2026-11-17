"""Which installed kit owns a definition: read from the kits' receipts, per projection.

A kit owns exactly the definitions its receipt records (``KitReceipt.artifacts``):
those it may replace and retire. Carried definitions are pinned, not owned, so
they name no owner. ``get`` on a ClaimType or SubjectKind and the ``orient``
claim_types and subject_kinds sections show the owner as ``kit``.

The cost is the installed kits', never the instance's: one range read of the
receipt Documents (``document:kit-*``) in the bound projection, and each
receipt body read and parsed once per process, keyed by its content digest.
"""

from __future__ import annotations

import sqlite3
from collections import OrderedDict
from collections.abc import Mapping
from typing import Literal

from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.claim_types import ClaimTypeFormatError, claim_type_path
from cruxible_client.contracts.errors import CruxibleError
from cruxible_client.contracts.kits import KIT_RECEIPT_DOCUMENT_KIND, KitReceipt
from cruxible_client.contracts.subject_kinds import SubjectKindFormatError, subject_kind_path
from cruxible_core.derived.memo import memo_get, memo_put
from cruxible_core.runtime.instance import PlaybillInstance

_RECEIPT_ACCESS = BodyAccessContext(principal_id="kit-receipt", can_read_body=True)
#: Owned paths by receipt body digest: immutable bytes, so parsed once per process.
_OWNED: OrderedDict[str, Mapping[str, str]] = OrderedDict()
_OWNED_CAPACITY = 1024
# Receipt Documents are named ``kit-<kit_id>``: one contiguous identity range.
_FIRST, _PAST = "document:kit-", "document:kit."


def kit_owners(instance: PlaybillInstance, connection: sqlite3.Connection) -> Mapping[str, str]:
    """Every definition path an installed kit owns at this projection -> that kit's id."""

    owners: dict[str, str] = {}
    for kind, body_digest in connection.execute(
        "SELECT document_kind, body_digest FROM documents "
        "WHERE identity >= ? AND identity < ? ORDER BY identity",
        (_FIRST, _PAST),
    ):
        if str(kind) != KIT_RECEIPT_DOCUMENT_KIND:
            continue
        owned = memo_get(_OWNED, str(body_digest))
        if owned is None:
            receipt = KitReceipt.model_validate_json(
                instance.body_store().read(str(body_digest), access=_RECEIPT_ACCESS)
            )
            owned = {entry.path: receipt.kit_id for entry in receipt.artifacts}
            memo_put(_OWNED, str(body_digest), owned, capacity=_OWNED_CAPACITY)
        owners.update(owned)
    return owners


_BUILDS: OrderedDict[str, Mapping[str, str]] = OrderedDict()


def kit_provider_owners(
    instance: PlaybillInstance, connection: sqlite3.Connection
) -> Mapping[str, str]:
    """Every provider build an installed kit bundles at this projection -> that kit's id."""

    owners: dict[str, str] = {}
    for kind, body_digest in connection.execute(
        "SELECT document_kind, body_digest FROM documents "
        "WHERE identity >= ? AND identity < ? ORDER BY identity",
        (_FIRST, _PAST),
    ):
        if str(kind) != KIT_RECEIPT_DOCUMENT_KIND:
            continue
        builds = memo_get(_BUILDS, str(body_digest))
        if builds is None:
            receipt = KitReceipt.model_validate_json(
                instance.body_store().read(str(body_digest), access=_RECEIPT_ACCESS)
            )
            builds = {provider.build: receipt.kit_id for provider in receipt.providers}
            memo_put(_BUILDS, str(body_digest), builds, capacity=_OWNED_CAPACITY)
        owners.update(builds)
    return owners


def bundled_retirement_warning(identity: str, kit_id: str) -> str:
    """The warning a retirement of a kit-bundled definition carries (S7).

    Until revive lands, a retired kit definition stays retired: kit upgrades
    keep the consumer's retirement and never re-add it.
    """

    return (
        f"{identity} is bundled by kit {kit_id}; once retired it stays retired across "
        f"{kit_id}'s upgrades"
    )


def kit_retirement_warnings(
    instance: PlaybillInstance, candidate_digest: str | None
) -> tuple[str, ...]:
    """Warnings for each kit-bundled definition one candidate retires (S7).

    Reads the candidate's members (its ``retire`` dispositions) and the
    installed kits' receipts at the head: the cost of the change and the kits,
    never the instance.
    """

    if candidate_digest is None:
        return ()
    try:
        candidate = instance.proposal_evidence().read_candidate(candidate_digest)
    except CruxibleError:
        # Advisory only: a candidate whose evidence cannot be read warns of
        # nothing here; reading the proposal itself reports why.
        return ()
    retired = [
        member.path
        for member in getattr(candidate, "members", ())
        if member.disposition == "retire"
    ]
    if not retired:
        return ()
    head = instance.accepted_coordinate()
    with instance.bind_accepted_projection(head) as projection:
        owners = kit_owners(instance, projection.typed.connection)
    warnings = []
    for path in retired:
        kit_id = owners.get(path)
        if kit_id is not None:
            warnings.append(
                bundled_retirement_warning(_path_identity(instance, head, path), kit_id)
            )
    return tuple(warnings)


def _path_identity(instance: PlaybillInstance, head: object, path: str) -> str:
    from cruxible_core.service.kits import _artifact_state

    content = instance.blob_at(head.git_oid, path)  # type: ignore[attr-defined]
    if content is None:
        return path
    try:
        return _artifact_state(path, content).identity.qualified
    except (CruxibleError, ValueError):  # a path that does not parse still names itself
        return path


def owned_by(
    owners: Mapping[str, str], family: Literal["ClaimType", "SubjectKind"], name: str
) -> str | None:
    """The kit owning one ClaimType (by predicate) or SubjectKind (by kind), if any."""

    try:
        path = claim_type_path(name) if family == "ClaimType" else subject_kind_path(name)
    except (ClaimTypeFormatError, SubjectKindFormatError):
        return None  # not path-addressable, so no kit can carry it
    return owners.get(path)


__all__ = [
    "bundled_retirement_warning",
    "kit_owners",
    "kit_provider_owners",
    "kit_retirement_warnings",
    "owned_by",
]
