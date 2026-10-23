"""The write verbs: ``set``, ``retire``, ``rebind`` and ``write``.

These verbs change accepted state, identically on MCP, the CLI, HTTP and the SDK:

- ``set`` puts one value in one field of one Subject. On a single-value field it
  replaces the live value (the Claim it revises is inferred);
- ``retire`` ends one live Claim, named by ID or by its Subject and field, or a
  Subject itself (``kind/id``; its own Claims only with ``with_claims``);
- ``rebind`` re-cites a live Claim whose cited text drifted on the text it
  rests on now: the same statement, a new revision of the same Claim;
- ``write`` applies a batch of ``create``, ``set``, ``add``, ``retire`` and
  ``rebind`` changes as one change set. A ``create`` names a new Subject by its
  kind and key under a label; the write's other changes name it
  ``{"created": label}``, as their subject or as a Subject value, in any order.

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
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypeAlias, cast

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from cruxible_client.contracts import AcceptedCoordinate
from cruxible_client.contracts.authoring.models import (
    WorkingDigestCoordinate,
    WorkingSelectionObservation,
    _canonical_base64,
)
from cruxible_client.contracts.captures import FOREIGN_SOURCE_MAX_BYTES
from cruxible_client.contracts.codes import CurrentCode, code_told_union
from cruxible_client.contracts.doc_sections import SectionPath, section_digest
from cruxible_client.contracts.get_reads import GetCoordinate, ReadSurface
from cruxible_client.contracts.read_values import ShownValue
from cruxible_client.contracts.subject_kinds import SubjectKeyPart, SubjectKeyValue

#: The kind part of a Subject reference: a canonical Subject kind.
SUBJECT_KIND_PATTERN = r"[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})*"
#: What follows ``kind/``: a Subject id, an alias of the kind, or a value of the
#: kind's single key, as written. The daemon resolves it (exact live id, alias,
#: key in its ClaimType's canonical form, exact retired id), so key text keeps
#: its case and spelling: ``security.vulnerability/CVE-2021-44228``.
SUBJECT_REF_TEXT_PATTERN = r"[^\x00-\x1f\x7f]{1,1024}"
SUBJECT_REF_PATTERN = rf"^{SUBJECT_KIND_PATTERN}/{SUBJECT_REF_TEXT_PATTERN}$"
CLAIM_ID_PATTERN = r"^(?:Claim:)?CLM-[0-9a-f]{32}$"
CAPTURE_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
CAPTURE_HANDLE_PATTERN = r"^CAP-[0-9a-f]{12,64}$"
CAPTURE_REF_PATTERN = r"^(?:sha256:[0-9a-f]{64}|CAP-[0-9a-f]{12,64})$"
FILE_ANCHOR_PATTERN = r"^[^#]+#.+$"
CITATION_REF_PATTERN = r"^(?:sha256:)?[0-9a-f]{12,64}$"
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
        description="kind/id (or kind/alias, kind/key; '@' optional).",
    ),
]
ClaimId = Annotated[
    str,
    Field(pattern=CLAIM_ID_PATTERN, description="CLM-… (or Claim:CLM-…)."),
]
FieldName = Annotated[
    str,
    Field(
        min_length=1,
        max_length=512,
        description="A field as orient names it, or its predicate.",
    ),
]
ClaimValue = bool | int | str
"""A scalar value. A Subject-valued field takes the Subject as kind/id (or
``@kind/id``); an exact-content field takes the text itself. Accepted values
hold integers only (canonical JSON has no fractions): a number field counts in
a scaled unit, or takes a decimal as text when its schema is a string."""

ExpectedValue = ClaimValue | tuple[ClaimValue, ...]
"""What a field must hold for a write to go ahead: one value, or every live
value of the field as a list (``[]`` when it must hold none)."""

_EXPECT_DESCRIPTION = "Compare-and-set: the value(s) you read, [] for none; else slot_changed."

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
WriteOp = Literal["create", "set", "add", "retire", "rebind"]
ApprovalReason = Literal[
    "independent_approval_required", "activation_not_permitted", "accept_never"
]


class _StrictWriteModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _omit_none(value: object) -> bool:
    return value is None


class NoneValue(_StrictWriteModel):
    """A none: the field holds nothing; needs a ClaimType inverse and its evidence."""

    # ``{"none": null}``: a one-valued field holds no value. ``{"none": KEY}``:
    # a many-valued field does not hold KEY (the value, a Subject as kind/id,
    # under an ``object`` inverse; the object's key value under ``object_key``).
    none: ClaimValue | None = Field(description="null, or the value (key) not held.")


#: What a set or add writes: a value, or a none.
WriteValue: TypeAlias = ClaimValue | NoneValue


def _omit_empty(value: object) -> bool:
    return value in (None, (), [])


# -- evidence -------------------------------------------------------------------


class SelfEvidence(_StrictWriteModel):
    """Own words; default: because (exact content: the value itself)."""

    kind: Literal["self"] = "self"
    self: str = Field(min_length=1, description="The text that backs the value.")


def capture_handle(digest: str, *, length: int = 12) -> str:
    """A Capture's short handle: ``CAP-`` and the first ``length`` hex of its digest."""

    return "CAP-" + digest.partition(":")[2][:length]


class CaptureEvidence(_StrictWriteModel):
    """Cite a verified Capture."""

    kind: Literal["capture"] = "capture"
    capture: str = Field(
        pattern=CAPTURE_REF_PATTERN,
        description="sha256 digest or unique CAP-<12+ hex>.",
    )


class ContractEvidence(_StrictWriteModel):
    """This contract's newest verified exact-bytes Capture about the Subject."""

    kind: Literal["contract"] = "contract"
    contract: str = Field(
        min_length=1,
        max_length=512,
        description="A name from admitted_contracts.",
    )


class FileEvidence(_StrictWriteModel):
    """File evidence sent as bytes by the client; the daemon never reads workspace files."""

    kind: Literal["file"] = "file"
    file: str = Field(
        pattern=FILE_ANCHOR_PATTERN,
        description="PATH#ANCHOR: a catalogued file and text found once in it.",
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


class SectionEvidence(_StrictWriteModel):
    """Cite a section's own body; the client observes it after this write's edits.

    Authored prose may be used as the writer's own words, with a locating copy.
    ClaimType admission still names fetched contracts; no authored-byte tracking
    or new provenance authority is implied by ``own_words``.
    """

    kind: Literal["section"] = "section"
    file: str = ""
    section: SectionPath = ()
    ref: str | None = Field(default=None, min_length=1, exclude_if=_omit_none)
    own_words: bool | None = None
    observation: WorkingSelectionObservation | None = Field(
        default=None, description="Filled by the client that read the section; leave it out."
    )

    @model_serializer(mode="wrap")
    def _reference_shape(self, handler: Any) -> dict[str, Any]:
        result: dict[str, Any] = handler(self)
        if self.ref is not None:
            result.pop("file", None)
            result.pop("section", None)
        return result

    @model_validator(mode="after")
    def _section_label(self) -> SectionEvidence:
        if self.ref is not None:
            if {"file", "section"} & self.model_fields_set or self.observation is not None:
                raise ValueError("section ref cannot be combined with file, section or observation")
        elif not self.file or "section" not in self.model_fields_set:
            raise ValueError("section evidence needs ref, or explicit file and section")
        if self.observation is not None:
            label = self.observation.selector.section
            if label is None or label.path != self.section:
                raise ValueError("section evidence observation must label the cited heading path")
        return self


Evidence = Annotated[
    SelfEvidence | CaptureEvidence | FileEvidence | ContractEvidence | SectionEvidence,
    Field(discriminator="kind"),
]


# -- document operations --------------------------------------------------------


class AppendSection(_StrictWriteModel):
    """Append a child subtree; a missing file is created with exactly this text."""

    op: Literal["append"] = "append"
    id: str | None = Field(default=None, min_length=1, exclude_if=_omit_none)
    file: str = Field(min_length=1)
    under: SectionPath = ()
    text: str = Field(min_length=1)
    name: str | None = Field(default=None, min_length=1, description="Catalog name for a new file.")


class ReplaceSection(_StrictWriteModel):
    """Replace a whole subtree, or the preamble at section []."""

    op: Literal["replace"] = "replace"
    id: str | None = Field(default=None, min_length=1, exclude_if=_omit_none)
    file: str = Field(min_length=1)
    section: SectionPath
    text: str
    expect: str | None = Field(default=None, pattern=CAPTURE_DIGEST_PATTERN)


DocOp = Annotated[AppendSection | ReplaceSection, Field(discriminator="op")]


class DocEditObservation(_StrictWriteModel):
    """Bounded wire observation of an edit; carries no workspace path or locator.

    ``section`` labels the resulting subtree. ``before_digest`` is absent for a
    new child. Whole-file coordinates support client-side compare-and-set;
    the daemon never opens the file they describe.
    """

    tag: Literal["playbill-doc-edit-observation-v1"] = "playbill-doc-edit-observation-v1"
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,255}$")
    op: Literal["append", "replace"]
    section: SectionPath
    before_digest: str | None = Field(default=None, pattern=CAPTURE_DIGEST_PATTERN)
    after_digest: str = Field(pattern=CAPTURE_DIGEST_PATTERN)
    content_base64: str = Field(max_length=4 * ((FOREIGN_SOURCE_MAX_BYTES + 2) // 3))
    before_coordinate: WorkingDigestCoordinate | None = None
    after_coordinate: WorkingDigestCoordinate
    start_byte: int = Field(default=0, ge=0)
    end_byte: int = Field(default=0, ge=0)
    source_content_base64: str | None = None

    @property
    def content(self) -> bytes:
        return _canonical_base64(self.content_base64, label="document subtree")

    @model_validator(mode="after")
    def _correspondence(self) -> DocEditObservation:
        content = self.content
        if len(content) > FOREIGN_SOURCE_MAX_BYTES:
            raise ValueError("document subtree exceeds the foreign-source byte budget")
        if section_digest(content) != self.after_digest:
            raise ValueError("document subtree digest does not reproduce")
        if len(content) > self.after_coordinate.source_byte_length:
            raise ValueError("document subtree exceeds the whole-file length")
        if self.op == "append" and not self.section:
            raise ValueError("append must label a new section, not the preamble")
        if self.op == "replace" and (self.before_coordinate is None or self.before_digest is None):
            raise ValueError("replace requires the prior file coordinate and subtree digest")
        if self.end_byte < self.start_byte or (
            self.before_coordinate is not None
            and self.end_byte > self.before_coordinate.source_byte_length
        ):
            raise ValueError("document edit window exceeds the prior file")
        if self.source_content_base64 is not None:
            whole = _canonical_base64(self.source_content_base64, label="document preimage")
            if self.before_coordinate is None or (
                section_digest(whole) != self.before_coordinate.source_content_digest
                or len(whole) != self.before_coordinate.source_byte_length
            ):
                raise ValueError("document preimage differs from its coordinate")
        return self


# -- changes --------------------------------------------------------------------

#: A ``create`` change's label: how the write's other changes name its Subject.
NEW_SUBJECT_LABEL_PATTERN = r"^[a-z][a-z0-9_-]{0,63}$"
NewSubjectLabel = Annotated[
    str,
    Field(
        pattern=NEW_SUBJECT_LABEL_PATTERN,
        description="Unique in this write.",
    ),
]


class CreatedSubject(_StrictWriteModel):
    """A create change of this write, by its label, in any order."""

    created: NewSubjectLabel


SubjectTarget: TypeAlias = SubjectRef | CreatedSubject
"""A change's Subject: an accepted one as kind/id, or one this write creates."""


class CreateChange(_StrictWriteModel):
    """Create a Subject by kind and key, or land on its one live holder; core assigns ids."""

    op: Literal["create"] = "create"
    label: NewSubjectLabel
    subject_kind: str = Field(
        pattern=rf"^{SUBJECT_KIND_PATTERN}$", description="The kind, as orient names it."
    )
    key: SubjectKeyValue | tuple[SubjectKeyPart, ...] | None = Field(
        default=None,
        description=('One key value, or parts {"predicate", "value"}; none for a minted kind.'),
    )
    create_new: bool = Field(
        default=False,
        description="Hint kinds only: create another live holder of the key.",
    )
    expect_new: bool = Field(
        default=False,
        description="Refuse subject_exists if the key has a live holder.",
    )


class SlotRef(_StrictWriteModel):
    """A Subject's field; value picks one of several live values."""

    subject: SubjectRef | None = Field(
        default=None,
        description="Default: the write's subject.",
    )
    field: FieldName
    value: ClaimValue | None = None


class SetChange(_StrictWriteModel):
    """Replace a single-value field; contend=true contests it instead."""

    op: Literal["set"] = "set"
    subject: SubjectTarget | None = Field(
        default=None,
        description="Default: the write's subject.",
    )
    field: FieldName
    value: ClaimValue | CreatedSubject | NoneValue
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
    """Add a value to a many-valued field."""

    op: Literal["add"] = "add"
    subject: SubjectTarget | None = Field(
        default=None,
        description="Default: the write's subject.",
    )
    field: FieldName
    value: ClaimValue | CreatedSubject | NoneValue
    role: WriteRole | None = Field(
        default=None,
        description="Only when the field permits more than one role.",
    )
    evidence: Evidence | None = Field(
        default=None, description="Default: the write's `because` as self evidence."
    )
    expect_absent: bool = Field(
        default=False,
        description="Refuse value_already_present if the value is live.",
    )


def _with_claims_target(target: object, with_claims: bool) -> None:
    if with_claims and not (isinstance(target, str) and _SUBJECT_REF.fullmatch(target)):
        raise ValueError("with_claims retires a Subject's Claims, so the target is kind/id")


class RetireChange(_StrictWriteModel):
    """End a live Claim (by ID or slot) or a Subject (kind/id)."""

    op: Literal["retire"] = "retire"
    target: ClaimId | SubjectRef | SlotRef
    because: str | None = Field(
        default=None,
        min_length=1,
        description="Overrides the write's because here.",
    )
    reason: WriteRetireReason = Field(
        default="was-rescinded",
        description="was-rescinded: withdrawn; was-wrong: false; superseded: obsolete form.",
    )
    expect: ExpectedValue | None = Field(default=None, description=_EXPECT_DESCRIPTION)
    with_claims: bool = Field(
        default=False,
        exclude_if=lambda value: not value,
        description="Subject target: also retire its live Claims.",
    )

    @model_validator(mode="after")
    def _subject_claims(self) -> RetireChange:
        _with_claims_target(self.target, self.with_claims)
        return self


#: The most live Claims one ``from_capture`` re-bind moves, as many as one write's changes.
MAX_REBIND_FROM_CAPTURE = 500


class RebindChange(_StrictWriteModel):
    """Re-cite a Claim on its text now, or on a fresh Capture; the value stays."""

    # ``from_capture`` instead of ``target`` re-cites every live Claim citing that
    # Capture on the fresh one (``evidence`` a capture), at most 500. The class
    # docstring is the MCP tool text too, so this note stays out of it.
    op: Literal["rebind"] = "rebind"
    target: ClaimId | None = Field(default=None, exclude_if=_omit_none)
    from_capture: str | None = Field(
        default=None,
        pattern=CAPTURE_REF_PATTERN,
        exclude_if=_omit_none,
        description="Instead of target: every live Claim citing this Capture.",
    )
    citation: str | None = Field(
        default=None,
        pattern=CITATION_REF_PATTERN,
        description="Citation to replace (12+ hex); default: the Claim's one of that file "
        "or that Capture's contract.",
    )
    evidence: Annotated[
        FileEvidence | SectionEvidence | CaptureEvidence, Field(discriminator="kind")
    ]

    @model_validator(mode="after")
    def _one_target(self) -> RebindChange:
        if (self.target is None) == (self.from_capture is None):
            raise ValueError("a rebind names target or from_capture, not both")
        if self.from_capture is not None:
            if not isinstance(self.evidence, CaptureEvidence):
                raise ValueError("a from_capture rebind cites a fresh Capture as its evidence")
            if self.citation is not None:
                raise ValueError(
                    "a from_capture rebind replaces each Claim's citation of that Capture; "
                    "leave citation out"
                )
        return self


Change = Annotated[
    CreateChange | SetChange | AddChange | RetireChange | RebindChange,
    Field(discriminator="op"),
]


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
    value: WriteValue
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
    target: ClaimId | SubjectRef | SlotRef
    reason: WriteRetireReason = "was-rescinded"
    expect: ExpectedValue | None = Field(default=None, description=_EXPECT_DESCRIPTION)
    with_claims: bool = Field(default=False, exclude_if=lambda value: not value)

    @model_validator(mode="after")
    def _subject_claims(self) -> RetireRequest:
        _with_claims_target(self.target, self.with_claims)
        return self

    def change(self) -> RetireChange:
        return RetireChange(
            target=self.target,
            reason=self.reason,
            expect=self.expect,
            with_claims=self.with_claims,
        )


class WriteRequest(_WriteRequestBase):
    tag: Literal["playbill-write-request-v1"] = "playbill-write-request-v1"
    subject: SubjectRef | None = Field(
        default=None,
        description=(
            "The Subject every change that names none is about; a change's own "
            "subject overrides it."
        ),
    )
    changes: tuple[Change, ...] = Field(default=(), max_length=500)
    docs: tuple[DocOp | DocEditObservation, ...] = Field(default=(), max_length=500)

    @model_validator(mode="after")
    def _nonempty(self) -> WriteRequest:
        if not self.changes and not self.docs:
            raise ValueError("write requires at least one change or document operation")
        return self

    @model_serializer(mode="wrap")
    def _preserve_docs_presence(self, handler: Any) -> dict[str, object]:
        payload = cast(dict[str, object], handler(self))
        if "docs" not in self.model_fields_set:
            payload.pop("docs", None)
        return payload


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
    # A create: whether it made the Subject or landed on the live one holding its key.
    resolved: Literal["created", "existing"] | None = Field(default=None, exclude_if=_omit_none)
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


class DocOutcome(_StrictWriteModel):
    """The client's section result; citing-Claim reports are deferred."""

    file: str | None = Field(default=None, min_length=1)
    source_id: str | None = None
    section: SectionPath
    before_digest: str | None = Field(default=None, pattern=CAPTURE_DIGEST_PATTERN)
    after_digest: str = Field(pattern=CAPTURE_DIGEST_PATTERN)
    written: bool = False
    refusal: WriteRefusal | None = None
    next: str | None = None


DOCS_ONLY_HINT = (
    "No Claim cites this text. If it records a belief or decision, add a Claim citing "
    "the section; if it is a draft or a note, prose is right."
)


class WriteOutcome(_StrictWriteModel):
    """The answer to ``set``, ``retire`` and ``write``: an outcome, not a record."""

    tag: Literal["playbill-write-outcome-v1"] = "playbill-write-outcome-v1"
    status: WriteStatus
    changes: tuple[ChangeOutcome, ...] = ()
    docs: tuple[DocOutcome, ...] = Field(default=(), exclude_if=_omit_empty)
    hint: str | None = Field(default=None, exclude_if=_omit_none)
    subjects_added: tuple[str, ...] = Field(
        default=(),
        exclude_if=_omit_empty,
        description=(
            "The Subjects this write creates, by the id core assigned. A minted id (or a "
            "further hint holder's) is assigned when the write is submitted, so a dry run "
            "leaves it out."
        ),
    )
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
    "AppendSection",
    "DocEditObservation",
    "DocOp",
    "DocOutcome",
    "ReplaceSection",
    "SectionEvidence",
    "SectionPath",
    "ApprovalNeeded",
    "ApprovalReason",
    "CaptureEvidence",
    "Change",
    "ChangeOutcome",
    "ContractEvidence",
    "CreateChange",
    "CreatedSubject",
    "NEW_SUBJECT_LABEL_PATTERN",
    "NewSubjectLabel",
    "ClaimId",
    "ClaimValue",
    "Evidence",
    "ExpectedValue",
    "FieldName",
    "FileEvidence",
    "NewerCaptureNotCitableWarning",
    "NoneValue",
    "RetireRequest",
    "SetRequest",
    "WriteRequest",
    "MAX_REBIND_FROM_CAPTURE",
    "RebindChange",
    "RetireChange",
    "SelfEvidence",
    "SetChange",
    "AddChange",
    "SlotRef",
    "SubjectRef",
    "SubjectTarget",
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
    "WriteValue",
    "as_write_request",
    "capture_handle",
]
