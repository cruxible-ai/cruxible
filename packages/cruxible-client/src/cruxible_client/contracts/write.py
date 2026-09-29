"""The write verbs: ``set``, ``retire`` and ``write``.

Three verbs change accepted state, identically on MCP, the CLI, HTTP and the SDK:

- ``set`` puts one value in one field of one Subject. On a single-value field it
  replaces the live value (the Claim it revises is inferred);
- ``retire`` ends one live Claim, named by ID or by its Subject and field;
- ``write`` applies a batch of ``set``, ``add`` and ``retire`` changes as one
  change set.

Every write lowers to the ordinary authoring change set and goes through the
same coordinator, preflight, submit, review and activation as any other
authored change; these models are only its typed front door. The answer is an
outcome, not a record: what changed (before and after), the Subjects added, the
proposal, the coordinate it is pinned to, and what is still needed.

Instance vocabulary -- field names and enum members -- stays open here and is
checked by the daemon against the accepted ClaimTypes; a wrong name refuses with
the nearest valid names.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_client.contracts import PlaybillAcceptedCoordinate
from cruxible_client.contracts.authoring.models import WorkingSelectionObservationV1
from cruxible_client.contracts.get_reads import PlaybillGetCoordinateV1, PlaybillReadSurface

SUBJECT_REF_PATTERN = r"^[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})*/[a-z][a-z0-9_.-]{0,255}$"
CLAIM_ID_PATTERN = r"^(?:Claim:)?CLM-[0-9a-f]{32}$"
CAPTURE_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
FILE_ANCHOR_PATTERN = r"^[^#]+#.+$"
_GIT_OID = re.compile(r"^[0-9a-f]{1,64}$")

SubjectRef = Annotated[
    str,
    Field(
        pattern=SUBJECT_REF_PATTERN,
        description="A Subject as kind/id, for example 'dev.roadmap_item/tidy-cli'.",
    ),
]
ClaimId = Annotated[
    str,
    Field(pattern=CLAIM_ID_PATTERN, description="A Claim ID: CLM-… or Claim:CLM-…."),
]
FieldName = Annotated[
    str,
    Field(
        min_length=1,
        max_length=512,
        description=(
            "A field of the Subject's kind: the predicate without the kind prefix "
            "(orient lists them), or the full predicate."
        ),
    ),
]
ClaimValue = bool | int | float | str
"""A scalar value. A Subject-valued field takes the Subject as kind/id; an
exact-content field takes the text itself."""

WriteRole = Literal["normative", "observation", "environment_binding"]
WriteAccept = Literal["if_allowed", "never"]
WriteRetireReason = Literal["was-rescinded", "was-wrong", "superseded"]
WriteStatus = Literal[
    "accepted",
    "awaiting_approval",
    "refused",
    "would_accept",
    "would_await_approval",
    "would_refuse",
]
WriteOp = Literal["set", "add", "retire"]
ApprovalReason = Literal[
    "independent_approval_required", "activation_not_permitted", "accept_never"
]


class _StrictWriteModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _omit_none(value: object) -> bool:
    return value is None


def _omit_empty(value: object) -> bool:
    return value in (None, (), [])


# -- evidence -------------------------------------------------------------------


class SelfEvidence(_StrictWriteModel):
    """The writer's own words as the Claim's source; the default is ``because``.

    For an exact-content field the source is the value itself, byte for byte, so
    the text is its own evidence and nothing else needs to be passed.
    """

    kind: Literal["self"] = "self"
    self: str = Field(min_length=1, description="The text that backs the value.")


class CaptureEvidence(_StrictWriteModel):
    """An existing Capture, by digest, cited as evidence for the value."""

    kind: Literal["capture"] = "capture"
    capture: str = Field(pattern=CAPTURE_DIGEST_PATTERN, description="sha256:<64 hex>.")


class FileEvidence(_StrictWriteModel):
    """A span of a workspace file, cited as evidence: ``PATH#ANCHOR``.

    The anchor is text that occurs exactly once in the file. The daemon never
    reads workspace files, so the CLI, the SDK and the MCP adapter read it on the
    writer's side and send what they observed in ``observation``.
    """

    kind: Literal["file"] = "file"
    file: str = Field(
        pattern=FILE_ANCHOR_PATTERN,
        description="PATH#ANCHOR: a catalogued workspace file and text found once in it.",
    )
    observation: WorkingSelectionObservationV1 | None = Field(
        default=None,
        description="Filled by the client that read the file; leave it out.",
    )

    @property
    def path(self) -> str:
        return self.file.partition("#")[0]

    @property
    def anchor(self) -> str:
        return self.file.partition("#")[2]


Evidence = Annotated[SelfEvidence | CaptureEvidence | FileEvidence, Field(discriminator="kind")]


# -- changes --------------------------------------------------------------------


class SlotRef(_StrictWriteModel):
    """One field of one Subject: the slot a single-value Claim fills."""

    subject: SubjectRef
    field: FieldName


class SetChange(_StrictWriteModel):
    """Put ``value`` in a single-value field, replacing the live value.

    The live Claim it replaces is inferred. ``contend`` states a competing Claim
    beside the live one instead of replacing it.
    """

    op: Literal["set"] = "set"
    subject: SubjectRef
    field: FieldName
    value: ClaimValue
    role: WriteRole | None = Field(
        default=None,
        description="Only when the field permits more than one role.",
    )
    evidence: Evidence | None = Field(
        default=None, description="Default: the write's `because` as self evidence."
    )
    contend: bool = False


class AddChange(_StrictWriteModel):
    """Add one more value to a many-valued field, beside the values already there."""

    op: Literal["add"] = "add"
    subject: SubjectRef
    field: FieldName
    value: ClaimValue
    role: WriteRole | None = Field(
        default=None,
        description="Only when the field permits more than one role.",
    )
    evidence: Evidence | None = Field(
        default=None, description="Default: the write's `because` as self evidence."
    )


class RetireChange(_StrictWriteModel):
    """End one live Claim, named by ID or by its Subject and single-value field."""

    op: Literal["retire"] = "retire"
    target: ClaimId | SlotRef
    because: str | None = Field(
        default=None,
        min_length=1,
        description="Why this one ends, when it differs from the write's `because`.",
    )
    reason: WriteRetireReason = Field(
        default="was-rescinded",
        description=(
            "was-rescinded: withdrawn by its author; was-wrong: it was false; "
            "superseded: it stood, but the shape it was stated in no longer does."
        ),
    )


Change = Annotated[SetChange | AddChange | RetireChange, Field(discriminator="op")]


# -- requests -------------------------------------------------------------------


class _WriteRequestBase(_StrictWriteModel):
    because: str = Field(min_length=1, description="Why: the change set's rationale.")
    dry_run: bool = Field(
        default=False,
        description="Run every check up to the commit and write nothing.",
    )
    accept: WriteAccept = Field(
        default="if_allowed",
        description="if_allowed: accept in this call when policy lets you; never: only propose.",
    )
    at: PlaybillAcceptedCoordinate | str | None = Field(
        default=None,
        description=(
            "The coordinate you read at (a git oid or a unique 12+ hex prefix). A "
            "set refuses if its slot changed since; default: the current head."
        ),
    )
    surface: PlaybillReadSurface = "mcp"
    full_coordinate: bool = False

    @field_validator("at")
    @classmethod
    def _at(
        cls, value: PlaybillAcceptedCoordinate | str | None
    ) -> PlaybillAcceptedCoordinate | str | None:
        if isinstance(value, str) and not _GIT_OID.fullmatch(value):
            raise ValueError("at must be an accepted coordinate or a lowercase hex git oid")
        return value


class PlaybillSetRequestV1(_WriteRequestBase):
    tag: Literal["playbill-set-request-v1"] = "playbill-set-request-v1"
    subject: SubjectRef
    field: FieldName
    value: ClaimValue
    role: WriteRole | None = None
    evidence: Evidence | None = None
    contend: bool = False

    def change(self) -> SetChange:
        return SetChange(
            subject=self.subject,
            field=self.field,
            value=self.value,
            role=self.role,
            evidence=self.evidence,
            contend=self.contend,
        )


class PlaybillRetireRequestV1(_WriteRequestBase):
    tag: Literal["playbill-retire-request-v1"] = "playbill-retire-request-v1"
    target: ClaimId | SlotRef
    reason: WriteRetireReason = "was-rescinded"

    def change(self) -> RetireChange:
        return RetireChange(target=self.target, reason=self.reason)


class PlaybillWriteRequestV1(_WriteRequestBase):
    tag: Literal["playbill-write-request-v1"] = "playbill-write-request-v1"
    changes: tuple[Change, ...] = Field(min_length=1, max_length=500)


def as_write_request(
    request: PlaybillSetRequestV1 | PlaybillRetireRequestV1 | PlaybillWriteRequestV1,
) -> PlaybillWriteRequestV1:
    """Every write is a batch; ``set`` and ``retire`` are batches of one."""

    if isinstance(request, PlaybillWriteRequestV1):
        return request
    common = request.model_dump(
        include={"because", "dry_run", "accept", "at", "surface", "full_coordinate"}
    )
    common["at"] = request.at
    return PlaybillWriteRequestV1(changes=(request.change(),), **common)


# -- outcome --------------------------------------------------------------------


class ChangeOutcome(_StrictWriteModel):
    """What one change did, or would do."""

    op: WriteOp
    subject: str | None = Field(default=None, exclude_if=_omit_none)
    field: str | None = Field(default=None, exclude_if=_omit_none)
    predicate: str | None = Field(default=None, exclude_if=_omit_none)
    before: Any = None
    after: Any = None
    claim: str | None = Field(
        default=None,
        exclude_if=_omit_none,
        description="The Claim this change writes, revises or retires.",
    )
    revises: str | None = Field(default=None, exclude_if=_omit_none)
    retired: tuple[str, ...] = Field(
        default=(),
        exclude_if=_omit_empty,
        description="Dependent Claims retired with this one.",
    )
    contenders_created: tuple[str, ...] = Field(
        default=(),
        exclude_if=_omit_empty,
        description="Live Claims this one now contends with in a single-value slot.",
    )


class WriteProposalRef(_StrictWriteModel):
    proposal_id: str
    state: str


class ApprovalNeeded(_StrictWriteModel):
    """Why the write stops short of accepted state, and the exact next call."""

    reason: ApprovalReason
    minimum_approvals: int = Field(ge=0)
    eligible_approvers: tuple[str, ...] = ()
    approve: str | None = Field(
        default=None,
        exclude_if=_omit_none,
        description="The approve call for one of the eligible approvers, on your surface.",
    )
    activate: str = Field(description="The call that accepts it once approved.")


class WriteRefusal(_StrictWriteModel):
    code: str
    message: str
    change: int | None = Field(
        default=None,
        exclude_if=_omit_none,
        description="Index of the change in `changes` that refused.",
    )
    field_path: str | None = Field(default=None, exclude_if=_omit_none)
    candidates: tuple[str, ...] = Field(default=(), exclude_if=_omit_empty)
    repair: str | None = Field(default=None, exclude_if=_omit_none)


class WriteOutcome(_StrictWriteModel):
    """The answer to ``set``, ``retire`` and ``write``: an outcome, not a record."""

    tag: Literal["playbill-write-outcome-v1"] = "playbill-write-outcome-v1"
    status: WriteStatus
    changes: tuple[ChangeOutcome, ...] = ()
    subjects_added: tuple[str, ...] = Field(default=(), exclude_if=_omit_empty)
    proposal: WriteProposalRef | None = Field(default=None, exclude_if=_omit_none)
    coordinate: PlaybillGetCoordinateV1 = Field(
        description=(
            "Accepted: the new generation. Otherwise the head this write was checked "
            "against; pass it back as `at` to pin a later write to it."
        )
    )
    base: PlaybillGetCoordinateV1 | None = Field(
        default=None,
        exclude_if=_omit_none,
        description="Accepted only: the head the write was checked against.",
    )
    accepted_coordinate: PlaybillAcceptedCoordinate | None = Field(
        default=None,
        exclude_if=_omit_none,
        description="The full accepted coordinate, when the request asked for it.",
    )
    approval: ApprovalNeeded | None = Field(default=None, exclude_if=_omit_none)
    refusal: WriteRefusal | None = Field(default=None, exclude_if=_omit_none)
    next: str | None = Field(default=None, exclude_if=_omit_none)

    @property
    def refused(self) -> bool:
        return self.status in ("refused", "would_refuse")


__all__ = [
    "ApprovalNeeded",
    "ApprovalReason",
    "CaptureEvidence",
    "Change",
    "ChangeOutcome",
    "ClaimId",
    "ClaimValue",
    "Evidence",
    "FieldName",
    "FileEvidence",
    "PlaybillRetireRequestV1",
    "PlaybillSetRequestV1",
    "PlaybillWriteRequestV1",
    "RetireChange",
    "SelfEvidence",
    "SetChange",
    "AddChange",
    "SlotRef",
    "SubjectRef",
    "WriteAccept",
    "WriteOp",
    "WriteOutcome",
    "WriteProposalRef",
    "WriteRefusal",
    "WriteRetireReason",
    "WriteRole",
    "WriteStatus",
    "as_write_request",
]
