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


def owned_by(
    owners: Mapping[str, str], family: Literal["ClaimType", "SubjectKind"], name: str
) -> str | None:
    """The kit owning one ClaimType (by predicate) or SubjectKind (by kind), if any."""

    try:
        path = claim_type_path(name) if family == "ClaimType" else subject_kind_path(name)
    except (ClaimTypeFormatError, SubjectKindFormatError):
        return None  # not path-addressable, so no kit can carry it
    return owners.get(path)


__all__ = ["kit_owners", "owned_by"]
