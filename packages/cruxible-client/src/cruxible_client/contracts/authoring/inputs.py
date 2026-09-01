"""Decision-only authoring inputs and base-bound lowering onto frozen expert wires."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_client.contracts.approval_policy import ApprovalPolicyV1
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.authoring.models import (
    ApprovalPolicyAuthoringPayloadV1,
    AuthoringArtifactReferenceV1,
    AuthoringCandidateReferenceV1,
    AuthoringClaimStatementV1,
    AuthoringExactContentObjectV1,
    AuthoringExistingClaimDispositionV1,
    AuthoringPayloadV1,
    ChangeSetAuthoringPayloadV1,
    ClaimAuthoringPayloadV1,
    ClaimAuthoringPayloadV3,
    ClaimDependencyDraftsV1,
    ExistingCaptureCitationSourceV1,
    ProcedureAuthoringPayloadV1,
    ProcedureAuthoringPayloadV2,
    ProcedureMandateAuthoringPayloadV1,
    ProcedureRuntimePolicyAuthoringPayloadV1,
    QueryDefinitionAuthoringPayloadV1,
    SelfSourceBodyV1,
    SubjectAuthoringPayloadV1,
    WorkingSelectionObservationV1,
    authoring_member_identity,
)
from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claims import (
    LiteralClaimObject,
    SubjectClaimObject,
)
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.procedure_runtime_policy import ProcedureRuntimePolicyV1
from cruxible_client.contracts.procedures.artifacts import ProcedureOwnedContractV1
from cruxible_client.contracts.procedures.contract_schema import ContractSchema, PropertySchema
from cruxible_client.contracts.procedures.models import ProcedureHardCapsV3
from cruxible_client.contracts.query.definitions import QueryDefinitionV1
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import SubjectShell, subject_path

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
    kind: Literal["exact_content"]
    text: str


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
    claim_id: str | None = None
    dispositions: tuple[ClaimDispositionInput, ...] = ()


class ProcedureInput(_StrictInputModel):
    kind: Literal["procedure"]
    definition: dict[str, object]
    activation_policy: Literal["drain", "abort", "snapshot", "epoch-check"]
    retire: bool = False
    contracts: tuple[CarriedContractInput, ...] = ()

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
    query_definition: QueryDefinitionV1


class ApprovalPolicyInput(_StrictInputModel):
    kind: Literal["approval_policy"]
    approval_policy: ApprovalPolicyV1


class ProcedureRuntimePolicyInput(_StrictInputModel):
    kind: Literal["procedure_runtime_policy"]
    procedure_runtime_policy: ProcedureRuntimePolicyV1


class ProcedureMandateInputV1(_StrictInputModel):
    tag: Literal["playbill-procedure-mandate-input-v1"] = "playbill-procedure-mandate-input-v1"
    kind: Literal["procedure_mandate"]
    name: str
    procedure_name: str
    rung: Literal[2, 3]
    authority_ceiling: ProcedureHardCapsV3
    namespace: tuple[str, ...]
    valid_from: datetime
    expires_at: datetime
    retire: bool = False


AuthoringChangeSetMemberInputV1: TypeAlias = Annotated[
    SubjectInput
    | QueryDefinitionInput
    | ApprovalPolicyInput
    | ProcedureRuntimePolicyInput
    | ProcedureMandateInputV1
    | ProcedureInput,
    Field(discriminator="kind"),
]


class ChangeSetInput(_StrictInputModel):
    kind: Literal["change_set"]
    members: tuple[AuthoringChangeSetMemberInputV1, ...] = Field(min_length=2)


AuthoringInputV1: TypeAlias = Annotated[
    ClaimInput
    | ProcedureInput
    | SubjectInput
    | QueryDefinitionInput
    | ApprovalPolicyInput
    | ProcedureRuntimePolicyInput
    | ProcedureMandateInputV1
    | ChangeSetInput,
    Field(discriminator="kind"),
]


@dataclass(eq=False)
class AuthoringInputError(PlaybillFormatError, ValueError):
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
            "playbill.authoring.input_subject_invalid",
            field_path,
            "Subject must use canonical <subject-kind>/<subject-id> shorthand.",
            "Replace it with a subject shown by playbill subject list.",
        )
    return SemanticAddress.whole_artifact(subject_path(match["kind"], match["id"]))


def _claim_object(
    value: AuthoringObjectInput,
) -> LiteralClaimObject | SubjectClaimObject | AuthoringExactContentObjectV1:
    if isinstance(value, LiteralObjectInput):
        return LiteralClaimObject(value=value.value)
    if isinstance(value, SubjectObjectInput):
        return SubjectClaimObject(
            address=_subject_address(value.subject, field_path="input.object.subject")
        )
    return AuthoringExactContentObjectV1(
        content_base64=base64.b64encode(value.text.encode("utf-8")).decode("ascii")
    )


def _dispositions(
    values: tuple[ClaimDispositionInput, ...],
) -> tuple[AuthoringExistingClaimDispositionV1, ...]:
    return tuple(
        AuthoringExistingClaimDispositionV1(
            claim_id=item.claim_id,
            disposition=item.disposition,
        )
        for item in sorted(values, key=lambda item: item.claim_id.encode("ascii"))
    )


def _claim_payload(value: ClaimInput) -> ClaimAuthoringPayloadV1:
    if isinstance(value.source, WorkingSelectionInput):
        raise AuthoringInputError(
            "playbill.authoring.working_selection_requires_bind",
            "input.source",
            "create and compile cannot observe local working-source bytes.",
            "Run playbill authoring bind with this input and the selected local file.",
        )
    if isinstance(value.source, ExistingCaptureInput):
        if value.citation_role is None:
            raise AuthoringInputError(
                "playbill.authoring.existing_capture_not_admitted",
                "input.citation_role",
                "An existing Capture requires evidence or copy intent.",
                "Set citation_role to evidence or copy.",
            )
        return ClaimAuthoringPayloadV3(
            statement=AuthoringClaimStatementV1(
                subject=_subject_address(value.subject, field_path="input.subject"),
                predicate=value.predicate,
                qualifier=value.qualifier,
                object=_claim_object(value.object),
                role=value.role,
                effective_from=value.effective_from,
                effective_until=value.effective_until,
            ),
            rationale=value.rationale,
            source=ExistingCaptureCitationSourceV1(
                capture_digest=value.source.capture_digest,
            ),
            citation_role=value.citation_role,
            claim_ref=value.claim_id,
            existing_claim_dispositions=_dispositions(value.dispositions),
            dependency_drafts=ClaimDependencyDraftsV1(),
        )
    if value.citation_role is not None:
        raise AuthoringInputError(
            "playbill.authoring.self_source_citation_role_forbidden",
            "input.citation_role",
            "Self-source fixes its copy citation role server-side.",
            "Remove citation_role.",
        )
    return ClaimAuthoringPayloadV1(
        statement=AuthoringClaimStatementV1(
            subject=_subject_address(value.subject, field_path="input.subject"),
            predicate=value.predicate,
            qualifier=value.qualifier,
            object=_claim_object(value.object),
            role=value.role,
            effective_from=value.effective_from,
            effective_until=value.effective_until,
        ),
        rationale=value.rationale,
        source=SelfSourceBodyV1(
            content_base64=base64.b64encode(value.source.body.encode("utf-8")).decode("ascii")
        ),
        claim_ref=value.claim_id,
        existing_claim_dispositions=_dispositions(value.dispositions),
    )


def lower_bound_claim_input(
    value: ClaimInput,
    *,
    observation: WorkingSelectionObservationV1,
) -> ClaimAuthoringPayloadV1:
    """Lower the only client-observed input form after bind constructs its observation."""

    if not isinstance(value.source, WorkingSelectionInput):
        raise AuthoringInputError(
            "playbill.authoring.bind_requires_working_selection",
            "input.source",
            "authoring bind accepts only a working_selection source.",
            "Use create or compile for self_source input.",
        )
    if observation.source_id != value.source.source_id:
        raise AuthoringInputError(
            "playbill.authoring.bind_source_mismatch",
            "input.source.source_id",
            "The observation source differs from the declared logical source.",
            "Bind the file using the declared source_id.",
        )
    if value.citation_role is None:
        raise AuthoringInputError(
            "playbill.authoring.working_selection_citation_role_required",
            "input.citation_role",
            "A working selection requires evidence or copy intent.",
            "Set citation_role to evidence or copy.",
        )
    return ClaimAuthoringPayloadV1(
        statement=AuthoringClaimStatementV1(
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
        claim_ref=value.claim_id,
        existing_claim_dispositions=_dispositions(value.dispositions),
    )


def _artifact_identity(value: str, *, field_path: str) -> ArtifactIdentity:
    kind, separator, name = value.partition(":")
    if not separator:
        raise AuthoringInputError(
            "playbill.authoring.accepted_target_invalid",
            field_path,
            "Accepted references use ArtifactKind:name.",
            "Replace target with an identity returned by discover.",
        )
    try:
        return ArtifactIdentity(kind=kind, name=name)
    except ValueError as exc:
        raise AuthoringInputError(
            "playbill.authoring.accepted_target_invalid",
            field_path,
            "Accepted reference identity is not canonical.",
            "Replace target with an identity returned by discover.",
        ) from exc


def _procedure_references(
    value: object,
    *,
    contracts: dict[str, ProcedureOwnedContractV1],
    field_path: str = "input.definition",
) -> object:
    if isinstance(value, dict):
        if value.get("kind") == "accepted" and set(value) == {"kind", "role", "target"}:
            accepted_reference = AcceptedReferenceInput.model_validate(value)
            return AuthoringArtifactReferenceV1(
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
                    "playbill.authoring.candidate_reference_invalid",
                    field_path,
                    "Candidate references require text role and target fields.",
                    "Use {kind: candidate, role: <role>, target: ArtifactKind:name}.",
                )
            return AuthoringCandidateReferenceV1(
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
                    "playbill.authoring.carried_contract_unresolved",
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
) -> ProcedureAuthoringPayloadV1 | ProcedureAuthoringPayloadV2:
    contracts = tuple(
        sorted(
            (
                ProcedureOwnedContractV1(
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
            "playbill.authoring.carried_contract_duplicate",
            "input.contracts",
            "Carried Contract names must be unique.",
            "Remove or rename the duplicate declaration.",
        )
    definition = cast(
        dict[str, object],
        _procedure_references(value.definition, contracts=by_name),
    )
    if contracts:
        return ProcedureAuthoringPayloadV2(
            definition=definition,
            activation_policy=value.activation_policy,
            owned_contracts=contracts,
            retire=value.retire,
        )
    return ProcedureAuthoringPayloadV1(
        definition=definition,
        activation_policy=value.activation_policy,
        retire=value.retire,
    )


def lower_authoring_input(value: AuthoringInputV1, *, tree: dict[str, bytes]) -> AuthoringPayloadV1:
    """Resolve one input against exactly the supplied accepted tree."""

    del tree
    if isinstance(value, ClaimInput):
        return _claim_payload(value)
    if isinstance(value, ProcedureInput):
        return _procedure_payload(value)
    if isinstance(value, SubjectInput):
        return SubjectAuthoringPayloadV1(subject=value.subject)
    if isinstance(value, QueryDefinitionInput):
        return QueryDefinitionAuthoringPayloadV1(query_definition=value.query_definition)
    if isinstance(value, ApprovalPolicyInput):
        return ApprovalPolicyAuthoringPayloadV1(approval_policy=value.approval_policy)
    if isinstance(value, ProcedureRuntimePolicyInput):
        return ProcedureRuntimePolicyAuthoringPayloadV1(
            procedure_runtime_policy=value.procedure_runtime_policy
        )
    if isinstance(value, ProcedureMandateInputV1):
        return ProcedureMandateAuthoringPayloadV1(
            name=value.name,
            procedure_name=value.procedure_name,
            rung=value.rung,
            authority_ceiling=value.authority_ceiling,
            namespace=value.namespace,
            valid_from=value.valid_from,
            expires_at=value.expires_at,
            retire=value.retire,
        )
    members = tuple(
        _procedure_payload(member)
        if isinstance(member, ProcedureInput)
        else (
            SubjectAuthoringPayloadV1(subject=member.subject)
            if isinstance(member, SubjectInput)
            else (
                QueryDefinitionAuthoringPayloadV1(query_definition=member.query_definition)
                if isinstance(member, QueryDefinitionInput)
                else (
                    ApprovalPolicyAuthoringPayloadV1(approval_policy=member.approval_policy)
                    if isinstance(member, ApprovalPolicyInput)
                    else (
                        ProcedureRuntimePolicyAuthoringPayloadV1(
                            procedure_runtime_policy=member.procedure_runtime_policy
                        )
                        if isinstance(member, ProcedureRuntimePolicyInput)
                        else ProcedureMandateAuthoringPayloadV1(
                            name=member.name,
                            procedure_name=member.procedure_name,
                            rung=member.rung,
                            authority_ceiling=member.authority_ceiling,
                            namespace=member.namespace,
                            valid_from=member.valid_from,
                            expires_at=member.expires_at,
                            retire=member.retire,
                        )
                    )
                )
            )
        )
        for member in value.members
    )
    identities = tuple(authoring_member_identity(member) for member in members)
    if len(set(identities)) != len(identities):
        raise AuthoringInputError(
            "playbill.authoring.change_set_duplicate_identity",
            "input.members",
            "Change-set member semantic identities must be unique.",
            "Remove or rename the duplicate member.",
        )
    return ChangeSetAuthoringPayloadV1(
        members=tuple(
            sorted(
                members,
                key=lambda member: authoring_member_identity(member).encode("utf-8"),
            )
        )
    )


__all__ = [
    "AcceptedReferenceInput",
    "ApprovalPolicyInput",
    "ProcedureRuntimePolicyInput",
    "AuthoringChangeSetMemberInputV1",
    "AuthoringInputError",
    "AuthoringInputV1",
    "AuthoringObjectInput",
    "AuthoringSourceInput",
    "CarriedContractInput",
    "CarriedContractReferenceInput",
    "ChangeSetInput",
    "ClaimDispositionInput",
    "ClaimInput",
    "ExistingCaptureInput",
    "ExactContentObjectInput",
    "LiteralObjectInput",
    "ProcedureInput",
    "ProcedureMandateInputV1",
    "QueryDefinitionInput",
    "SelfSourceInput",
    "SlotReferenceInput",
    "SubjectObjectInput",
    "SubjectInput",
    "WorkingSelectionInput",
    "lower_bound_claim_input",
    "lower_authoring_input",
]
