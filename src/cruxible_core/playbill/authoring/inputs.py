"""Decision-only authoring inputs and base-bound lowering onto frozen expert wires."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_core.playbill.artifacts import ArtifactAuthority, ArtifactIdentity
from cruxible_core.playbill.authoring.models import (
    AuthoringArtifactReferenceV1,
    AuthoringClaimStatementV1,
    AuthoringExactContentObjectV1,
    AuthoringExistingClaimDispositionV1,
    AuthoringPayloadV1,
    ClaimAuthoringPayloadV1,
    ProcedureAuthoringPayloadV1,
    ProcedureAuthoringPayloadV2,
    SelfSourceBodyV1,
    WorkingSelectionObservationV1,
)
from cruxible_core.playbill.canonical import canonical_bytes
from cruxible_core.playbill.claims import (
    LiteralClaimObject,
    SubjectClaimObject,
    claim_path,
    claim_statement_digest,
    parse_claim,
)
from cruxible_core.playbill.knowledge_briefs import (
    KNOWLEDGE_BRIEF_PREDICATE,
    KnowledgeBriefClaimExpectationV1,
    KnowledgeBriefClaimRefV1,
    KnowledgeBriefQueryRefV1,
    KnowledgeBriefValueV1,
)
from cruxible_core.playbill.procedures.artifacts import ProcedureOwnedContractV1
from cruxible_core.playbill.procedures.contract_schema import ContractSchema, PropertySchema
from cruxible_core.playbill.query.definitions import (
    parse_query_definition,
    query_definition_digest,
    query_definition_path,
)
from cruxible_core.playbill.semantic import SemanticAddress
from cruxible_core.playbill.subjects import subject_path

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


AuthoringSourceInput: TypeAlias = Annotated[
    SelfSourceInput | WorkingSelectionInput,
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


class BriefExpectationInput(_StrictInputModel):
    resolution: Literal["accepted"] = "accepted"
    subject: str | None = None
    claim_type: str | None = None


class BriefClaimRefInput(_StrictInputModel):
    claim_id: str
    expect: BriefExpectationInput | None = None
    statement_digest: str | None = None


class BriefQueryRefInput(_StrictInputModel):
    query_id: str
    parameters: dict[str, object]
    render_field: str
    definition_digest: str | None = None


class BriefInput(_StrictInputModel):
    kind: Literal["brief"]
    subject: str
    purpose: str
    brief_kind: Literal["brief", "guidance", "faq"]
    prose: str
    audience: Literal["agent", "human", "both"] | None = None
    claim_refs: tuple[BriefClaimRefInput, ...] = ()
    query_refs: tuple[BriefQueryRefInput, ...] = ()
    rationale: str
    claim_id: str | None = None
    dispositions: tuple[ClaimDispositionInput, ...] = ()


class ProcedureInput(_StrictInputModel):
    kind: Literal["procedure"]
    definition: dict[str, object]
    authority: ArtifactAuthority
    activation_policy: Literal["drain", "abort", "snapshot", "epoch-check"]
    retire: bool = False
    contracts: tuple[CarriedContractInput, ...] = ()

    @field_validator("definition", mode="before")
    @classmethod
    def _definition(cls, value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            raise ValueError("Procedure input definition must be an object")
        return cast(dict[str, object], value)


AuthoringInputV1: TypeAlias = Annotated[
    ClaimInput | BriefInput | ProcedureInput,
    Field(discriminator="kind"),
]


@dataclass(frozen=True)
class AuthoringInputError(ValueError):
    code: str
    field_path: str
    message: str
    repair: str

    def __str__(self) -> str:
        return f"{self.code} at {self.field_path}: {self.message} Repair: {self.repair}"


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


def _brief_expectation(value: BriefExpectationInput | None) -> KnowledgeBriefClaimExpectationV1:
    if value is None:
        return KnowledgeBriefClaimExpectationV1()
    return KnowledgeBriefClaimExpectationV1(
        resolution=value.resolution,
        subject=(
            None
            if value.subject is None
            else _subject_address(value.subject, field_path="input.claim_refs[].expect.subject")
        ),
        claim_type=value.claim_type,
    )


def _brief_payload(value: BriefInput, *, tree: dict[str, bytes]) -> ClaimAuthoringPayloadV1:
    claim_refs: list[KnowledgeBriefClaimRefV1] = []
    for index, reference in enumerate(value.claim_refs):
        path = claim_path(reference.claim_id)
        content = tree.get(path)
        if content is None:
            raise AuthoringInputError(
                "playbill.authoring.brief_claim_ref_unresolved",
                f"input.claim_refs[{index}].claim_id",
                "The referenced Claim is not accepted at the intent base.",
                "Choose a Claim returned at this coordinate.",
            )
        claim = parse_claim(content, path=path)
        resolved_digest = claim_statement_digest(claim.statement).tagged
        if reference.statement_digest not in {None, resolved_digest}:
            raise AuthoringInputError(
                "playbill.authoring.brief_statement_digest_mismatch",
                f"input.claim_refs[{index}].statement_digest",
                "The expert statement digest does not match the intent base.",
                "Remove the digest or replace it with the current Claim statement digest.",
            )
        claim_refs.append(
            KnowledgeBriefClaimRefV1(
                claim_id=reference.claim_id,
                statement_digest=resolved_digest,
                expect=_brief_expectation(reference.expect),
            )
        )
    query_refs: list[KnowledgeBriefQueryRefV1] = []
    for index, query_reference in enumerate(value.query_refs):
        path = query_definition_path(query_reference.query_id)
        content = tree.get(path)
        if content is None:
            raise AuthoringInputError(
                "playbill.authoring.brief_query_ref_unresolved",
                f"input.query_refs[{index}].query_id",
                "The referenced QueryDefinition is not accepted at the intent base.",
                "Choose a query returned at this coordinate.",
            )
        query = parse_query_definition(content, path=path)
        resolved_digest = query_definition_digest(query).tagged
        if query_reference.definition_digest not in {None, resolved_digest}:
            raise AuthoringInputError(
                "playbill.authoring.brief_definition_digest_mismatch",
                f"input.query_refs[{index}].definition_digest",
                "The expert definition digest does not match the intent base.",
                "Remove the digest or replace it with the current QueryDefinition digest.",
            )
        query_refs.append(
            KnowledgeBriefQueryRefV1(
                query_id=query_reference.query_id,
                definition_digest=resolved_digest,
                parameters=query_reference.parameters,
                render_field=query_reference.render_field,
            )
        )
    brief_fields: dict[str, object] = {
        "purpose": value.purpose,
        "kind": value.brief_kind,
        "prose": value.prose,
        "claim_refs": tuple(
            sorted(claim_refs, key=lambda item: item.model_dump_json().encode("utf-8"))
        ),
        "query_refs": tuple(
            sorted(query_refs, key=lambda item: item.model_dump_json().encode("utf-8"))
        ),
    }
    if value.audience is not None:
        brief_fields["audience"] = value.audience
    brief = KnowledgeBriefValueV1.model_validate(brief_fields)
    return ClaimAuthoringPayloadV1(
        statement=AuthoringClaimStatementV1(
            subject=_subject_address(value.subject, field_path="input.subject"),
            predicate=KNOWLEDGE_BRIEF_PREDICATE,
            object=LiteralClaimObject(value=brief.model_dump(mode="json")),
            role="normative",
        ),
        rationale=value.rationale,
        source=SelfSourceBodyV1(
            content_base64=base64.b64encode(value.prose.encode("utf-8")).decode("ascii")
        ),
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


def lower_authoring_input(value: AuthoringInputV1, *, tree: dict[str, bytes]) -> AuthoringPayloadV1:
    """Resolve one input against exactly the supplied accepted tree."""

    if isinstance(value, ClaimInput):
        return _claim_payload(value)
    if isinstance(value, BriefInput):
        return _brief_payload(value, tree=tree)
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
            authority=value.authority,
            activation_policy=value.activation_policy,
            owned_contracts=contracts,
            retire=value.retire,
        )
    return ProcedureAuthoringPayloadV1(
        definition=definition,
        authority=value.authority,
        activation_policy=value.activation_policy,
        retire=value.retire,
    )


__all__ = [
    "AcceptedReferenceInput",
    "AuthoringInputError",
    "AuthoringInputV1",
    "BriefInput",
    "CarriedContractInput",
    "CarriedContractReferenceInput",
    "ClaimInput",
    "ProcedureInput",
    "SlotReferenceInput",
    "WorkingSelectionInput",
    "lower_bound_claim_input",
    "lower_authoring_input",
]
