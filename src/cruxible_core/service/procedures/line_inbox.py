"""The Line inbox surface: list, read, and an agent's dispositions.

Reads are keyed and bounded. Dispositions are an agent's judgment, recorded
as typed transitions in the inbox journal; none of them reads or writes
accepted state, except that a link to a proposal or a Subject must name one
that exists.
"""

from __future__ import annotations

from datetime import datetime

from cruxible_client.contracts.errors import SubjectNotFoundError
from cruxible_client.contracts.line_inbox import (
    INBOX_ITEM_SELECTOR_PATTERN,
    INBOX_LIST_MAX_LIMIT,
    InboxAcknowledged,
    InboxDismissed,
    InboxDisposition,
    InboxDispositionResult,
    InboxItem,
    InboxItemStatus,
    InboxLinked,
    InboxLinkProposal,
    InboxLinkTarget,
    InboxList,
    InboxListEntry,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.repairs import RepairOperation
from cruxible_core.exhaust.line_inbox import InboxItemNotFound, LineInboxStore
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.procedures.procedure_runs import ProcedureSurfaceError

_INBOX_LIST_REPAIR = RepairOperation(operation="cruxible.inbox.list")


class InboxItemNotFoundError(ProcedureSurfaceError):
    """No inbox item matches the selector, or a prefix matches several."""

    code = "cruxible.inbox.item_not_found"
    error_code = code
    http_status = 404
    repair = _INBOX_LIST_REPAIR


class InboxRequestInvalid(ProcedureSurfaceError):
    """An inbox request names a cursor or filter this surface does not serve."""

    code = "cruxible.inbox.request_invalid"
    error_code = code
    http_status = 400
    repair = _INBOX_LIST_REPAIR


def inbox_line_filter(line: str | None) -> str | None:
    """A Line named as ``name`` or ``Line:name``, as the envelope records it."""

    if line is None:
        return None
    name = line.removeprefix("Line:")
    if not name:
        raise InboxRequestInvalid(f"{InboxRequestInvalid.code}: an empty Line name filters nothing")
    return f"Line:{name}"


def _resolve(store: LineInboxStore, selector: str) -> str:
    try:
        return store.resolve(selector)
    except InboxItemNotFound as exc:
        detail = "names more than one item" if exc.ambiguous else "names no item"
        raise InboxItemNotFoundError(
            f"{InboxItemNotFoundError.code}: inbox selector {selector!r} {detail}; name an "
            f"item id or a unique prefix ({INBOX_ITEM_SELECTOR_PATTERN}); run "
            "`cruxible inbox list`"
        ) from exc


def service_list_inbox(
    instance: PlaybillInstance,
    *,
    line: str | None = None,
    status: InboxItemStatus | None = None,
    limit: int,
    cursor: str | None = None,
) -> InboxList:
    if not 1 <= limit <= INBOX_LIST_MAX_LIMIT:
        raise InboxRequestInvalid(
            f"{InboxRequestInvalid.code}: limit must be between 1 and {INBOX_LIST_MAX_LIMIT}"
        )
    line_filter = inbox_line_filter(line)
    try:
        items, next_cursor = LineInboxStore(instance).page(
            line=line_filter, status=status, limit=limit, cursor=cursor
        )
    except ValueError as exc:
        raise InboxRequestInvalid(f"{InboxRequestInvalid.code}: {exc}") from exc
    return InboxList(
        line_filter=line_filter,
        status_filter=status,
        entries=tuple(
            InboxListEntry(item=item.envelope, payload_preview=item.preview) for item in items
        ),
        truncated=next_cursor is not None,
        next_cursor=next_cursor,
    )


def service_get_inbox_item(instance: PlaybillInstance, *, item: str) -> InboxItem:
    store = LineInboxStore(instance)
    stored = store.get(_resolve(store, item))
    return InboxItem(
        item=stored.envelope,
        payload=stored.payload,
        recent_dispositions=stored.recent_dispositions,
    )


def _record(
    instance: PlaybillInstance,
    *,
    item: str,
    disposition: InboxDisposition,
    actor_context: GovernedActorContext,
    decided_at: datetime,
) -> InboxDispositionResult:
    instance.require_writable()
    store = LineInboxStore(instance)
    envelope, recorded = store.record_disposition(
        _resolve(store, item),
        disposition,
        actor=actor_context,
        now=decided_at,
        coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
    )
    return InboxDispositionResult(item=envelope, recorded=recorded)


def service_acknowledge_inbox_item(
    instance: PlaybillInstance,
    *,
    item: str,
    actor_context: GovernedActorContext,
    decided_at: datetime,
) -> InboxDispositionResult:
    return _record(
        instance,
        item=item,
        disposition=InboxAcknowledged(actor_id=actor_context.actor_id, decided_at=decided_at),
        actor_context=actor_context,
        decided_at=decided_at,
    )


def service_dismiss_inbox_item(
    instance: PlaybillInstance,
    *,
    item: str,
    reason: str,
    actor_context: GovernedActorContext,
    decided_at: datetime,
) -> InboxDispositionResult:
    return _record(
        instance,
        item=item,
        disposition=InboxDismissed(
            actor_id=actor_context.actor_id, decided_at=decided_at, reason=reason
        ),
        actor_context=actor_context,
        decided_at=decided_at,
    )


def _existing_target(instance: PlaybillInstance, target: InboxLinkTarget) -> InboxLinkTarget:
    """The link target, canonicalized; a proposal or Subject that does not exist refuses."""

    if isinstance(target, InboxLinkProposal):
        from cruxible_core.service.proposals.proposals import (
            service_resolve_playbill_proposal_selector,
        )

        resolved = service_resolve_playbill_proposal_selector(instance, selector=target.proposal_id)
        return InboxLinkProposal(proposal_id=resolved.proposal_id)
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        if projection.subject(f"Subject:{target.subject}") is None:
            raise SubjectNotFoundError(
                f"cruxible.subject_not_found: Subject {target.subject!r} is not accepted; "
                "link the item to an accepted Subject or to the proposal that adds it"
            )
    return target


def service_link_inbox_item(
    instance: PlaybillInstance,
    *,
    item: str,
    target: InboxLinkTarget,
    actor_context: GovernedActorContext,
    decided_at: datetime,
) -> InboxDispositionResult:
    return _record(
        instance,
        item=item,
        disposition=InboxLinked(
            actor_id=actor_context.actor_id,
            decided_at=decided_at,
            target=_existing_target(instance, target),
        ),
        actor_context=actor_context,
        decided_at=decided_at,
    )


__all__ = [
    "InboxItemNotFoundError",
    "InboxRequestInvalid",
    "inbox_line_filter",
    "service_acknowledge_inbox_item",
    "service_dismiss_inbox_item",
    "service_get_inbox_item",
    "service_link_inbox_item",
    "service_list_inbox",
]
