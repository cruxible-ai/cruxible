"""Decision-only authoring inputs and base-bound lowering onto frozen expert wires."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Literal, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.acquisition_policies import SourceAcquisitionPolicy
from cruxible_client.contracts.approval_policy import ApprovalPolicy
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.authoring.models import (
    ApprovalPolicyAuthoringPayload,
    AuthoringArtifactReference,
    AuthoringCandidateReference,
    AuthoringChangeSetMember,
    AuthoringClaimStatement,
    AuthoringExactContentObject,
    AuthoringExistingClaimDisposition,
    AuthoringPayload,
    ChangeSetAuthoringPayload,
    ClaimAuthoringPayload,
    ClaimAuthoringPayloadV1,
    ClaimDependencyDrafts,
    ClaimRetirementMember,
    ClaimTypeAuthoringPayload,
    ClaimTypeSuccessionDependent,
    ClaimTypeSuccessionMember,
    ExistingCaptureCitationSource,
    LineAuthoringPayload,
    MandateConditionAuthoring,
    MandateScopeAuthoring,
    ProcedureAuthoringPayload,
    ProcedureMandateAuthoringPayload,
    ProcedureRuntimePolicyAuthoringPayload,
    QueryDefinitionAuthoringPayload,
    SelfSourceBody,
    SourceAcquisitionPolicyAuthoringPayload,
    SubjectAuthoringPayload,
    TriggerAuthoringPayload,
    WorkingSelectionObservation,
    authoring_member_identity,
)
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claim_types import ClaimType
from cruxible_client.contracts.claims import (
    ClaimRetireDependent,
    ClaimRetirementReason,
    LiteralClaimObject,
    SubjectClaimObject,
)
from cruxible_client.contracts.errors import FormatError
from cruxible_client.contracts.procedure_runtime_policy import ProcedureRuntimePolicy
from cruxible_client.contracts.procedures.artifacts import ProcedureOwnedContract
from cruxible_client.contracts.procedures.contract_schema import ContractSchema, PropertySchema
from cruxible_client.contracts.procedures.models import ProcedureHardCaps
from cruxible_client.contracts.proposal_models import (
    CHANGE_SET_RATIONALE_MAX_LENGTH,
    validate_change_set_rationale,
)
from cruxible_client.contracts.query.definitions import QueryDefinition, QueryDefinitionSpec
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import SubjectShell, subject_path
from cruxible_client.contracts.triggers import InternalActionName, TriggerSchedule

if TYPE_CHECKING:
    from cruxible_client.contracts.records import RecordConstructor

_SUBJECT_SHORTHAND_RE = re.compile(
    r"^(?P<kind>[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})*)/"
    r"(?P<id>[a-z][a-z0-9_.-]{0,255})$"
)


class _StrictInputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LiteralObjectInput(_StrictInputModel):
    kind: Literal["literal"]
    value: object


class SubjectObjectInput(_StrictInputModel):
    kind: Literal["subject"]
    subject: str


class ExactContentObjectInput(_StrictInputModel):
    """The exact bytes a Claim's object IS, spelled as text or as base64.

    `text` is the ordinary spelling and stays the ordinary spelling: rulings and
    method laws are prose, and prose typed into JSON should not have to be
    encoded first. `content_base64` is for the bytes text cannot carry -- a body
    that is not valid UTF-8, or one whose exact bytes matter more than its
    reading -- and is the same spelling the SDK's own `ExactContent` lowers to,
    so the two doors put identical bytes in the ledger.

    Exactly one of them, because two spellings of one body are two bodies the
    moment they disagree, and the digest is over the bytes.
    """

    kind: Literal["exact_content"]
    text: str | None = None
    content_base64: str | None = None

    @model_validator(mode="after")
    def _one_spelling(self) -> "ExactContentObjectInput":
        if (self.text is None) == (self.content_base64 is None):
            raise ValueError(
                "an exact_content object carries exactly one of text or content_base64"
            )
        return self


AuthoringObjectInput: TypeAlias = Annotated[
    LiteralObjectInput | SubjectObjectInput | ExactContentObjectInput,
    Field(discriminator="kind"),
]


class SelfSourceInput(_StrictInputModel):
    kind: Literal["self_source"]
    body: str


class WorkingSelectionInput(_StrictInputModel):
    kind: Literal["working_selection"]
    source_id: str


class ExistingCaptureInput(_StrictInputModel):
    kind: Literal["existing_capture"]
    capture_digest: str


AuthoringSourceInput: TypeAlias = Annotated[
    SelfSourceInput | WorkingSelectionInput | ExistingCaptureInput,
    Field(discriminator="kind"),
]


class AcceptedReferenceInput(_StrictInputModel):
    kind: Literal["accepted"]
    role: str
    target: str


class SlotReferenceInput(_StrictInputModel):
    kind: Literal["slot"]
    slot_name: str


class CarriedContractReferenceInput(_StrictInputModel):
    kind: Literal["carried_contract"]
    name: str
    role: str


class CarriedContractInput(_StrictInputModel):
    name: str
    description: str | None = None
    fields: dict[str, PropertySchema]
    allow_extra: bool = False

    @property
    def value(self) -> RecordConstructor:
        """Construct a value under this carried schema without changing its wire."""
        from cruxible_client.contracts.procedures.contract_schema import ContractSchema
        from cruxible_client.contracts.records import RecordConstructor

        return RecordConstructor(ContractSchema(fields=self.fields, allow_extra=self.allow_extra))


class ClaimDispositionInput(_StrictInputModel):
    claim_id: str
    disposition: Literal["not_tested", "support", "contradict", "unsure"]


class ClaimInput(_StrictInputModel):
    kind: Literal["claim"]
    subject: str
    predicate: str
    qualifier: str | None = None
    object: AuthoringObjectInput
    role: Literal["normative", "observation", "environment_binding", "derivation"]
    effective_from: datetime | None = None
    effective_until: datetime | None = None
    rationale: str
    source: AuthoringSourceInput
    citation_role: Literal["evidence", "copy"] | None = None
    revises: str | None = Field(
        default=None,
        description="Claim ID this Claim revises; omit to state a new Claim.",
    )
    dispositions: tuple[ClaimDispositionInput, ...] = ()


class ProcedureInput(_StrictInputModel):
    kind: Literal["procedure"]
    definition: dict[str, object]
    activation_policy: Literal["drain", "abort", "snapshot", "epoch-check"]
    retire: bool = False
    contracts: tuple[CarriedContractInput, ...] = ()
    #: Semantic name of the SourceAcquisitionPolicy this Procedure's envelope
    #: pins. Lowering resolves it against the accepted tree (or this change
    #: set's own candidates) and declares the exact pin.
    acquisition_policy: str | None = None

    @field_validator("definition", mode="before")
    @classmethod
    def _definition(cls, value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            raise ValueError("Procedure input definition must be an object")
        return cast(dict[str, object], value)


class SubjectInput(_StrictInputModel):
    kind: Literal["subject"]
    subject: SubjectShell


class QueryDefinitionInput(_StrictInputModel):
    kind: Literal["query_definition"]
    query_definition: QueryDefinitionSpec | QueryDefinition


class ApprovalPolicyInput(_StrictInputModel):
    kind: Literal["approval_policy"]
    approval_policy: ApprovalPolicy


class ProcedureRuntimePolicyInput(_StrictInputModel):
    kind: Literal["procedure_runtime_policy"]
    procedure_runtime_policy: ProcedureRuntimePolicy


class ClaimTypeInput(_StrictInputModel):
    kind: Literal["claim_type"]
    claim_type: ClaimType


class ClaimTypeSuccessionInput(_StrictInputModel):
    """Succeed one accepted ClaimType, and disposition its closure, in this set."""

    kind: Literal["claim_type_succession"]
    successor: ClaimType
    dependents: tuple[ClaimTypeSuccessionDependent, ...] = ()
    carry_all: bool = Field(
        default=False,
        description=(
            "Carry every closure member dependents does not name to the successor, computed "
            "by the daemon (retired Claims included); dependents then names only exceptions."
        ),
    )


class ClaimRetirementInput(_StrictInputModel):
    kind: Literal["claim_retirement"]
    retires: str = Field(description="Claim ID this member retires, parallel to revises.")
    reason: ClaimRetirementReason
    effective_until: datetime | None = None
    dependents: tuple[ClaimRetireDependent, ...] = ()


class ProcedureMandateInput(_StrictInputModel):
    """A propose grant, or a settle grant with its Claim scope and condition query.

    ``grants`` is the verb the mandate delegates. A settle grant names the
    ClaimTypes (by predicate) and change kinds it covers and the accepted query
    that decides each target; lowering pins every exact digest.
    """

    tag: Literal["playbill-procedure-mandate-input-v1"] = "playbill-procedure-mandate-input-v1"
    kind: Literal["procedure_mandate"]
    name: str
    procedure_name: str
    grants: Literal["propose", "settle"]
    resource_ceiling: ProcedureHardCaps
    namespace: tuple[str, ...]
    valid_from: datetime
    expires_at: datetime
    scope: tuple[MandateScopeAuthoring, ...] = ()
    subject_scope: tuple[SemanticAddress, ...] | None = None
    condition: MandateConditionAuthoring | None = None
    suspended: bool = False
    retire: bool = False


class AcquisitionPolicyInput(_StrictInputModel):
    """One SourceAcquisitionPolicy: how a Line's Source inputs may be acquired."""

    kind: Literal["acquisition_policy"]
    acquisition_policy: SourceAcquisitionPolicy


class LineInput(_StrictInputModel):
    """One Line: a stable instantiation of an accepted or same-set Procedure.

    Lowering resolves the named Procedure and acquisition policy into exact
    pins. A Line runs when run explicitly, or when a Trigger aimed at it fires.
    A Line that proposes or settles also needs a live ProcedureMandate covering
    its Procedure before it can run; an observe-only Line needs none.
    """

    kind: Literal["line"]
    name: str
    procedure_name: str
    acquisition_policy_name: str | None = Field(
        default=None,
        description=(
            "SourceAcquisitionPolicy name; required only when the Procedure has Source nodes."
        ),
    )
    max_authority: Literal["observe", "propose", "settle"] | None = Field(
        default=None,
        description="Caps this Line below its Procedure's own capability; omit to inherit it.",
    )
    trigger_input: str | None = None
    parameters: dict[str, object] = Field(
        default_factory=dict,
        description=(
            "The Procedure's input record, checked against its input contract at authoring."
        ),
    )
    budgets: dict[str, int] | None = Field(
        default=None,
        description="Per-run budgets; omit to use the Procedure's hard caps.",
    )
    occurrence_epoch: int = Field(default=1, ge=1)
    retire: bool = False


class TriggerInput(_StrictInputModel):
    """One schedule aimed at an accepted or same-set Line, or a registered action.

    Actions admit cadence, cron and generation_accepted. Every target's declared
    input must be supplied by its schedule. Change or retire through a successor.
    """

    kind: Literal["trigger"]
    name: str
    schedule: TriggerSchedule
    line_name: str | None = Field(
        default=None, description="The Line this Trigger runs; omit when naming an action."
    )
    action: InternalActionName | None = Field(
        default=None, description="The internal action this Trigger fires; omit for a Line."
    )
    retire: bool = False


AuthoringChangeSetMemberInput: TypeAlias = Annotated[
    ClaimInput
    | ClaimTypeInput
    | ClaimTypeSuccessionInput
    | ClaimRetirementInput
    | SubjectInput
    | QueryDefinitionInput
    | ApprovalPolicyInput
    | ProcedureRuntimePolicyInput
    | ProcedureMandateInput
    | AcquisitionPolicyInput
    | LineInput
    | TriggerInput
    | ProcedureInput,
    Field(discriminator="kind"),
]


class ChangeSetInput(_StrictInputModel):
    kind: Literal["change_set"]
    members: tuple[AuthoringChangeSetMemberInput, ...] = Field(min_length=1)
    # The same sentence `cx.changes(rationale=...)` carries, on the surface a
    # CLI file and an MCP dict use. Leaving it to the SDK would have made "say
    # why you proposed this" an SDK-only capability, which is exactly the kind
    # of split the three-surface parity law exists to prevent.
    rationale: str | None = Field(default=None, max_length=CHANGE_SET_RATIONALE_MAX_LENGTH)

    @field_validator("rationale")
    @classmethod
    def _rationale(cls, value: str | None) -> str | None:
        return validate_change_set_rationale(value)


AuthoringInput: TypeAlias = Annotated[
    ClaimInput
    | ProcedureInput
    | SubjectInput
    | QueryDefinitionInput
    | ApprovalPolicyInput
    | ProcedureRuntimePolicyInput
    | ProcedureMandateInput
    | AcquisitionPolicyInput
    | LineInput
    | TriggerInput
    | ChangeSetInput,
    Field(discriminator="kind"),
]


@dataclass(eq=False)
class AuthoringInputError(FormatError, ValueError):
    code: str
    field_path: str
    message: str
    repair: str

    def __post_init__(self) -> None:
        super().__init__(str(self))

    def __str__(self) -> str:
        return f"{self.code} at {self.field_path}: {self.message} Repair: {self.repair}"

    @property
    def error_code(self) -> str:
        return self.code


def _subject_address(shorthand: str, *, field_path: str) -> SemanticAddress:
    match = _SUBJECT_SHORTHAND_RE.fullmatch(shorthand)
    if match is None:
        raise AuthoringInputError(
            "cruxible.authoring.input_subject_invalid",
            field_path,
            "Subject must use canonical <subject-kind>/<subject-id> shorthand.",
            "Replace it with a subject shown by cruxible query KIND.",
        )
    return SemanticAddress.whole_artifact(subject_path(match["kind"], match["id"]))


def _claim_object(
    value: AuthoringObjectInput,
) -> LiteralClaimObject | SubjectClaimObject | AuthoringExactContentObject:
    if isinstance(value, LiteralObjectInput):
        return LiteralClaimObject(value=value.value)
    if isinstance(value, SubjectObjectInput):
        return SubjectClaimObject(
            address=_subject_address(value.subject, field_path="input.object.subject")
        )
    if value.content_base64 is not None:
        return AuthoringExactContentObject(content_base64=value.content_base64)
    assert value.text is not None
    return AuthoringExactContentObject(
        content_base64=base64.b64encode(value.text.encode("utf-8")).decode("ascii")
    )


def _dispositions(
    values: tuple[ClaimDispositionInput, ...],
) -> tuple[AuthoringExistingClaimDisposition, ...]:
    return tuple(
        AuthoringExistingClaimDisposition(
            claim_id=item.claim_id,
            disposition=item.disposition,
        )
        for item in sorted(values, key=lambda item: item.claim_id.encode("ascii"))
    )


def _claim_payload(value: ClaimInput) -> ClaimAuthoringPayloadV1:
    if isinstance(value.source, WorkingSelectionInput):
        raise AuthoringInputError(
            "cruxible.authoring.working_selection_requires_bind",
            "input.source",
            "compile and submit cannot observe local working-source bytes.",
            "Run cruxible authoring bind with this input and the selected local file.",
        )
    if isinstance(value.source, ExistingCaptureInput):
        if value.citation_role is None:
            raise AuthoringInputError(
                "cruxible.authoring.existing_capture_not_admitted",
                "input.citation_role",
                "An existing Capture requires evidence or copy intent.",
                "Set citation_role to evidence or copy.",
            )
        return ClaimAuthoringPayload(
            statement=AuthoringClaimStatement(
                subject=_subject_address(value.subject, field_path="input.subject"),
                predicate=value.predicate,
                qualifier=value.qualifier,
                object=_claim_object(value.object),
                role=value.role,
                effective_from=value.effective_from,
                effective_until=value.effective_until,
            ),
            rationale=value.rationale,
            source=ExistingCaptureCitationSource(
                capture_digest=value.source.capture_digest,
            ),
            citation_role=value.citation_role,
            revises=value.revises,
            existing_claim_dispositions=_dispositions(value.dispositions),
            dependency_drafts=ClaimDependencyDrafts(),
        )
    if value.citation_role is not None:
        raise AuthoringInputError(
            "cruxible.authoring.self_source_citation_role_forbidden",
            "input.citation_role",
            "Self-source fixes its copy citation role server-side.",
            "Remove citation_role.",
        )
    return ClaimAuthoringPayloadV1(
        statement=AuthoringClaimStatement(
            subject=_subject_address(value.subject, field_path="input.subject"),
            predicate=value.predicate,
            qualifier=value.qualifier,
            object=_claim_object(value.object),
            role=value.role,
            effective_from=value.effective_from,
            effective_until=value.effective_until,
        ),
        rationale=value.rationale,
        source=SelfSourceBody(
            content_base64=base64.b64encode(value.source.body.encode("utf-8")).decode("ascii")
        ),
        revises=value.revises,
        existing_claim_dispositions=_dispositions(value.dispositions),
    )


def lower_bound_claim_input(
    value: ClaimInput,
    *,
    observation: WorkingSelectionObservation,
) -> ClaimAuthoringPayloadV1:
    """Lower the only client-observed input form after bind constructs its observation."""

    if not isinstance(value.source, WorkingSelectionInput):
        raise AuthoringInputError(
            "cruxible.authoring.bind_requires_working_selection",
            "input.source",
            "authoring bind accepts only a working_selection source.",
            "Use create or compile for self_source input.",
        )
    if observation.source_id != value.source.source_id:
        raise AuthoringInputError(
            "cruxible.authoring.bind_source_mismatch",
            "input.source.source_id",
            "The observation source differs from the declared logical source.",
            "Bind the file using the declared source_id.",
        )
    if value.citation_role is None:
        raise AuthoringInputError(
            "cruxible.authoring.working_selection_citation_role_required",
            "input.citation_role",
            "A working selection requires evidence or copy intent.",
            "Set citation_role to evidence or copy.",
        )
    return ClaimAuthoringPayloadV1(
        statement=AuthoringClaimStatement(
            subject=_subject_address(value.subject, field_path="input.subject"),
            predicate=value.predicate,
            qualifier=value.qualifier,
            object=_claim_object(value.object),
            role=value.role,
            effective_from=value.effective_from,
            effective_until=value.effective_until,
        ),
        rationale=value.rationale,
        source=observation,
        citation_role=value.citation_role,
        revises=value.revises,
        existing_claim_dispositions=_dispositions(value.dispositions),
    )


def _artifact_identity(value: str, *, field_path: str) -> ArtifactIdentity:
    kind, separator, name = value.partition(":")
    if not separator:
        raise AuthoringInputError(
            "cruxible.authoring.accepted_target_invalid",
            field_path,
            "Accepted references use ArtifactKind:name.",
            "Replace target with an identity returned by discover.",
        )
    try:
        return ArtifactIdentity(kind=kind, name=name)
    except ValueError as exc:
        raise AuthoringInputError(
            "cruxible.authoring.accepted_target_invalid",
            field_path,
            "Accepted reference identity is not canonical.",
            "Replace target with an identity returned by discover.",
        ) from exc


def _procedure_references(
    value: object,
    *,
    contracts: dict[str, ProcedureOwnedContract],
    field_path: str = "input.definition",
) -> object:
    if isinstance(value, dict):
        if value.get("kind") == "accepted" and set(value) == {"kind", "role", "target"}:
            accepted_reference = AcceptedReferenceInput.model_validate(value)
            return AuthoringArtifactReference(
                role=accepted_reference.role,
                target=_artifact_identity(
                    accepted_reference.target, field_path=f"{field_path}.target"
                ),
            ).model_dump(mode="json")
        if value.get("kind") == "candidate" and set(value) == {"kind", "role", "target"}:
            role = value["role"]
            target = value["target"]
            if not isinstance(role, str) or not isinstance(target, str):
                raise AuthoringInputError(
                    "cruxible.authoring.candidate_reference_invalid",
                    field_path,
                    "Candidate references require text role and target fields.",
                    "Use {kind: candidate, role: <role>, target: ArtifactKind:name}.",
                )
            return AuthoringCandidateReference(
                role=role,
                target=_artifact_identity(target, field_path=f"{field_path}.target"),
            ).model_dump(mode="json")
        if value.get("kind") == "slot" and set(value) == {"kind", "slot_name"}:
            slot_reference = SlotReferenceInput.model_validate(value)
            return {
                "tag": "playbill-procedure-pin-slot-ref-v1",
                "slot_name": slot_reference.slot_name,
            }
        if value.get("kind") == "carried_contract" and set(value) == {
            "kind",
            "name",
            "role",
        }:
            reference = CarriedContractReferenceInput.model_validate(value)
            contract = contracts.get(reference.name)
            if contract is None:
                raise AuthoringInputError(
                    "cruxible.authoring.carried_contract_unresolved",
                    f"{field_path}.name",
                    "The carried Contract reference has no matching declaration.",
                    "Declare that name in input.contracts or repair the reference.",
                )
            return reference.model_dump(mode="json")
        return {
            key: _procedure_references(
                member,
                contracts=contracts,
                field_path=f"{field_path}.{key}",
            )
            for key, member in value.items()
        }
    if isinstance(value, list | tuple):
        return [
            _procedure_references(
                member,
                contracts=contracts,
                field_path=f"{field_path}[{index}]",
            )
            for index, member in enumerate(value)
        ]
    return value


def _procedure_payload(
    value: ProcedureInput,
) -> ProcedureAuthoringPayload:
    contracts = tuple(
        sorted(
            (
                ProcedureOwnedContract(
                    identity=ArtifactIdentity(kind="Contract", name=contract.name),
                    schema=ContractSchema(
                        description=contract.description,
                        fields=contract.fields,
                        allow_extra=contract.allow_extra,
                    ),
                )
                for contract in value.contracts
            ),
            key=lambda contract: canonical_bytes(contract.model_dump(mode="json", by_alias=True)),
        )
    )
    by_name = {contract.identity.name: contract for contract in contracts}
    if len(by_name) != len(contracts):
        raise AuthoringInputError(
            "cruxible.authoring.carried_contract_duplicate",
            "input.contracts",
            "Carried Contract names must be unique.",
            "Remove or rename the duplicate declaration.",
        )
    definition = cast(
        dict[str, object],
        _procedure_references(value.definition, contracts=by_name),
    )
    return ProcedureAuthoringPayload(
        definition=definition,
        activation_policy=value.activation_policy,
        owned_contracts=contracts,
        acquisition_policy=value.acquisition_policy,
        retire=value.retire,
    )


def _mandate_payload(value: ProcedureMandateInput) -> ProcedureMandateAuthoringPayload:
    return ProcedureMandateAuthoringPayload.model_validate(
        value.model_dump(mode="python", exclude={"tag", "kind"})
    )


def _line_payload(value: LineInput) -> LineAuthoringPayload:
    return LineAuthoringPayload(
        name=value.name,
        procedure_name=value.procedure_name,
        acquisition_policy_name=value.acquisition_policy_name,
        max_authority=value.max_authority,
        trigger_input=value.trigger_input,
        parameters=value.parameters,
        budgets=value.budgets,
        occurrence_epoch=value.occurrence_epoch,
        retire=value.retire,
    )


def _trigger_payload(value: TriggerInput) -> TriggerAuthoringPayload:
    return TriggerAuthoringPayload.model_validate(value.model_dump(mode="python", exclude={"kind"}))


def _change_set_member(member: AuthoringChangeSetMemberInput) -> AuthoringChangeSetMember:
    if isinstance(member, ClaimInput):
        return _claim_payload(member)
    if isinstance(member, ClaimTypeInput):
        return ClaimTypeAuthoringPayload(claim_type=member.claim_type)
    if isinstance(member, ClaimTypeSuccessionInput):
        return ClaimTypeSuccessionMember(
            successor=member.successor,
            dependents=member.dependents,
            carry_all=member.carry_all,
        )
    if isinstance(member, ClaimRetirementInput):
        return ClaimRetirementMember(
            retires=member.retires,
            reason=member.reason,
            effective_until=member.effective_until,
            dependents=member.dependents,
        )
    if isinstance(member, ProcedureInput):
        return _procedure_payload(member)
    if isinstance(member, SubjectInput):
        return SubjectAuthoringPayload(subject=member.subject)
    if isinstance(member, QueryDefinitionInput):
        return QueryDefinitionAuthoringPayload(query_definition=member.query_definition)
    if isinstance(member, ApprovalPolicyInput):
        return ApprovalPolicyAuthoringPayload(approval_policy=member.approval_policy)
    if isinstance(member, ProcedureRuntimePolicyInput):
        return ProcedureRuntimePolicyAuthoringPayload(
            procedure_runtime_policy=member.procedure_runtime_policy
        )
    if isinstance(member, AcquisitionPolicyInput):
        return SourceAcquisitionPolicyAuthoringPayload(acquisition_policy=member.acquisition_policy)
    if isinstance(member, LineInput):
        return _line_payload(member)
    if isinstance(member, TriggerInput):
        return _trigger_payload(member)
    return _mandate_payload(member)


def lower_authoring_input(value: AuthoringInput) -> AuthoringPayload:
    """Lower typed input; accepted-state references are checked during preflight."""
    if isinstance(value, ClaimInput):
        return _claim_payload(value)
    if isinstance(value, ProcedureInput):
        return _procedure_payload(value)
    if isinstance(value, SubjectInput):
        return SubjectAuthoringPayload(subject=value.subject)
    if isinstance(value, QueryDefinitionInput):
        return QueryDefinitionAuthoringPayload(query_definition=value.query_definition)
    if isinstance(value, ApprovalPolicyInput):
        return ApprovalPolicyAuthoringPayload(approval_policy=value.approval_policy)
    if isinstance(value, ProcedureRuntimePolicyInput):
        return ProcedureRuntimePolicyAuthoringPayload(
            procedure_runtime_policy=value.procedure_runtime_policy
        )
    if isinstance(value, ProcedureMandateInput):
        return _mandate_payload(value)
    if isinstance(value, AcquisitionPolicyInput):
        return SourceAcquisitionPolicyAuthoringPayload(acquisition_policy=value.acquisition_policy)
    if isinstance(value, LineInput):
        return _line_payload(value)
    if isinstance(value, TriggerInput):
        return _trigger_payload(value)
    members = tuple(_change_set_member(member) for member in value.members)
    identities = tuple(authoring_member_identity(member) for member in members)
    if len(set(identities)) != len(identities):
        raise AuthoringInputError(
            "cruxible.authoring.change_set_duplicate_identity",
            "input.members",
            "Change-set member semantic identities must be unique.",
            "Remove or rename the duplicate member.",
        )
    return ChangeSetAuthoringPayload(
        members=tuple(
            sorted(
                members,
                key=lambda member: authoring_member_identity(member).encode("utf-8"),
            )
        ),
        rationale=value.rationale,
    )


__all__ = [
    "AcceptedReferenceInput",
    "AcquisitionPolicyInput",
    "ApprovalPolicyInput",
    "ProcedureRuntimePolicyInput",
    "AuthoringChangeSetMemberInput",
    "AuthoringInputError",
    "AuthoringInput",
    "AuthoringObjectInput",
    "AuthoringSourceInput",
    "CarriedContractInput",
    "CarriedContractReferenceInput",
    "ChangeSetInput",
    "ClaimDispositionInput",
    "ClaimRetirementInput",
    "ClaimTypeInput",
    "ClaimTypeSuccessionInput",
    "ClaimInput",
    "ExistingCaptureInput",
    "ExactContentObjectInput",
    "LineInput",
    "TriggerInput",
    "LiteralObjectInput",
    "ProcedureInput",
    "ProcedureMandateInput",
    "QueryDefinitionInput",
    "SelfSourceInput",
    "SlotReferenceInput",
    "SubjectObjectInput",
    "SubjectInput",
    "WorkingSelectionInput",
    "lower_bound_claim_input",
    "lower_authoring_input",
]
