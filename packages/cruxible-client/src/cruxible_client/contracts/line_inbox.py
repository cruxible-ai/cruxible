"""The Line inbox: what a Line sent to intelligence, and an agent's dispositions of it.

A ``post_inbox`` terminal posts the items its run selected, as they are, into an
operational inbox. Posting is inert: it never touches accepted state. Each item
is its payload (open instance data, canonical JSON as posted) inside a core
envelope this module declares: the Line, Procedure, run and node that posted it,
when, the Captures in its dependency closure, the dedupe key, and the agent's
disposition.

Rule of thumb for authors: propose when the automation can state the belief,
post when it cannot.

The dedupe key is the payload digest, the source Capture digests and the Line.
Posting an item whose key exists creates nothing new: it counts the
re-occurrence, and an item an agent already decided returns to ``unread`` with
that prior disposition still shown. Core does not interpret it.

Dispositions are an agent's, never automatic: ``acknowledged``, ``dismissed``
(with a reason) or ``linked`` (to a proposal or a Subject).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_client.contracts.canonical import CanonicalValue, normalize_canonical
from cruxible_client.contracts.read_values import ShownValue
from cruxible_client.contracts.temporal import ensure_utc

#: Items one page of ``inbox list`` returns when the request names no limit, and the most.
INBOX_LIST_DEFAULT_LIMIT = 50
INBOX_LIST_MAX_LIMIT = 200
#: The most items one run posts when its ``post_inbox`` terminal declares no ``max_items``,
#: and the most any terminal may declare.
INBOX_RUN_ITEM_CAP_DEFAULT = 100
INBOX_RUN_ITEM_CAP_MAX = 1_000
#: The longest dismissal reason recorded.
INBOX_REASON_MAX_CHARS = 1_000

#: An item id is ``INB-`` and the 64 hex digits of its dedupe key; a selector may
#: name a unique prefix of at least 12 hex digits.
INBOX_ITEM_ID_PATTERN = r"^INB-[0-9a-f]{64}$"
INBOX_ITEM_SELECTOR_PATTERN = r"^INB-[0-9a-f]{12,64}$"
#: A Subject reference (``kind/id``); the same grammar as ``contracts.write.SUBJECT_REF_PATTERN``,
#: restated so this module stays importable from the contracts package root.
INBOX_SUBJECT_REF_PATTERN = (
    r"^[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})*/[a-z][a-z0-9_.-]{0,255}$"
)
_ITEM_ID = re.compile(INBOX_ITEM_ID_PATTERN)
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")

InboxItemStatus: TypeAlias = Literal["unread", "acknowledged", "dismissed", "linked"]


class _StrictInboxModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("inbox timestamps must be timezone-aware")
    return ensure_utc(value)


def _digest(value: str) -> str:
    if not _DIGEST.fullmatch(value):
        raise ValueError("expected a tagged lowercase SHA-256 digest")
    return value


def inbox_item_id(dedupe_key: str) -> str:
    """The served item id for one dedupe key."""

    return "INB-" + _digest(dedupe_key).removeprefix("sha256:")


class InboxLinkProposal(_StrictInboxModel):
    """Link an item to the proposal an agent wrote about it."""

    kind: Literal["proposal"] = "proposal"
    proposal_id: str = Field(min_length=1, max_length=256)


class InboxLinkSubject(_StrictInboxModel):
    """Link an item to a Subject (``kind/id``), such as a note about it."""

    kind: Literal["subject"] = "subject"
    subject: str = Field(pattern=INBOX_SUBJECT_REF_PATTERN)


InboxLinkTarget: TypeAlias = Annotated[
    InboxLinkProposal | InboxLinkSubject, Field(discriminator="kind")
]


class InboxAcknowledged(_StrictInboxModel):
    """An agent saw the item and owes it nothing further."""

    tag: Literal["cruxible-inbox-acknowledged-v1"] = "cruxible-inbox-acknowledged-v1"
    disposition: Literal["acknowledged"] = "acknowledged"
    actor_id: str
    decided_at: datetime

    _time = field_validator("decided_at")(_utc)


class InboxDismissed(_StrictInboxModel):
    """An agent judged the item needs nothing, and said why."""

    tag: Literal["cruxible-inbox-dismissed-v1"] = "cruxible-inbox-dismissed-v1"
    disposition: Literal["dismissed"] = "dismissed"
    actor_id: str
    decided_at: datetime
    reason: str = Field(min_length=1, max_length=INBOX_REASON_MAX_CHARS)

    _time = field_validator("decided_at")(_utc)


class InboxLinked(_StrictInboxModel):
    """An agent recorded where its judgment of the item lives: a proposal or a Subject."""

    tag: Literal["cruxible-inbox-linked-v1"] = "cruxible-inbox-linked-v1"
    disposition: Literal["linked"] = "linked"
    actor_id: str
    decided_at: datetime
    target: InboxLinkTarget

    _time = field_validator("decided_at")(_utc)


InboxDisposition: TypeAlias = Annotated[
    InboxAcknowledged | InboxDismissed | InboxLinked, Field(discriminator="disposition")
]


class InboxItemEnvelope(_StrictInboxModel):
    """The core envelope of one inbox item: everything about it except its payload.

    ``run_id``/``posted_at`` are the first posting; ``last_run_id``/``last_posted_at``
    the latest. ``occurrences`` counts every run that posted the same dedupe key.
    ``disposition`` is the agent's latest decision, kept when the item recurs:
    a recurring item is ``unread`` again, and ``recurred_since_disposition``
    counts the postings after that decision.
    """

    tag: Literal["cruxible-inbox-item-envelope-v1"] = "cruxible-inbox-item-envelope-v1"
    item_id: str = Field(pattern=INBOX_ITEM_ID_PATTERN)
    status: InboxItemStatus
    line: str
    procedure: str
    procedure_artifact_digest: str
    node_id: str
    run_id: str
    posted_at: datetime
    last_run_id: str
    last_posted_at: datetime
    occurrences: int = Field(ge=1)
    source_capture_digests: tuple[str, ...] = ()
    dedupe_key: str
    payload_digest: str
    payload_bytes: int = Field(ge=0)
    disposition: InboxDisposition | None = None
    recurred_since_disposition: int = Field(default=0, ge=0)

    _times = field_validator("posted_at", "last_posted_at")(_utc)
    _digests = field_validator("procedure_artifact_digest", "dedupe_key", "payload_digest")(_digest)

    @field_validator("source_capture_digests")
    @classmethod
    def _captures(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            _digest(item)
        if value != tuple(sorted(set(value))):
            raise ValueError("source Capture digests are sorted and unique")
        return value


class InboxListEntry(_StrictInboxModel):
    """One listed item: its envelope and a bounded preview of its payload.

    ``payload_preview`` follows the summary-read conventions: a long string is a
    ``TruncatedText``, an object a ``ShownObject``. A payload whose canonical JSON
    runs past the summary bound is shown as a ``TruncatedText`` of that JSON
    text. ``inbox get`` returns the whole payload.
    """

    tag: Literal["cruxible-inbox-list-entry-v1"] = "cruxible-inbox-list-entry-v1"
    item: InboxItemEnvelope
    payload_preview: ShownValue


class InboxList(_StrictInboxModel):
    """One page of inbox items, most recently posted first."""

    tag: Literal["cruxible-inbox-list-v1"] = "cruxible-inbox-list-v1"
    line_filter: str | None = None
    status_filter: InboxItemStatus | None = None
    entries: tuple[InboxListEntry, ...]
    truncated: bool = False
    next_cursor: str | None = None


class InboxItem(_StrictInboxModel):
    """One inbox item whole: envelope, the payload exactly as posted, and every disposition."""

    tag: Literal["cruxible-inbox-item-v1"] = "cruxible-inbox-item-v1"
    item: InboxItemEnvelope
    payload: object
    dispositions: tuple[InboxDisposition, ...] = ()

    @field_validator("payload", mode="before")
    @classmethod
    def _payload(cls, value: object) -> CanonicalValue:
        return normalize_canonical(value)


class InboxDismissRequest(_StrictInboxModel):
    tag: Literal["cruxible-inbox-dismiss-request-v1"] = "cruxible-inbox-dismiss-request-v1"
    reason: str = Field(min_length=1, max_length=INBOX_REASON_MAX_CHARS)


class InboxLinkRequest(_StrictInboxModel):
    tag: Literal["cruxible-inbox-link-request-v1"] = "cruxible-inbox-link-request-v1"
    target: InboxLinkTarget


class InboxDispositionResult(_StrictInboxModel):
    """The item after a disposition. ``recorded`` is false when it repeated the current one."""

    tag: Literal["cruxible-inbox-disposition-result-v1"] = "cruxible-inbox-disposition-result-v1"
    item: InboxItemEnvelope
    recorded: bool


def is_inbox_item_id(value: str) -> bool:
    return _ITEM_ID.fullmatch(value) is not None


__all__ = [
    "INBOX_ITEM_ID_PATTERN",
    "INBOX_ITEM_SELECTOR_PATTERN",
    "INBOX_LIST_DEFAULT_LIMIT",
    "INBOX_LIST_MAX_LIMIT",
    "INBOX_REASON_MAX_CHARS",
    "INBOX_RUN_ITEM_CAP_DEFAULT",
    "INBOX_RUN_ITEM_CAP_MAX",
    "INBOX_SUBJECT_REF_PATTERN",
    "InboxAcknowledged",
    "InboxDismissRequest",
    "InboxDismissed",
    "InboxDisposition",
    "InboxDispositionResult",
    "InboxItem",
    "InboxItemEnvelope",
    "InboxItemStatus",
    "InboxLinkProposal",
    "InboxLinkRequest",
    "InboxLinkSubject",
    "InboxLinkTarget",
    "InboxLinked",
    "InboxList",
    "InboxListEntry",
    "inbox_item_id",
    "is_inbox_item_id",
]
