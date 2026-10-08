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
from typing import TYPE_CHECKING, Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
)

from cruxible_client.contracts import AcceptedCoordinate
from cruxible_client.contracts.authoring.models import WorkingSelectionObservation
from cruxible_client.contracts.codes import CurrentCode, code_told_union
from cruxible_client.contracts.get_reads import GetCoordinate, ReadSurface
from cruxible_client.contracts.read_values import ShownValue

SUBJECT_REF_PATTERN = r"^[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})*/[a-z][a-z0-9_.-]{0,255}$"
CLAIM_ID_PATTERN = r"^(?:Claim:)?CLM-[0-9a-f]{32}$"
CAPTURE_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
CAPTURE_HANDLE_PATTERN = r"^CAP-[0-9a-f]{12,64}$"
CAPTURE_REF_PATTERN = r"^(?:sha256:[0-9a-f]{64}|CAP-[0-9a-f]{12,64})$"
FILE_ANCHOR_PATTERN = r"^[^#]+#.+$"
_GIT_OID = re.compile(r"^[0-9a-f]{1,64}$")

_SUBJECT_REF = re.compile(SUBJECT_REF_PATTERN)


def subject_reference(value: object) -> object:
    """``@kind/id`` names the same Subject as ``kind/id``; anything else is unchanged.

    Scripts mark a Subject with a leading ``@`` to tell it from a string. No
    Subject kind starts with ``@``, so wherever a Subject is expected the sigil
    is unambiguous: one is dropped when what follows is a Subject reference.
    """

    if isinstance(value, str) and value.startswith("@") and _SUBJECT_REF.fullmatch(value[1:]):
        return value[1:]
    return value


SubjectRef = Annotated[
    str,
    BeforeValidator(subject_reference),
    Field(
        pattern=SUBJECT_REF_PATTERN,
        description=(
            "A Subject as kind/id, for example 'dev.roadmap_item/tidy-cli'; "
            "'@kind/id' names the same Subject."
        ),
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
"""A scalar value. A Subject-valued field takes the Subject as kind/id (or
``@kind/id``); an exact-content field takes the text itself."""

ExpectedValue = ClaimValue | tuple[ClaimValue, ...]
"""What a field must hold for a write to go ahead: one value, or every live
value of the field as a list (``[]`` when it must hold none)."""

_EXPECT_DESCRIPTION = (
    "Compare-and-set: the field's value as you read it (list every live value if "
    "many-valued; [] for none); otherwise cruxible.write.slot_changed shows it."
)

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


def capture_handle(digest: str, *, length: int = 12) -> str:
    """A Capture's short handle: ``CAP-`` and the first ``length`` hex of its digest."""

    return "CAP-" + digest.partition(":")[2][:length]


class CaptureEvidence(_StrictWriteModel):
    """An existing Capture, cited as evidence for the value.

    ``capture`` is its digest, or its handle ``CAP-<12+ hex>``: a digest prefix
    unique among the verified Captures the instance holds, cited or not. The
    handle is resolved to the digest before the write is lowered.
    """

    kind: Literal["capture"] = "capture"
    capture: str = Field(
        pattern=CAPTURE_REF_PATTERN,
        description="sha256:<64 hex>, or the handle CAP-<12+ hex> of a verified Capture.",
    )


class ContractEvidence(_StrictWriteModel):
    """The newest verified exact-bytes Capture of one CaptureContract about the Subject.

    A Capture is about the Subject when an accepted Claim on it cites it or its
    source names the Subject. The outcome names the one used as ``capture``.
    """

    kind: Literal["contract"] = "contract"
    contract: str = Field(
        min_length=1,
        max_length=512,
        description="A CaptureContract by name, as admitted_contracts names it.",
    )


class FileEvidence(_StrictWriteModel):
    """A span of a workspace file, cited as evidence: ``PATH#ANCHOR``.

    The anchor is text occurring exactly once in the file. The writer's side
    (CLI, SDK, MCP adapter) reads it and sends what it observed; the daemon
    never reads workspace files.
    """

    kind: Literal["file"] = "file"
    file: str = Field(
        pattern=FILE_ANCHOR_PATTERN,
        description="PATH#ANCHOR: a catalogued workspace file and text found once in it.",
    )
    observation: WorkingSelectionObservation | None = Field(
        default=None,
        description="Filled by the client that read the file; leave it out.",
    )

    @property
    def path(self) -> str:
        return self.file.partition("#")[0]

    @property
    def anchor(self) -> str:
        return self.file.partition("#")[2]


Evidence = Annotated[
    SelfEvidence | CaptureEvidence | FileEvidence | ContractEvidence,
    Field(discriminator="kind"),
]


# -- changes --------------------------------------------------------------------


class SlotRef(_StrictWriteModel):
    """One field of one Subject: the slot a single-value Claim fills."""

    subject: SubjectRef | None = Field(
        default=None,
        description="The Subject as kind/id; default: the write's own subject.",
    )
    field: FieldName


class SetChange(_StrictWriteModel):
    """Put ``value`` in a single-value field, replacing the live value.

    The live Claim it replaces is inferred. ``contend`` states a competing Claim
    beside the live one instead of replacing it.
    """

    op: Literal["set"] = "set"
    subject: SubjectRef | None = Field(
        default=None,
        description="The Subject as kind/id; default: the write's own subject.",
    )
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
    expect: ExpectedValue | None = Field(default=None, description=_EXPECT_DESCRIPTION)


class AddChange(_StrictWriteModel):
    """Add one more value to a many-valued field, beside the values already there."""

    op: Literal["add"] = "add"
    subject: SubjectRef | None = Field(
        default=None,
        description="The Subject as kind/id; default: the write's own subject.",
    )
    field: FieldName
    value: ClaimValue
    role: WriteRole | None = Field(
        default=None,
        description="Only when the field permits more than one role.",
    )
    evidence: Evidence | None = Field(
        default=None, description="Default: the write's `because` as self evidence."
    )
    expect_absent: bool = Field(
        default=False,
        description=(
            "Refuse cruxible.write.value_already_present when the value is already "
            "live, instead of answering it as already done."
        ),
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
    expect: ExpectedValue | None = Field(default=None, description=_EXPECT_DESCRIPTION)


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
    at: AcceptedCoordinate | str | None = Field(
        default=None,
        description=(
            "The coordinate you read at (a git oid or a unique 12+ hex prefix). A "
            "set refuses if its slot changed since; default: the current head."
        ),
    )
    surface: ReadSurface = "mcp"
    full_coordinate: bool = False

    @field_validator("at")
    @classmethod
    def _at(cls, value: AcceptedCoordinate | str | None) -> AcceptedCoordinate | str | None:
        if isinstance(value, str) and not _GIT_OID.fullmatch(value):
            raise ValueError("at must be an accepted coordinate or a lowercase hex git oid")
        return value


class SetRequest(_WriteRequestBase):
    tag: Literal["playbill-set-request-v1"] = "playbill-set-request-v1"
    subject: SubjectRef
    field: FieldName
    value: ClaimValue
    role: WriteRole | None = None
    evidence: Evidence | None = None
    contend: bool = False
    expect: ExpectedValue | None = Field(default=None, description=_EXPECT_DESCRIPTION)

    def change(self) -> SetChange:
        return SetChange(
            subject=self.subject,
            field=self.field,
            value=self.value,
            role=self.role,
            evidence=self.evidence,
            contend=self.contend,
            expect=self.expect,
        )


class RetireRequest(_WriteRequestBase):
    tag: Literal["playbill-retire-request-v1"] = "playbill-retire-request-v1"
    target: ClaimId | SlotRef
    reason: WriteRetireReason = "was-rescinded"
    expect: ExpectedValue | None = Field(default=None, description=_EXPECT_DESCRIPTION)

    def change(self) -> RetireChange:
        return RetireChange(target=self.target, reason=self.reason, expect=self.expect)


class WriteRequest(_WriteRequestBase):
    tag: Literal["playbill-write-request-v1"] = "playbill-write-request-v1"
    subject: SubjectRef | None = Field(
        default=None,
        description=(
            "The Subject every change that names none is about; a change's own "
            "subject overrides it."
        ),
    )
    changes: tuple[Change, ...] = Field(min_length=1, max_length=500)


def as_write_request(
    request: SetRequest | RetireRequest | WriteRequest,
) -> WriteRequest:
    """Every write is a batch; ``set`` and ``retire`` are batches of one."""

    if isinstance(request, WriteRequest):
        return request
    common = request.model_dump(
        include={"because", "dry_run", "accept", "at", "surface", "full_coordinate"}
    )
    common["at"] = request.at
    return WriteRequest(changes=(request.change(),), **common)


# -- outcome --------------------------------------------------------------------


class ChangeOutcome(_StrictWriteModel):
    """What one change did, or would do."""

    op: WriteOp
    subject: str | None = Field(default=None, exclude_if=_omit_none)
    field: str | None = Field(default=None, exclude_if=_omit_none)
    predicate: str | None = Field(default=None, exclude_if=_omit_none)
    # The slot's value before and after, as a summary shows it: a long string is
    # a TruncatedText preview: before names the read of the value it replaced;
    # after is the value this write sent.
    before: ShownValue = None
    after: ShownValue = None
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
    already_live: bool = Field(
        default=False,
        exclude_if=lambda value: value is False,
        description="The value was already live as `claim`: nothing was submitted for it.",
    )
    verdict: str | None = Field(
        default=None,
        exclude_if=_omit_none,
        description=(
            "The written Claim's verdict: at the accepted coordinate once accepted, "
            "otherwise as the candidate evaluation found it."
        ),
    )
    capture: str | None = Field(
        default=None,
        exclude_if=_omit_none,
        description="The Capture cited as evidence, as its handle CAP-<12 hex>.",
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


class VerdictNotSupportedWarning(_StrictWriteModel):
    """The written Claim's verdict is not ``supported``; the write still lands.

    For example ``uncovered``, because the ClaimType's evidence policy does not
    admit the evidence given (R05). ``repair`` is the write again with admitted
    evidence, and becomes the outcome's ``next``.
    """

    code: Annotated[Literal["cruxible.write.verdict_not_supported"], CurrentCode] = (
        "cruxible.write.verdict_not_supported"
    )
    change: int
    claim: str | None = Field(default=None, exclude_if=_omit_none)
    verdict: str
    message: str
    admitted_contracts: tuple[str, ...] = Field(default=(), exclude_if=_omit_empty)
    used_contract: str | None = Field(default=None, exclude_if=_omit_none)
    repair: str | None = Field(default=None, exclude_if=_omit_none)


class NewerCaptureNotCitableWarning(_StrictWriteModel):
    """Contract evidence cited an older Capture: the newest cannot back a Claim.

    ``capture`` is that newest Capture. It is not committed as exact bytes, so no
    Claim can map a source span onto it; the write cited the change's
    ``capture`` instead.
    """

    code: Annotated[Literal["cruxible.write.newer_capture_not_citable"], CurrentCode] = (
        "cruxible.write.newer_capture_not_citable"
    )
    change: int
    capture: str = Field(
        pattern=CAPTURE_HANDLE_PATTERN,
        description="The newest Capture, which no Claim can cite, as its handle CAP-<12 hex>.",
    )
    message: str
    repair: str | None = Field(default=None, exclude_if=_omit_none)


if TYPE_CHECKING:
    WriteWarning: TypeAlias = VerdictNotSupportedWarning | NewerCaptureNotCitableWarning
else:
    WriteWarning = code_told_union(
        "code",
        (
            (VerdictNotSupportedWarning, "cruxible.write.verdict_not_supported"),
            (NewerCaptureNotCitableWarning, "cruxible.write.newer_capture_not_citable"),
        ),
    )
"""Something the write did that the writer did not ask for, said plainly."""


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
    coordinate: GetCoordinate = Field(
        description=(
            "Accepted: the new generation. Otherwise the head this write was checked "
            "against; pass it back as `at` to pin a later write to it."
        )
    )
    base: GetCoordinate | None = Field(
        default=None,
        exclude_if=_omit_none,
        description="Accepted only: the head the write was checked against.",
    )
    accepted_coordinate: AcceptedCoordinate | None = Field(
        default=None,
        exclude_if=_omit_none,
        description="The full accepted coordinate, when the request asked for it.",
    )
    approval: ApprovalNeeded | None = Field(default=None, exclude_if=_omit_none)
    warnings: tuple[WriteWarning, ...] = Field(default=(), exclude_if=_omit_empty)
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
    "ContractEvidence",
    "ClaimId",
    "ClaimValue",
    "Evidence",
    "ExpectedValue",
    "FieldName",
    "FileEvidence",
    "NewerCaptureNotCitableWarning",
    "RetireRequest",
    "SetRequest",
    "WriteRequest",
    "RetireChange",
    "SelfEvidence",
    "SetChange",
    "AddChange",
    "SlotRef",
    "SubjectRef",
    "subject_reference",
    "VerdictNotSupportedWarning",
    "WriteAccept",
    "WriteOp",
    "WriteOutcome",
    "WriteProposalRef",
    "WriteRefusal",
    "WriteRetireReason",
    "WriteRole",
    "WriteStatus",
    "as_write_request",
    "capture_handle",
]
