"""Synchronous, agent-oriented authoring facade over the Playbill wire ISA."""

from __future__ import annotations

import base64
import json
import os
import re
import time
import warnings
from collections import OrderedDict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeVar, cast

from pydantic import SecretStr, TypeAdapter

import cruxible_client.compatibility as client_compatibility
from cruxible_client import contracts as api
from cruxible_client.authoring.attestations import (
    ClaimAttestationV2Signer,
    append_prepared_claim_attestation,
)
from cruxible_client.authoring.blocks import (
    assert_independent_projection_evidence,
    repin_projection_block,
    sync_projection_blocks,
)
from cruxible_client.authoring.context import (
    PlaybillContextResolutionError,
    resolve_playbill_context,
)
from cruxible_client.authoring.sdk_types import (
    AccessProfile,
    ActivationPolicy,
    CallSite,
    CapabilityNotServed,
    CaptureRef,
    Cardinality,
    ClaimObjectKind,
    ClaimRef,
    ClaimRole,
    ClaimTypeRef,
    Diagnostic,
    Disposition,
    Duration,
    EffectivePeriod,
    ExactContent,
    ExactContentTypeError,
    LiteralValue,
    LiteralValueTypeError,
    PendingClaimTypeRef,
    PendingSubjectRef,
    ProcedureRef,
    QueryRef,
    ReferenceKindError,
    ReferentSensitivity,
    RefKind,
    SlotRef,
    SourceMapEntry,
    SourceRef,
    SubjectRef,
    TypedRef,
)
from cruxible_client.authoring.selectors import (
    EvidenceSelection,
    FileSelector,
    WorkspaceSources,
)
from cruxible_client.authoring.source_map import (
    DiagnosticSourceMap,
    capture_keyword_sites,
    entries_for_keywords,
)
from cruxible_client.authoring.workspace import (
    activate_with_workspace_refresh,
    observe_playbill_next_workspace,
    observe_playbill_next_workspace_with_coverage,
    refresh_workspace_floor,
)
from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_client.contracts.authoring.models import (
    AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST,
    AUTHORING_SDK_VERSION,
    AuthoringChangeSetMemberV1,
    AuthoringClaimStatementV1,
    AuthoringExactContentObjectV1,
    AuthoringExistingClaimDispositionV1,
    AuthoringProgramOperationV1,
    AuthoringProgramStampV1,
    AuthoringReferenceExpectationV1,
    ChangeSetAuthoringPayloadV1,
    ClaimAuthoringPayloadV1,
    ClaimAuthoringPayloadV2,
    ClaimAuthoringPayloadV3,
    ClaimDependencyDraftsV1,
    ClaimRetirementMemberV1,
    ClaimTypeAuthoringPayloadV1,
    ClaimTypeSuccessionDependentV1,
    ClaimTypeSuccessionMemberV1,
    ExistingCaptureCitationSourceV1,
    ProcedureAuthoringPayloadV2,
    SelfSourceBodyV1,
    SubjectAuthoringPayloadV1,
    authoring_member_identity,
    authoring_program_digest,
)
from cruxible_client.contracts.canonical import (
    CanonicalValue,
    Sha256Value,
    normalize_canonical,
    typed_digest,
)
from cruxible_client.contracts.captures import (
    capture_contract_digest,
    capture_contract_path,
    foreign_source_capture_contract,
)
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestationAppendResultV1,
    ClaimStance,
    PreparedClaimAttestationRequestV1,
)
from cruxible_client.contracts.claim_types import (
    ClaimAttestationConsequencePolicyV1,
    ClaimEvidenceFreshnessV1,
    ClaimFreshnessDurationV1,
    ClaimType,
)
from cruxible_client.contracts.claims import (
    ClaimArtifactAny,
    ClaimArtifactV2,
    ClaimArtifactV3,
    ClaimRetireDependentV1,
    ClaimRetirementReason,
    ClaimRetireRequestV1,
    ClaimUnsupportedFormatError,
    LiteralClaimObject,
    SubjectClaimObject,
)
from cruxible_client.contracts.declared_blocks import ProjectionBlockStampV1
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicyV1,
    ClaimEvidenceAdmissionPolicyV1,
    ClaimEvidenceAdmissionRuleV1,
    ClaimResolutionPolicyV1,
)
from cruxible_client.contracts.predictions import (
    ObservationSettlementEvidenceV1,
    PlaybillPredictRequestV1,
    PredictionClaimPayloadV1,
    PredictionEqualityRuleV1,
    PredictionObservationSelectorV1,
    PredictionPresenceRuleV1,
    PredictionRuleV1,
    PredictionThresholdRuleV1,
    TerminalSettlementEvidenceV1,
)
from cruxible_client.contracts.procedures.models import (
    ProcedureDefinitionV3,
    ProcedureDefinitionV4,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import SubjectShell
from cruxible_client.contracts.temporal import format_datetime
from cruxible_client.errors import CoreError
from cruxible_client.transport.http import CruxibleClient, connect_orientation_budget

if TYPE_CHECKING:  # pragma: no cover - typing only
    from cruxible_client.authoring.world import World

SDK_CONTRACT_SNAPSHOT_DIGEST = AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST

_SUBJECT_RE = re.compile(
    r"^(?P<kind>[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})*)/"
    r"(?P<identifier>[a-z][a-z0-9_.-]{0,255})$"
)
# Object kinds this client understands from an accepted ClaimType envelope.
# Anything outside the set is skew, not caller error; see _claim_type_object_kind.
_CLAIM_TYPE_OBJECT_KINDS = frozenset({"literal", "subject", "exact_content"})
_CLAIM_ADAPTER: TypeAdapter[ClaimArtifactAny] = TypeAdapter(ClaimArtifactAny)
_PREDICTION_RULE_ADAPTER: TypeAdapter[PredictionRuleV1] = TypeAdapter(PredictionRuleV1)
_RETIRE_CLOSURE_MISMATCH_CODE = "playbill.claim.retire_closure_mismatch"
_CLAIM_RETIRE_OPERATION_DOMAIN = "playbill-claim-retire-operation-v1"
_RETIREMENT_SUBMISSION_CACHE_LIMIT = 128
# The one deprecation notice this package emits, in the shape
# `cruxible_core.deprecation` serializes -- {surface, replacement,
# removal_version}, compact and key-sorted. The client cannot import the core
# registry, so a guardrail pins the two spellings equal.
_BLOCK_SYNC_DISCARD_LOCAL_DEPRECATION = json.dumps(
    {
        "removal_version": "0.6.0",
        "replacement": "`--accept-local`, which re-stamps the block on the body the author wrote",
        "surface": "playbill block sync --discard-local",
    },
    separators=(",", ":"),
    sort_keys=True,
)


def _coordinate(value: api.PlaybillAcceptedCoordinate | Mapping[str, object]) -> AcceptedCoordinate:
    payload = value.model_dump(mode="json") if hasattr(value, "model_dump") else dict(value)
    return AcceptedCoordinate.model_validate(payload)


def _api_coordinate(value: AcceptedCoordinate) -> api.PlaybillAcceptedCoordinate:
    return api.PlaybillAcceptedCoordinate.model_validate(value.model_dump(mode="json"))


def _subject_parts(value: str) -> tuple[str, str]:
    match = _SUBJECT_RE.fullmatch(value)
    if match is None:
        raise ValueError("subject must use canonical <subject-kind>/<subject-id> shorthand")
    return match["kind"], match["identifier"]


def _subject_address(value: str) -> SemanticAddress:
    kind, identifier = _subject_parts(value)
    return SemanticAddress.whole_artifact(f"subjects/{kind}/{identifier}.json")


def _address(value: str | TypedRef, expected: RefKind) -> str:
    if isinstance(value, str):
        return value
    if value.kind is not expected:
        raise ReferenceKindError(
            f"expected {expected.value} reference, received {value.kind.value}"
        )
    return value.address


@dataclass(frozen=True)
class ClaimView:
    """The few Claim fields a caller reads, lifted out of the fact array."""

    claim_id: str
    revision: int
    subject: str
    predicate: str
    qualifier: str | None
    role: str
    object_kind: str
    value: object
    lifecycle_state: str
    verdict: str
    captures: tuple[CaptureRef, ...]


def _address_path(value: object) -> str:
    if isinstance(value, Mapping):
        return str(value.get("artifact_path", ""))
    return ""


_EnumT = TypeVar("_EnumT", bound=Enum)


def _enum(value: _EnumT | str, kind: type[_EnumT], *, label: str) -> _EnumT:
    """Accept the enum or its exact string value.

    Every vocabulary here is a `str, Enum`, so a plain string reads as correct
    and only fails deep in the call as an AttributeError on `.value` -- at
    runtime, not at typecheck. Coerce at the boundary instead, and name the
    admissible values when the string is not one of them.
    """

    if isinstance(value, kind):
        return value
    if isinstance(value, str):
        try:
            return kind(value)
        except ValueError:
            admissible = ", ".join(sorted(item.value for item in kind))
            raise ValueError(f"{label} must be one of: {admissible}") from None
    raise TypeError(f"{label} must be a {kind.__name__} or one of its string values")


_REFERENCE_KINDS: Mapping[RefKind, str] = {
    RefKind.SUBJECT: "Subject",
    RefKind.CLAIM_TYPE: "ClaimType",
    RefKind.CLAIM: "Claim",
    RefKind.PROCEDURE: "Procedure",
    RefKind.QUERY: "QueryDefinition",
    RefKind.SOURCE: "Source",
}


def _expectation(
    value: str | TypedRef,
    *,
    expected: RefKind,
    payload_path: str,
) -> AuthoringReferenceExpectationV1 | None:
    if isinstance(value, str):
        return None
    _address(value, expected)
    if expected is RefKind.SLOT:
        return None
    if isinstance(value, (PendingSubjectRef, PendingClaimTypeRef)):
        # A same-set definition did not exist at the coordinate this ref names,
        # so asserting it there would refuse in preflight against the base tree.
        # The set lowers definitions before the members that read them.
        return None
    return AuthoringReferenceExpectationV1(
        payload_path=payload_path,
        artifact_kind=cast(Any, _REFERENCE_KINDS[expected]),
        address=value.address,
        minted_coordinate=value.coordinate,
    )


def _sorted_expectations(
    values: Sequence[AuthoringReferenceExpectationV1 | None],
) -> tuple[AuthoringReferenceExpectationV1, ...]:
    return tuple(
        sorted(
            (value for value in values if value is not None),
            key=lambda item: (
                item.payload_path.encode("utf-8"),
                item.artifact_kind.encode("ascii"),
                item.address.encode("utf-8"),
            ),
        )
    )


def _program_stamp(operation: str, decisions: Mapping[str, object]) -> AuthoringProgramStampV1:
    operation_value = AuthoringProgramOperationV1(operation=operation, decisions=dict(decisions))
    return AuthoringProgramStampV1(
        program_digest=authoring_program_digest(
            sdk_contract_snapshot_digest=SDK_CONTRACT_SNAPSHOT_DIGEST,
            operations=(operation_value,),
        ),
        sdk_version=AUTHORING_SDK_VERSION,
        sdk_contract_snapshot_digest=SDK_CONTRACT_SNAPSHOT_DIGEST,
    )


def _claim_from_public_view(view: api.PlaybillClaimViewV2) -> ClaimArtifactAny:
    """Reconstruct the exact Claim from its pure projection envelope and facts."""

    statement = next(
        (
            fact.get("value")
            for fact in view.facts
            if fact.get("schema_id") == "playbill.claim.statement"
        ),
        None,
    )
    backing = next(
        (
            fact.get("value")
            for fact in view.facts
            if fact.get("schema_id") == "playbill.claim.backing"
        ),
        None,
    )
    lifecycle = next(
        (
            fact.get("value")
            for fact in view.facts
            if fact.get("schema_id") == "playbill.claim.lifecycle"
        ),
        None,
    )
    identity = view.envelope.get("identity")
    artifact_format = view.envelope.get("format_tag")
    if not (
        isinstance(identity, str)
        and isinstance(statement, dict)
        and isinstance(backing, dict)
        and isinstance(lifecycle, dict)
        and isinstance(artifact_format, str)
    ):
        raise ValueError("Claim read lacks its complete canonical artifact")
    if artifact_format == "playbill-claim-v2":
        model: type[ClaimArtifactV2] | type[ClaimArtifactV3] = ClaimArtifactV2
    elif artifact_format == "playbill-claim-v3":
        model = ClaimArtifactV3
    else:
        raise ClaimUnsupportedFormatError(
            f"{ClaimUnsupportedFormatError.error_code}: {artifact_format!r}"
        )
    return _CLAIM_ADAPTER.validate_python(
        model.model_validate(
            {
                "artifact_format": artifact_format,
                "identity": {
                    "kind": "Claim",
                    "name": identity.removeprefix("Claim:"),
                },
                "statement": statement,
                "backing": backing,
                "pins": lifecycle.get("pins"),
                "lifecycle": lifecycle.get("lifecycle"),
                **(
                    {"retirement": lifecycle.get("retirement")}
                    if artifact_format == "playbill-claim-v3"
                    else {}
                ),
            }
        )
    )


@dataclass(frozen=True)
class KnowledgeCard:
    kind: RefKind
    identity: str
    coordinate: AcceptedCoordinate
    value: object

    @property
    def ref(self) -> TypedRef:
        constructors = {
            RefKind.SUBJECT: SubjectRef,
            RefKind.CLAIM_TYPE: ClaimTypeRef,
            RefKind.CLAIM: ClaimRef,
            RefKind.PROCEDURE: ProcedureRef,
            RefKind.QUERY: QueryRef,
            RefKind.SOURCE: SourceRef,
        }
        constructor = constructors.get(self.kind)
        if constructor is None:
            raise ReferenceKindError(f"{self.kind.value} cards do not mint references")
        return cast(TypedRef, constructor(address=self.identity, coordinate=self.coordinate))


@dataclass(frozen=True)
class SearchPage:
    coordinate: AcceptedCoordinate
    evaluation_time: str
    rows: tuple[dict[str, object], ...]
    result_digest: str
    cursor: dict[str, object] | None
    truncated: bool
    orientation: dict[str, object] | None = None


@dataclass(frozen=True)
class NextPage:
    coordinate: AcceptedCoordinate
    evaluation_time: str
    items: tuple[dict[str, object], ...]
    result_digest: str
    observed_domains: tuple[str, ...]
    unobserved_domains: tuple[str, ...]
    attestation_head_digest: str | None = None

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.items)


@dataclass(frozen=True)
class ClaimTypeDraft:
    _playbill: Playbill = field(repr=False, compare=False)
    definition: ClaimType

    @property
    def predicate(self) -> str:
        return self.definition.predicate

    def propose(self, *, proposal_name: str) -> Proposal:
        result = self._playbill._client.propose_playbill_claim_type(
            self._playbill._instance_id,
            claim_type=self.definition.model_dump(mode="json"),
            proposal_name=proposal_name,
            base=_api_coordinate(self._playbill.coordinate),
        )
        return Proposal.from_inspection(self._playbill, result)


@dataclass(frozen=True)
class _IntentDraft:
    _playbill: Playbill = field(repr=False, compare=False)
    payload: (
        ClaimAuthoringPayloadV1
        | ClaimAuthoringPayloadV2
        | ClaimAuthoringPayloadV3
        | ProcedureAuthoringPayloadV2
        | SubjectAuthoringPayloadV1
        | ChangeSetAuthoringPayloadV1
    )
    reference_expectations: tuple[AuthoringReferenceExpectationV1, ...]
    program_stamp: AuthoringProgramStampV1
    source_map: DiagnosticSourceMap

    def prepare(self) -> Intent:
        result = self._playbill._client.compile_playbill_authoring(
            self._playbill._instance_id,
            payload=self.payload.model_dump(mode="json"),
            reference_expectations=[
                item.model_dump(mode="json") for item in self.reference_expectations
            ],
            program_stamp=self.program_stamp.model_dump(mode="json"),
        )
        return Intent.from_preflight(self._playbill, self, result)


@dataclass(frozen=True)
class ClaimDraft(_IntentDraft):
    def derived_by(self, derivation: object) -> ClaimDraft:
        del derivation
        raise CapabilityNotServed(
            code="playbill.sdk.derivation_carry_not_served",
            capability="derivation_carry",
            repair=("Remove derived_by() or use a separately approved derivation-carry contract."),
        )


@dataclass(frozen=True)
class Prediction:
    """A submitted predicted Claim and its immutable settlement declaration."""

    _playbill: Playbill = field(repr=False, compare=False)
    prediction_id: str
    intent_id: str
    proposal_id: str
    predicted_claim_id: str
    declaration_digest: str

    @property
    def proposal(self) -> Proposal:
        return Proposal(self._playbill, self.proposal_id)


@dataclass(frozen=True)
class PredictionSettlement:
    prediction_id: str
    outcome: bool
    relation: dict[str, object]


@dataclass(frozen=True)
class ProcedureDraft(_IntentDraft):
    pass


def carry(claim: str | ClaimRef) -> ClaimTypeSuccessionDependentV1:
    """Carry one dependent to the successor by re-pinning it, unchanged.

    Available when the dependent still says something true under the successor.
    A successor that changes `object_kind` refuses this for a live Claim: its
    object no longer says what the ClaimType now means.
    """

    return ClaimTypeSuccessionDependentV1(
        identity=_claim_identity(claim),
        disposition="successor",
    )


def rescind(claim: str | ClaimRef) -> ClaimTypeSuccessionDependentV1:
    """Tombstone one dependent because it should never have been stated.

    The tombstone keeps the exact statement it was accepted with, under the
    vocabulary it was accepted under -- that is what makes the record readable
    after the vocabulary moves, rather than silently rewritten.
    """

    return ClaimTypeSuccessionDependentV1(
        identity=_claim_identity(claim),
        disposition="retire",
        claim_retirement_reason="was-rescinded",
    )


def retire(
    claim: str | ClaimRef,
    *,
    reason: ClaimRetirementReason,
    effective_until: datetime | None = None,
) -> ClaimTypeSuccessionDependentV1:
    """Retire one dependent with an attributed reason as the succession lands."""

    return ClaimTypeSuccessionDependentV1(
        identity=_claim_identity(claim),
        disposition="retire",
        claim_retirement_reason=reason,
        claim_effective_until=effective_until,
    )


def re_author(
    claim: str | ClaimRef,
    *,
    with_: str | ClaimRef | None = None,
) -> ClaimTypeSuccessionDependentV1:
    """Say this dependent again, under the successor, as a sibling Claim member.

    The sibling revises this same Claim -- a re-authoring keeps the identity,
    the subject, the predicate and the exact predecessor digest of what it
    re-states -- so `with_` is only ever an explicit spelling of what `claim`
    already says, and `re_author(claim)` alone is complete.
    """

    return ClaimTypeSuccessionDependentV1(
        identity=_claim_identity(claim),
        disposition="re_author",
        successor_claim_id=_claim_id(claim if with_ is None else with_),
    )


def _claim_id(claim: str | ClaimRef) -> str:
    return _address(claim, RefKind.CLAIM).removeprefix("Claim:")


def _claim_identity(claim: str | ClaimRef) -> ArtifactIdentity:
    return ArtifactIdentity(kind="Claim", name=_claim_id(claim))


@dataclass(frozen=True)
class _ChangeSetMember:
    payload: AuthoringChangeSetMemberV1
    expectations: tuple[AuthoringReferenceExpectationV1, ...]
    source_map: DiagnosticSourceMap
    decisions: dict[str, object]


@dataclass
class ChangeSetDraft:
    """One authoring intent under construction, carrying any mix of members.

    Authoring surfaces and changesets are one-to-one: everything added here
    lowers once, proposes once and generates once, and the whole intent admits
    or refuses together. There is no member ceiling -- how many members one
    daemon will receive in a single submission is an operator admission knob.
    """

    _playbill: Playbill = field(repr=False, compare=False)
    rationale: str | None = None
    _members: list[_ChangeSetMember] = field(default_factory=list, repr=False)

    def claim(
        self,
        *,
        subject: str | SubjectRef,
        predicate: str | ClaimTypeRef,
        value: CanonicalValue | SubjectRef | LiteralValue | ExactContent,
        role: ClaimRole | str,
        rationale: str,
        supported_by: EvidenceSelection | CaptureRef | None,
        copied_from: EvidenceSelection | CaptureRef | None,
        self_source: str | None,
        qualifier: str | None,
        effective_period: EffectivePeriod | None,
        revises: str | ClaimRef | None,
        dispositions: Mapping[str | ClaimRef, Disposition | str],
        subject_definition: SubjectDraft | None,
        claim_type_definition: ClaimTypeDraft | None,
    ) -> ChangeSetDraft:
        """Add one Claim to this changeset; the signature is `Playbill.claim`'s."""

        draft = self._playbill._claim_draft(
            sites=capture_keyword_sites("claim", stacklevel=1),
            subject=subject,
            predicate=predicate,
            value=value,
            role=role,
            rationale=rationale,
            supported_by=supported_by,
            copied_from=copied_from,
            self_source=self_source,
            qualifier=qualifier,
            effective_period=effective_period,
            revises=revises,
            dispositions=dispositions,
            subject_definition=subject_definition,
            claim_type_definition=claim_type_definition,
            staged_object_kinds=self._staged_object_kinds(),
        )
        assert isinstance(draft.payload, ClaimAuthoringPayloadV1)
        self._members.append(
            _ChangeSetMember(
                payload=draft.payload,
                expectations=draft.reference_expectations,
                source_map=draft.source_map,
                decisions={"kind": "claim", "predicate": _address(predicate, RefKind.CLAIM_TYPE)},
            )
        )
        return self

    def _staged_object_kinds(self) -> dict[str, str]:
        """The object kinds this set's own ClaimType definitions declare, by predicate.

        A ref returned by `.claim_type(...)` already carries its object kind, so
        a caller who keeps the ref needs nothing here. A caller who names the
        predicate as a string does: the object-kind lookup would otherwise skip
        the definition sitting in this very set and read the accepted
        coordinate, where in a first generation there is no coordinate at all.
        Same set, same answer, whichever way the predicate is spelled.
        """

        return {
            member.payload.claim_type.predicate: member.payload.claim_type.object_kind
            for member in self._members
            if isinstance(member.payload, ClaimTypeAuthoringPayloadV1)
        }

    def subject(self, definition: SubjectDraft | SubjectShell) -> PendingSubjectRef:
        """Define one Subject inside this changeset, and return a ref to it.

        A Claim member may still carry its Subject as a dependency draft; this
        is for the Subjects a set defines that no single Claim owns.

        The ref it returns is usable as `subject=` or `value=` in the same set,
        which is what lets one changeset define a Subject and say something
        about it without the caller retyping the address as a string.
        """

        shell = definition.shell if isinstance(definition, SubjectDraft) else definition
        self._members.append(
            _ChangeSetMember(
                payload=SubjectAuthoringPayloadV1(subject=shell),
                expectations=(),
                source_map=DiagnosticSourceMap(()),
                decisions={"kind": "subject", "subject": shell.identity.name},
            )
        )
        return PendingSubjectRef(
            address=shell.identity.name,
            coordinate=self._playbill.coordinate,
        )

    def claim_type(self, definition: ClaimTypeDraft | ClaimType) -> PendingClaimTypeRef:
        """Define one whole ClaimType inside this changeset, and return a ref.

        Succeeding an accepted ClaimType stays on `/claim-types/proposals`,
        where the migration a succession demands is decided.

        The ref it returns is usable as `predicate=` in the same set, and
        carries the object kind the definition declares so a Claim under it
        lowers without reading a ClaimType that is not accepted yet.
        """

        value = definition.definition if isinstance(definition, ClaimTypeDraft) else definition
        self._members.append(
            _ChangeSetMember(
                payload=ClaimTypeAuthoringPayloadV1(claim_type=value),
                expectations=(),
                source_map=DiagnosticSourceMap(()),
                decisions={"kind": "claim_type", "predicate": value.predicate},
            )
        )
        return PendingClaimTypeRef(
            address=value.predicate,
            coordinate=self._playbill.coordinate,
            object_kind=value.object_kind,
        )

    def retire(
        self,
        claim: str | ClaimRef,
        *,
        reason: ClaimRetirementReason,
        effective_until: datetime | None = None,
        dependents: Sequence[ClaimRetireDependentV1] = (),
    ) -> ChangeSetDraft:
        """Retire one accepted Claim, and its live closure, inside this changeset.

        Takes exactly what `Playbill.retire_claim` takes: the SDK's own rows
        and refs spell a Claim identity `Claim:CLM-...`, so a builder that
        refused the prefix made one library disagree with itself. The member
        carries the one canonical bare spelling, which is what keeps two
        spellings of one retirement on one member identity and one digest.
        """

        address = _address(claim, RefKind.CLAIM).removeprefix("Claim:")
        self._members.append(
            _ChangeSetMember(
                payload=ClaimRetirementMemberV1(
                    claim_ref=address,
                    reason=reason,
                    effective_until=effective_until,
                    dependents=tuple(dependents),
                ),
                expectations=(),
                source_map=DiagnosticSourceMap(()),
                decisions={"kind": "retire", "claim": address, "reason": reason},
            )
        )
        return self

    def succeed_claim_type(
        self,
        successor: ClaimTypeDraft | ClaimType,
        *,
        dependents: Sequence[ClaimTypeSuccessionDependentV1] = (),
    ) -> ChangeSetDraft:
        """Succeed one accepted ClaimType, and settle its closure, in this set.

        Vocabulary evolution is one epistemic move -- "I need this distinction,
        and here is everything it changes" -- so it lands in the same signed
        generation as the Claims that speak the new vocabulary. Write the
        dependents with `carry`, `rescind`, `retire` and `re_author`; the
        closure must be exact, and preflight names every member of it that is
        still missing.
        """

        value = successor.definition if isinstance(successor, ClaimTypeDraft) else successor
        self._members.append(
            _ChangeSetMember(
                payload=ClaimTypeSuccessionMemberV1(
                    successor=value,
                    dependents=tuple(
                        sorted(
                            dependents,
                            key=lambda item: item.identity.qualified.encode("utf-8"),
                        )
                    ),
                ),
                expectations=(),
                source_map=DiagnosticSourceMap(()),
                decisions={
                    "kind": "claim_type_succession",
                    "predicate": value.predicate,
                    "dependents": [
                        {"claim": item.identity.qualified, "disposition": item.disposition}
                        for item in sorted(
                            dependents,
                            key=lambda item: item.identity.qualified.encode("utf-8"),
                        )
                    ],
                },
            )
        )
        return self

    def prepare(self) -> Intent:
        """Compile and preflight the whole changeset as one intent."""

        return self._compiled().prepare()

    def _compiled(self) -> _IntentDraft:
        """Fold every member into exactly one intent draft."""

        if not self._members:
            raise ValueError("a changeset needs at least one member")
        payload = ChangeSetAuthoringPayloadV1(
            members=tuple(
                sorted(
                    (member.payload for member in self._members),
                    key=lambda item: authoring_member_identity(item).encode("utf-8"),
                )
            ),
            # The prose travels now, instead of only being hashed into the
            # program digest: the daemon writes it as the candidate commit's
            # subject, which is the one place a reviewer reading Git looks for
            # why a change set exists.
            rationale=self.rationale,
        )
        index_by_identity = {
            authoring_member_identity(member): index for index, member in enumerate(payload.members)
        }
        expectations: list[AuthoringReferenceExpectationV1 | None] = []
        entries: list[SourceMapEntry] = []
        decisions: list[dict[str, object]] = []
        for member in sorted(
            self._members,
            key=lambda item: index_by_identity[authoring_member_identity(item.payload)],
        ):
            prefix = f"members[{index_by_identity[authoring_member_identity(member.payload)]}]."
            expectations.extend(
                expectation.model_copy(update={"payload_path": prefix + expectation.payload_path})
                for expectation in member.expectations
            )
            entries.extend(
                SourceMapEntry(
                    builder_path=entry.builder_path,
                    emitted_paths=tuple(prefix + path for path in entry.emitted_paths),
                    call_site=entry.call_site,
                )
                for entry in member.source_map.entries
            )
            decisions.append(dict(member.decisions))
        return _IntentDraft(
            self._playbill,
            payload,
            _sorted_expectations(expectations),
            _program_stamp("changes", {"members": decisions, "rationale": self.rationale}),
            DiagnosticSourceMap(tuple(entries)),
        )


@dataclass(frozen=True)
class SubjectDraft(_IntentDraft):
    shell: SubjectShell

    @property
    def address(self) -> str:
        return self.shell.identity.name

    def propose(self, *, proposal_name: str) -> Proposal:
        del proposal_name
        from cruxible_client.contracts.errors import PlaybillDeprecatedWriteError

        raise PlaybillDeprecatedWriteError(
            replacement="the authoring coordinator with payload kind 'subject'"
        )


class Intent:
    def __init__(
        self,
        playbill: Playbill,
        draft: _IntentDraft,
        raw: Mapping[str, object],
        *,
        preflight: api.PlaybillAuthoringPreflightResult | None = None,
        candidate_status: api.PlaybillCandidateStatus | None = None,
    ) -> None:
        self._playbill = playbill
        self._draft = draft
        self._raw = dict(raw)
        self._preflight = preflight
        self._candidate_status = candidate_status

    @classmethod
    def from_preflight(
        cls,
        playbill: Playbill,
        draft: _IntentDraft,
        result: api.PlaybillAuthoringPreflightResult,
    ) -> Intent:
        intent_id = result.certificate.get("intent_id")
        if not isinstance(intent_id, str):
            raise ValueError("preflight certificate did not name an intent")
        raw = playbill._client.get_playbill_authoring_intent(
            playbill._instance_id, intent_id
        ).intent
        return cls(playbill, draft, raw, preflight=result)

    @property
    def intent_id(self) -> str:
        value = self._raw.get("intent_id")
        if not isinstance(value, str):
            raise ValueError("authoring intent response omitted intent_id")
        return value

    @property
    def revision(self) -> int:
        value = self._raw.get("intent_revision")
        if not isinstance(value, int):
            raise ValueError("authoring intent response omitted intent_revision")
        return value

    @property
    def refused(self) -> bool:
        return self._preflight is not None and self._preflight.verdict == "refused"

    @property
    def lint(self) -> api.PlaybillClaimTypeProposalLint | None:
        return None if self._preflight is None else self._preflight.lint

    @property
    def warnings(self) -> tuple[dict[str, Any], ...]:
        lint = self.lint
        return () if lint is None else tuple(lint.warnings)

    @property
    def diagnostics(self) -> tuple[Diagnostic, ...]:
        if self._preflight is None:
            return ()
        raw_diagnostics = self._preflight.frontier.get("diagnostics", [])
        if not isinstance(raw_diagnostics, list):
            return ()
        result: list[Diagnostic] = []
        for raw in raw_diagnostics:
            if not isinstance(raw, Mapping):
                continue
            offending = str(raw.get("offending_element", ""))
            repairs = raw.get("repairs", [])
            result.append(
                Diagnostic(
                    code=str(raw.get("code", "")),
                    stage=str(raw.get("stage", "")),
                    offending_element=offending,
                    message=str(raw.get("message", "")),
                    repair=tuple(repairs) if isinstance(repairs, list) else (),
                    owner=cast(str | None, raw.get("owner")),
                    disposition=cast(str | None, raw.get("disposition")),
                    call_site=self._draft.source_map.locate(offending),
                )
            )
        return tuple(result)

    @property
    def path_to_acceptance(self) -> tuple[dict[str, object], ...]:
        status = self.status()
        return tuple(cast(dict[str, object], item) for item in status.path_to_acceptance)

    @property
    def publication(self) -> Publication | None:
        """The one publication a singular Claim intent owns, if it has one."""

        expectation = self._raw.get("insertion_expectation")
        if not isinstance(expectation, Mapping):
            self._refresh_raw()
            expectation = self._raw.get("insertion_expectation")
        if not isinstance(expectation, Mapping):
            return None
        return Publication(self, dict(expectation))

    @property
    def publications(self) -> tuple[Publication, ...]:
        """Every publication this intent owns, one per publishing Claim member."""

        expectations = self._raw.get("insertion_expectations")
        if not isinstance(expectations, list) or not expectations:
            self._refresh_raw()
            expectations = self._raw.get("insertion_expectations")
        if not isinstance(expectations, list):
            return ()
        return tuple(
            Publication(self, dict(item)) for item in expectations if isinstance(item, Mapping)
        )

    def _refresh_raw(self) -> None:
        self._raw = self._playbill._client.get_playbill_authoring_intent(
            self._playbill._instance_id, self.intent_id
        ).intent

    def prepare(self) -> Intent:
        result = self._playbill._client.preflight_playbill_authoring_intent(
            self._playbill._instance_id, self.intent_id
        )
        self._preflight = result
        self._raw = self._playbill._client.get_playbill_authoring_intent(
            self._playbill._instance_id, self.intent_id
        ).intent
        return self

    def reprepare(self, *, draft: ClaimDraft | ProcedureDraft | SubjectDraft) -> Intent:
        if draft._playbill is not self._playbill:
            raise ValueError("replacement draft belongs to another Playbill connection")
        result = self._playbill._client.compile_playbill_authoring(
            self._playbill._instance_id,
            payload=draft.payload.model_dump(mode="json"),
            intent_id=self.intent_id,
            reference_expectations=[
                item.model_dump(mode="json") for item in draft.reference_expectations
            ],
            program_stamp=draft.program_stamp.model_dump(mode="json"),
        )
        self._draft = draft
        self._preflight = result
        self._raw = self._playbill._client.get_playbill_authoring_intent(
            self._playbill._instance_id, self.intent_id
        ).intent
        return self

    def submit(self) -> Intent:
        result = self._playbill._client.submit_playbill_authoring_intent(
            self._playbill._instance_id, self.intent_id
        )
        self._raw = result.intent
        self._candidate_status = result.status
        return self

    def status(self) -> api.PlaybillCandidateStatus:
        status = self._playbill._client.playbill_authoring_intent_status(
            self._playbill._instance_id, self.intent_id
        )
        self._candidate_status = status
        return status

    def rebase(self) -> Intent:
        self._raw = self._playbill._client.rebase_playbill_authoring_intent(
            self._playbill._instance_id, self.intent_id
        ).intent
        self._preflight = None
        self._candidate_status = None
        return self

    def wait_for_acceptance(
        self,
        *,
        timeout: Duration,
        poll_interval: Duration,
    ) -> api.PlaybillCandidateStatus:
        return cast(
            api.PlaybillCandidateStatus,
            _wait_for_status(self.status, timeout=timeout, poll_interval=poll_interval),
        )


class Proposal:
    def __init__(
        self,
        playbill: Playbill,
        proposal_id: str,
        *,
        lint: api.PlaybillClaimTypeProposalLint | None = None,
    ) -> None:
        self._playbill = playbill
        self.proposal_id = proposal_id
        self.lint = lint

    @classmethod
    def from_inspection(
        cls, playbill: Playbill, inspection: api.PlaybillProposalInspection
    ) -> Proposal:
        proposal_id = inspection.proposal.get("admission", {}).get("proposal_id")
        if not isinstance(proposal_id, str):
            proposal_id = inspection.proposal.get("proposal_id")
        if not isinstance(proposal_id, str):
            raise ValueError("proposal inspection omitted proposal_id")
        return cls(playbill, proposal_id, lint=inspection.lint)

    @property
    def warnings(self) -> tuple[dict[str, Any], ...]:
        return () if self.lint is None else tuple(self.lint.warnings)

    def status(self) -> api.PlaybillProposalListEntry:
        for entry in self._playbill._client.list_playbill_proposals(
            self._playbill._instance_id
        ).entries:
            if entry.proposal_id == self.proposal_id:
                return entry
        raise ValueError(f"proposal {self.proposal_id!r} was not listed by the daemon")

    def wait_for_acceptance(
        self,
        *,
        timeout: Duration,
        poll_interval: Duration,
    ) -> api.PlaybillProposalListEntry:
        deadline = time.monotonic_ns() + timeout.value * 1_000
        while True:
            status = self.status()
            if status.terminal_reason is not None:
                return status
            if time.monotonic_ns() >= deadline:
                return status
            time.sleep(poll_interval.value / 1_000_000)


class Publication:
    """One publication expectation an EXISTING intent owns.

    Nothing mints a new one: the `publish_to` road is gone, and a projection
    block is declared with `block repin` over accepted Claims instead. What
    remains is the exit an instance that already published needs -- read the
    state, and abandon (depublish) the expectation.
    """

    def __init__(self, intent: Intent, expectation: dict[str, object]) -> None:
        self._intent = intent
        self._expectation = expectation

    @property
    def state(self) -> str:
        return str(self._expectation.get("state", "terminal"))

    @property
    def expectation_id(self) -> str:
        value = self._expectation.get("expectation_id")
        if not isinstance(value, str):
            raise ValueError("insertion expectation omitted its ID")
        return value

    def status(self) -> str:
        self._intent._refresh_raw()
        expectations = self._intent._raw.get("insertion_expectations")
        if isinstance(expectations, list):
            for item in expectations:
                if isinstance(item, Mapping) and item.get("expectation_id") == self.expectation_id:
                    self._expectation = dict(item)
                    break
        return self.state

    def abandon(self) -> Publication:
        result = self._intent._playbill._client.abandon_playbill_authoring_insertion(
            self._intent._playbill._instance_id,
            self._intent.intent_id,
            expectation_id=self.expectation_id,
        )
        self._intent._raw = result.intent
        self._expectation = result.expectation
        return self


def _wait_for_status(call: Any, *, timeout: Duration, poll_interval: Duration) -> Any:
    deadline = time.monotonic_ns() + timeout.value * 1_000
    while True:
        status = call()
        if status.state in {"accepted", "terminal", "superseded"}:
            return status
        if time.monotonic_ns() >= deadline:
            return status
        time.sleep(poll_interval.value / 1_000_000)


class Playbill:
    def __init__(
        self,
        *,
        client: CruxibleClient,
        instance_id: str,
        workspace: Path,
        access_profile: AccessProfile,
        clock: Any,
    ) -> None:
        self._client = client
        self._instance_id = instance_id
        self._workspace = workspace.expanduser().resolve()
        self._workspace_sources: WorkspaceSources | None = None
        self._access_profile = access_profile
        self._clock = clock
        self._coordinate: AcceptedCoordinate | None = None
        self._retirement_submissions: OrderedDict[str, tuple[ClaimRetireRequestV1, str]] = (
            OrderedDict()
        )

    @classmethod
    def connect(
        cls,
        *,
        context: str | Path | None = None,
        target: str | None = None,
        instance: str | None = None,
        token: SecretStr | None = None,
        workspace: Path | None = None,
        access_profile: AccessProfile | None = None,
    ) -> Playbill:
        context_path = (
            Path(context).expanduser().resolve()
            if context is not None
            else Path(
                os.environ.get(
                    "CRUXIBLE_CLI_CONTEXT_PATH",
                    str(Path.home() / ".cruxible" / "client-context.json"),
                )
            )
            .expanduser()
            .resolve()
        )
        remembered: dict[str, object] = {}
        if context_path.is_file():
            loaded = json.loads(context_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("Playbill context must contain a JSON object")
            remembered = loaded
        explicit_url = target
        explicit_socket = None
        if target is not None and target.startswith("unix:"):
            explicit_url = None
            explicit_socket = target.removeprefix("unix:")
        resolved = resolve_playbill_context(
            server_url=explicit_url,
            server_socket=explicit_socket,
            instance_id=instance,
            workspace=workspace,
            remembered=remembered,
        )
        if resolved.server_url is None and resolved.server_socket is None:
            raise ValueError("Playbill connection requires a server target")
        if not resolved.instance_id:
            if resolved.instance_transport_mismatch:
                raise PlaybillContextResolutionError(resolved.instance_transport_mismatch)
            raise ValueError("Playbill connection requires an instance")
        raw_token = (
            token.get_secret_value()
            if token is not None
            else os.environ.get("CRUXIBLE_SERVER_BEARER_TOKEN")
        )
        client = CruxibleClient(
            base_url=resolved.server_url,
            socket_path=resolved.server_socket,
            token=raw_token,
        )
        try:
            client_compatibility.check_daemon_compatibility(client)
            result = cls(
                client=client,
                instance_id=resolved.instance_id,
                workspace=resolved.workspace,
                access_profile=access_profile
                or AccessProfile(
                    profile_id="sdk-default",
                    permitted_access_classes=("instance", "public"),
                    disclose_restricted_existence=True,
                ),
                clock=lambda: datetime.now(UTC),
            )
            # Orientation walks the whole accepted world, so its cost tracks the
            # size of the instance, not the size of a call. It gets its own read
            # budget (CRUXIBLE_CLIENT_CONNECT_TIMEOUT_S) so a healthy but large
            # instance cannot read as an unreachable server.
            with connect_orientation_budget(client):
                result.refresh()
        except BaseException:
            client.close()
            raise
        return result

    @classmethod
    def _from_client(
        cls,
        client: CruxibleClient,
        *,
        instance_id: str,
        workspace: Path,
        access_profile: AccessProfile | None = None,
        clock: Any = None,
    ) -> Playbill:
        result = cls(
            client=client,
            instance_id=instance_id,
            workspace=workspace,
            access_profile=access_profile
            or AccessProfile(
                profile_id="sdk-default",
                permitted_access_classes=("instance", "public"),
                disclose_restricted_existence=True,
            ),
            clock=clock or (lambda: datetime.now(UTC)),
        )
        result.refresh()
        return result

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Playbill:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @property
    def _sources(self) -> WorkspaceSources:
        """Resolve the workspace source catalog the first time one is needed.

        Reads -- orientation, search, `world()` -- touch no working tree, so
        demanding a catalog at connect made every read depend on a writer's
        setup. The refusal is unchanged; it now lands on the first surface that
        actually selects from the workspace.
        """

        if self._workspace_sources is None:
            self._workspace_sources = WorkspaceSources(self._workspace)
        return self._workspace_sources

    @property
    def coordinate(self) -> AcceptedCoordinate:
        if self._coordinate is None:
            raise ValueError("Playbill has not installed an orientation coordinate")
        return self._coordinate

    @property
    def block(self) -> ProjectionBlocks:
        """Client-only declaration stamps; prose remains wholly agent-owned."""

        return ProjectionBlocks(self)

    def claim_view(self, claim: str | ClaimRef) -> ClaimView:
        """Read one accepted Claim as the few fields callers actually ask for.

        The wire read returns a fact array keyed by schema id, so answering
        "what does this Claim say, and is it believed" means walking that array
        by hand every time. This is that walk, once.
        """

        identity = _address(claim, RefKind.CLAIM) if isinstance(claim, ClaimRef) else claim
        view = self._client.get_playbill_claim(self._instance_id, identity)
        return self._typed_claim_view(view, identity)

    @staticmethod
    def _typed_claim_view(view: api.PlaybillClaimViewV2, identity: str = "") -> ClaimView:
        facts = {
            str(fact.get("schema_id")): fact.get("value")
            for fact in view.facts
            if isinstance(fact, Mapping)
        }
        statement = facts.get("playbill.claim.statement")
        lifecycle = facts.get("playbill.claim.lifecycle")
        verdict = facts.get("playbill.claim.current_verdict")
        if not isinstance(statement, Mapping):
            raise ValueError(f"accepted Claim {identity} carries no statement fact")
        item = statement.get("object")
        object_value: object = None
        object_kind = ""
        if isinstance(item, Mapping):
            object_kind = str(item.get("kind", ""))
            if object_kind == "literal":
                object_value = item.get("value")
            elif object_kind == "subject":
                address = item.get("address")
                object_value = (
                    address.get("artifact_path") if isinstance(address, Mapping) else None
                )
            else:
                object_value = item.get("content_digest")
        state = ""
        if isinstance(lifecycle, Mapping):
            inner = lifecycle.get("lifecycle")
            if isinstance(inner, Mapping):
                state = str(inner.get("state", ""))
        return ClaimView(
            claim_id=str(view.envelope.get("identity", identity)),
            revision=int(view.envelope.get("revision", 0)),
            subject=_address_path(statement.get("subject")),
            predicate=str(statement.get("predicate", "")),
            qualifier=cast(str | None, statement.get("qualifier")),
            role=str(statement.get("role", "")),
            object_kind=object_kind,
            value=object_value,
            lifecycle_state=state,
            verdict=(str(verdict.get("verdict", "")) if isinstance(verdict, Mapping) else ""),
            captures=tuple(
                CaptureRef(
                    capture_digest=account.capture_digest,
                    contract_address=capture_contract_path(
                        account.capture_contract_identity.removeprefix("CaptureContract:")
                    ),
                    coordinate=_coordinate(view.coordinate),
                    citation_role=account.citation_role,
                )
                for account in view.admission_accounts
            ),
        )

    def claim_views(self, claims: Sequence[str | ClaimRef]) -> tuple[ClaimView, ...]:
        """Read up to 256 exact or prefix identities at this connection's coordinate.

        The complete batch preserves input order and all single-view fields.
        Use explicit batches for larger selections; no population read is implied.
        """
        from cruxible_client.contracts.claim_reads import ClaimReadBatchRequestV1

        for claim in claims:
            if isinstance(claim, ClaimRef):
                self._assert_coordinate(claim.coordinate)
        request = ClaimReadBatchRequestV1(
            at=_api_coordinate(self.coordinate),
            claim_ids=tuple(
                _address(claim, RefKind.CLAIM) if isinstance(claim, ClaimRef) else claim
                for claim in claims
            ),
            evaluation_time=datetime.fromisoformat(self._evaluation_time()),
        )
        result = self._client.read_playbill_claim_batch(self._instance_id, request=request)
        self._assert_coordinate(_coordinate(result.coordinate))
        if result.truncated or result.cursor is not None or len(result.claims) != len(claims):
            raise ValueError("identity batch did not return a complete Claim selection")
        for identity, view in zip(request.claim_ids, result.claims, strict=True):
            self._assert_coordinate(_coordinate(view.coordinate))
            bare = identity.removeprefix("Claim:")
            returned = str(view.envelope.get("identity", "")).removeprefix("Claim:")
            if not returned.startswith(bare):
                raise ValueError("identity batch returned a Claim outside its requested position")
        return tuple(self._typed_claim_view(view) for view in result.claims)

    def predict(
        self,
        prediction: ClaimDraft,
        *,
        procedure: str | ProcedureRef,
        measurement_name: str,
        observation_subject: str | SubjectRef,
        observation_predicate: str | ClaimTypeRef,
        rule: PredictionRuleV1 | Mapping[str, object],
        deadline: datetime,
        observation_qualifier: str | None = None,
        outcome_class: str = "prediction-correctness",
    ) -> Prediction:
        """Submit a predicted Claim and bind its later settlement rule."""

        if prediction._playbill is not self:
            raise ValueError("prediction draft belongs to another Playbill connection")
        procedure_name = _address(procedure, RefKind.PROCEDURE)
        subject_name = _address(observation_subject, RefKind.SUBJECT)
        predicate_name = _address(observation_predicate, RefKind.CLAIM_TYPE)
        for reference in (procedure, observation_subject, observation_predicate):
            if isinstance(reference, TypedRef):
                self._assert_coordinate(reference.coordinate)
        typed_rule = (
            rule
            if isinstance(
                rule,
                (
                    PredictionEqualityRuleV1,
                    PredictionThresholdRuleV1,
                    PredictionPresenceRuleV1,
                ),
            )
            else _PREDICTION_RULE_ADAPTER.validate_python(dict(rule))
        )
        result = self._client.predict_playbill(
            self._instance_id,
            request=PlaybillPredictRequestV1(
                prediction=cast(PredictionClaimPayloadV1, prediction.payload),
                procedure=procedure_name,
                measurement_name=measurement_name,
                observation=PredictionObservationSelectorV1(
                    subject=_subject_address(subject_name),
                    predicate=predicate_name,
                    qualifier=observation_qualifier,
                ),
                rule=typed_rule,
                deadline=deadline,
                outcome_class=outcome_class,
            ),
        )
        declaration = result.declaration
        return Prediction(
            self,
            prediction_id=declaration.prediction_id,
            intent_id=declaration.intent_id,
            proposal_id=declaration.proposal_id,
            predicted_claim_id=declaration.predicted_claim_id,
            declaration_digest=declaration.declaration_digest,
        )

    def settle(
        self,
        prediction: Prediction | str,
        *,
        observation: ClaimRef | str,
        terminal_run_id: str | None = None,
        terminal_record_digest: str | None = None,
    ) -> PredictionSettlement:
        """Settle a prediction from a later observation or retained terminal record."""

        if isinstance(prediction, Prediction):
            if prediction._playbill is not self:
                raise ValueError("prediction belongs to another Playbill connection")
            prediction_id = prediction.prediction_id
        else:
            prediction_id = prediction
        claim_id = _address(observation, RefKind.CLAIM)
        if isinstance(observation, ClaimRef):
            self._assert_coordinate(observation.coordinate)
        if (terminal_run_id is None) != (terminal_record_digest is None):
            raise ValueError(
                "terminal settlement requires both terminal_run_id and terminal_record_digest"
            )
        evidence = (
            ObservationSettlementEvidenceV1(claim_id=claim_id)
            if terminal_run_id is None
            else TerminalSettlementEvidenceV1(
                claim_id=claim_id,
                run_id=terminal_run_id,
                terminal_record_digest=cast(str, terminal_record_digest),
            )
        )
        result = self._client.settle_playbill_prediction(
            self._instance_id,
            prediction_id,
            request=api.PlaybillSettleRequestV1(evidence=evidence),
        )
        outcome = result.resolution.get("settlement_outcome")
        if not isinstance(outcome, bool):
            raise ValueError("prediction settlement response omitted its mechanical outcome")
        return PredictionSettlement(
            prediction_id=result.prediction_id,
            outcome=outcome,
            relation=result.relation,
        )

    def accept(self, proposal_id: str) -> api.PlaybillActivationReceipt:
        """Request durable acceptance without refreshing local reading surfaces.

        The daemon's publication, recovery and workspace-advertisement protocol
        is unchanged. This call performs no client floor export or block check.
        The returned accepted_coordinate identifies the result; this connection
        and existing World snapshots retain their prior read coordinates. Call
        refresh() explicitly to orient this connection to the current head.
        """

        return self._client.activate_playbill_proposal(self._instance_id, proposal_id)

    def refresh_workspace(
        self,
        *,
        at: AcceptedCoordinate | api.PlaybillAcceptedCoordinate,
    ) -> api.PlaybillFloorRefreshResult:
        """Materialize the configured floor at an explicit accepted coordinate.

        Reports the coordinate written, or a failed/not_configured status.
        Does not advance this connection's read coordinate or check agent-owned
        projection blocks; use block.sync() separately for that inspection.
        """

        coordinate = api.PlaybillAcceptedCoordinate.model_validate(at.model_dump(mode="json"))
        return refresh_workspace_floor(
            self._client, self._instance_id, workspace=self._workspace, at=coordinate
        )

    def activate(
        self,
        proposal_id: str,
        *,
        no_sync: bool = False,
    ) -> api.PlaybillWorkspaceActivationResult:
        """Activate one proposal and refresh this workspace's configured floor.

        Convenience path: accepts, exports the floor at the accepted coordinate,
        then checks blocks against the server's current head unless no_sync is
        set. Use accept() and refresh_workspace() to schedule maintenance
        separately. Neither path advances this connection's read coordinate.
        """

        return activate_with_workspace_refresh(
            self._client,
            self._instance_id,
            proposal_id,
            workspace=self._workspace,
            sync=not no_sync,
        )

    def refresh(self) -> SearchPage:
        page = self._search(
            mode="orient",
            query=None,
            kinds=("claim", "demand", "procedure"),
            statuses=(),
            at_active_coordinate=False,
        )
        self._coordinate = page.coordinate
        return page

    def file(self, path: str | Path) -> FileSelector:
        return self._sources.select(path)

    def subject(
        self,
        *,
        subject: str | SubjectRef,
        pins: Sequence[ArtifactPin],
        lifecycle: ArtifactLifecycle,
    ) -> SubjectDraft:
        address = _address(subject, RefKind.SUBJECT)
        if isinstance(subject, SubjectRef):
            self._assert_coordinate(subject.coordinate)
        kind, identifier = _subject_parts(address)
        shell = SubjectShell(
            identity=ArtifactIdentity(kind="Subject", name=address),
            subject_kind=kind,
            subject_id=identifier,
            pins=tuple(pins),
            lifecycle=lifecycle,
        )
        return SubjectDraft(
            self,
            SubjectAuthoringPayloadV1(subject=shell),
            (),
            _program_stamp(
                "subject",
                {"subject": shell.model_dump(mode="json")},
            ),
            DiagnosticSourceMap(()),
            shell,
        )

    def claim_type(
        self,
        *,
        predicate: str | ClaimTypeRef,
        subject_kinds: Sequence[str],
        object_kind: ClaimObjectKind | str,
        value_schema: dict[str, object] | None,
        object_subject_kinds: Sequence[str],
        cardinality: Cardinality | str,
        permitted_roles: Sequence[ClaimRole | str],
        referent_sensitivity: ReferentSensitivity | str,
        sources: Sequence[str | SourceRef],
        admission_policy: ClaimAdmissionPolicyV1,
        resolution_policy: ClaimResolutionPolicyV1,
        pins: Sequence[ArtifactPin],
        evidence_freshness: Duration | None,
        attestation_consequence_policy: ClaimAttestationConsequencePolicyV1 | None = None,
    ) -> ClaimTypeDraft:
        kind = _enum(object_kind, ClaimObjectKind, label="claim-type object kind")
        arity = _enum(cardinality, Cardinality, label="claim-type cardinality")
        sensitivity = _enum(
            referent_sensitivity, ReferentSensitivity, label="claim-type referent sensitivity"
        )
        roles = tuple(
            _enum(item, ClaimRole, label="claim-type permitted role") for item in permitted_roles
        )
        name = _address(predicate, RefKind.CLAIM_TYPE)
        if isinstance(predicate, ClaimTypeRef):
            self._assert_coordinate(predicate.coordinate)
        for source in sources:
            if isinstance(source, SourceRef):
                self._assert_coordinate(source.coordinate)
        source_ids = tuple(sorted({_address(item, RefKind.SOURCE) for item in sources}))
        rules = tuple(
            ClaimEvidenceAdmissionRuleV1(
                rule_id=f"source-{source_id}",
                claim_roles=tuple(sorted({role.value for role in roles})),
                capture_contract_digests=(
                    capture_contract_digest(foreign_source_capture_contract(source_id)).tagged,
                ),
                evidence_kinds=("self_asserted",),
                admission="direct",
                subject_binding="exact_claim_subject",
            )
            for source_id in source_ids
        )
        artifact_format: Literal[
            "playbill-claim-type-v1",
            "playbill-claim-type-v3",
            "playbill-claim-type-v4",
        ]
        if attestation_consequence_policy is not None:
            artifact_format = "playbill-claim-type-v4"
        elif evidence_freshness is not None:
            artifact_format = "playbill-claim-type-v3"
        else:
            artifact_format = "playbill-claim-type-v1"
        lifecycle = ArtifactLifecycle()
        if isinstance(predicate, ClaimTypeRef):
            predecessor = self._client.get_playbill_claim_type(
                self._instance_id,
                name,
                at=_api_coordinate(predicate.coordinate),
            )
            lifecycle = ArtifactLifecycle(predecessor_digest=predecessor.artifact_digest)
        definition = ClaimType(
            artifact_format=artifact_format,
            identity=ArtifactIdentity(kind="ClaimType", name=name),
            predicate=name,
            allowed_subject_kinds=tuple(subject_kinds),
            object_kind=kind.value,
            literal_schema=value_schema,
            allowed_object_subject_kinds=tuple(object_subject_kinds),
            cardinality=arity.value,
            permitted_roles=tuple(role.value for role in roles),
            referent_sensitivity=sensitivity.value,
            evidence_admission_policy=ClaimEvidenceAdmissionPolicyV1(rules=rules),
            admission_policy=admission_policy,
            resolution_policy=resolution_policy,
            pins=tuple(pins),
            lifecycle=lifecycle,
            evidence_freshness=(
                None
                if evidence_freshness is None
                else ClaimEvidenceFreshnessV1(
                    stale_after=ClaimFreshnessDurationV1(microseconds=evidence_freshness.value)
                )
            ),
            attestation_consequence_policy=attestation_consequence_policy,
        )
        return ClaimTypeDraft(self, definition)

    def world(self) -> World:
        """Read this instance's accepted vocabulary as typed objects.

        Strings are the one place the SDK gave away what it knows. The daemon
        already publishes the accepted ClaimTypes, so this reads them once and
        hands back a tree of kinds, predicates and admissible values, every ref
        stamped with this connection's orientation.

        This refreshes first, so a world is always built at the instance's
        current accepted coordinate rather than at whatever this connection last
        installed. No Subject is read here. The first Subject access of any kind
        reads every Subject of every kind in one list, because the served verb
        takes neither a kind filter nor a cursor; a world with a thousand
        Subjects therefore costs the vocabulary at `world()` and that one list
        the first time any Subject is named.
        """

        from cruxible_client.authoring.world import build_world

        self.refresh()
        listing = self._client.list_playbill_claim_types(
            self._instance_id,
            at=_api_coordinate(self.coordinate),
        )
        return build_world(
            self,
            coordinate=self.coordinate,
            claim_type_envelopes=tuple(view.envelope for view in listing.claim_types),
        )

    def changes(self, *, rationale: str | None = None) -> ChangeSetDraft:
        """Open one changeset that any mix of members can be authored into.

        `pb.claim(...)` still authors exactly one Claim. This is the same
        authoring surface for an intent that carries more than one: it lowers
        once, proposes once, and admits or refuses whole.
        """

        return ChangeSetDraft(self, rationale)

    def claim(
        self,
        *,
        subject: str | SubjectRef,
        predicate: str | ClaimTypeRef,
        value: CanonicalValue | SubjectRef | LiteralValue | ExactContent,
        role: ClaimRole | str,
        rationale: str,
        supported_by: EvidenceSelection | CaptureRef | None,
        copied_from: EvidenceSelection | CaptureRef | None,
        self_source: str | None,
        qualifier: str | None,
        effective_period: EffectivePeriod | None,
        revises: str | ClaimRef | None,
        dispositions: Mapping[str | ClaimRef, Disposition | str],
        subject_definition: SubjectDraft | None,
        claim_type_definition: ClaimTypeDraft | None,
    ) -> ClaimDraft:
        return self._claim_draft(
            sites=capture_keyword_sites("claim", stacklevel=1),
            subject=subject,
            predicate=predicate,
            value=value,
            role=role,
            rationale=rationale,
            supported_by=supported_by,
            copied_from=copied_from,
            self_source=self_source,
            qualifier=qualifier,
            effective_period=effective_period,
            revises=revises,
            dispositions=dispositions,
            subject_definition=subject_definition,
            claim_type_definition=claim_type_definition,
        )

    def _claim_draft(
        self,
        *,
        sites: dict[str, CallSite],
        subject: str | SubjectRef,
        predicate: str | ClaimTypeRef,
        value: CanonicalValue | SubjectRef | LiteralValue | ExactContent,
        role: ClaimRole | str,
        rationale: str,
        supported_by: EvidenceSelection | CaptureRef | None,
        copied_from: EvidenceSelection | CaptureRef | None,
        self_source: str | None,
        qualifier: str | None,
        effective_period: EffectivePeriod | None,
        revises: str | ClaimRef | None,
        dispositions: Mapping[str | ClaimRef, Disposition | str],
        subject_definition: SubjectDraft | None,
        claim_type_definition: ClaimTypeDraft | None,
        staged_object_kinds: Mapping[str, str] | None = None,
    ) -> ClaimDraft:
        """Build one authored Claim from decisions plus its caller's call sites.

        The call sites arrive as an argument so a Claim authored inside
        `pb.changes(...)` still points its diagnostics at the author's own
        keyword, not at the builder that forwarded it.
        """

        claim_role = _enum(role, ClaimRole, label="claim role")
        resolved_dispositions = {
            key: _enum(value, Disposition, label="claim disposition")
            for key, value in dispositions.items()
        }
        branches = tuple(item is not None for item in (supported_by, copied_from, self_source))
        if sum(branches) != 1:
            raise ValueError("exactly one of supported_by, copied_from, or self_source is required")
        subject_name = _address(subject, RefKind.SUBJECT)
        if isinstance(subject, str):
            # PC-HR moved accepted artifacts to .json without retiring the
            # pre-PC-HR .yaml authoring shorthand.
            if subject_name.endswith(".json"):
                subject_name = subject_name.removesuffix(".json")
            elif subject_name.endswith(".yaml"):
                subject_name = subject_name.removesuffix(".yaml")
        predicate_name = _address(predicate, RefKind.CLAIM_TYPE)
        statement_object: LiteralClaimObject | SubjectClaimObject | AuthoringExactContentObjectV1
        if isinstance(value, ExactContent):
            # Exact bytes name their own kind, so the only question left is
            # whether the predicate states one. Asking here turns a shape the
            # daemon would refuse into a refusal that names the predicate and
            # the kind it does state, before anything is written.
            object_kind = self._claim_type_object_kind(
                predicate_name=predicate_name,
                predicate=predicate,
                claim_type_definition=claim_type_definition,
                staged_object_kinds=staged_object_kinds,
            )
            if object_kind != "exact_content":
                raise ExactContentTypeError(
                    predicate=predicate_name,
                    object_kind=object_kind,
                )
            statement_object = AuthoringExactContentObjectV1(
                content_base64=base64.b64encode(value.content).decode("ascii")
            )
        elif isinstance(value, LiteralValue):
            # A typed literal already names the ClaimType that admits it, so it
            # answers the object kind without a read and refuses the wrong
            # predicate here rather than in the daemon's preflight.
            if value.predicate != predicate_name:
                raise LiteralValueTypeError(
                    minted_under=value.predicate,
                    passed_to=predicate_name,
                )
            self._assert_coordinate(value.coordinate)
            statement_object = LiteralClaimObject(value=normalize_canonical(value.value))
        elif isinstance(value, SubjectRef):
            self._assert_coordinate(value.coordinate)
            statement_object = SubjectClaimObject(address=_subject_address(value.address))
        elif isinstance(value, str) and _SUBJECT_RE.fullmatch(value):
            object_kind = self._claim_type_object_kind(
                predicate_name=predicate_name,
                predicate=predicate,
                claim_type_definition=claim_type_definition,
                staged_object_kinds=staged_object_kinds,
            )
            statement_object = (
                SubjectClaimObject(address=_subject_address(value))
                if object_kind == "subject"
                else LiteralClaimObject(value=value)
            )
        else:
            statement_object = LiteralClaimObject(value=normalize_canonical(value))
        source: Any
        if supported_by is not None:
            if isinstance(supported_by, CaptureRef):
                self._assert_coordinate(supported_by.coordinate)
                if supported_by.citation_role != "evidence":
                    raise ValueError(
                        "a CaptureRef minted from a copy or legacy citation cannot be "
                        "promoted to independent evidence; reuse it with copied_from"
                    )
                source = ExistingCaptureCitationSourceV1(capture_digest=supported_by.capture_digest)
            else:
                assert_independent_projection_evidence(
                    source_id=supported_by.source_id,
                    content=supported_by.content,
                    start_byte=supported_by.start_byte,
                    end_byte=supported_by.end_byte,
                )
                source = supported_by.observation()
            citation_role: Literal["evidence", "copy"] | None = "evidence"
        elif copied_from is not None:
            if isinstance(copied_from, CaptureRef):
                self._assert_coordinate(copied_from.coordinate)
                source = ExistingCaptureCitationSourceV1(capture_digest=copied_from.capture_digest)
            else:
                # A copy of projection bytes attests them into concrete exactly
                # as evidence would; the role changes nothing about the law.
                assert_independent_projection_evidence(
                    source_id=copied_from.source_id,
                    content=copied_from.content,
                    start_byte=copied_from.start_byte,
                    end_byte=copied_from.end_byte,
                )
                source = copied_from.observation()
            citation_role = "copy"
        else:
            assert self_source is not None
            source = SelfSourceBodyV1(
                content_base64=base64.b64encode(self_source.encode("utf-8")).decode("ascii")
            )
            citation_role = None
        sorted_dispositions = tuple(
            sorted(
                (
                    (_address(key, RefKind.CLAIM), value)
                    for key, value in resolved_dispositions.items()
                ),
                key=lambda item: item[0].encode("ascii"),
            )
        )
        payload_values = dict(
            statement=AuthoringClaimStatementV1(
                subject=_subject_address(subject_name),
                predicate=predicate_name,
                qualifier=qualifier,
                object=statement_object,
                role=claim_role.value,
                effective_from=(None if effective_period is None else effective_period.starts_at),
                effective_until=(None if effective_period is None else effective_period.ends_at),
            ),
            rationale=rationale,
            source=source,
            citation_role=citation_role,
            claim_ref=(None if revises is None else _address(revises, RefKind.CLAIM)),
            existing_claim_dispositions=tuple(
                AuthoringExistingClaimDispositionV1(
                    claim_id=claim_id, disposition=disposition.value
                )
                for claim_id, disposition in sorted_dispositions
            ),
            dependency_drafts=ClaimDependencyDraftsV1(
                subject=None if subject_definition is None else subject_definition.shell,
                claim_type=(
                    None if claim_type_definition is None else claim_type_definition.definition
                ),
            ),
        )
        payload = (
            ClaimAuthoringPayloadV3(**payload_values)
            if isinstance(source, ExistingCaptureCitationSourceV1)
            else ClaimAuthoringPayloadV2(**payload_values)
        )
        expectations: list[AuthoringReferenceExpectationV1 | None] = [
            _expectation(
                subject,
                expected=RefKind.SUBJECT,
                payload_path="statement.subject",
            ),
            _expectation(
                predicate,
                expected=RefKind.CLAIM_TYPE,
                payload_path="statement.predicate",
            ),
        ]
        if isinstance(value, SubjectRef):
            expectations.append(
                _expectation(
                    value,
                    expected=RefKind.SUBJECT,
                    payload_path="statement.object.address",
                )
            )
        if revises is not None:
            expectations.append(
                _expectation(revises, expected=RefKind.CLAIM, payload_path="claim_ref")
            )
        capture_ref = (
            supported_by
            if isinstance(supported_by, CaptureRef)
            else copied_from
            if isinstance(copied_from, CaptureRef)
            else None
        )
        if capture_ref is not None:
            expectations.append(
                AuthoringReferenceExpectationV1(
                    payload_path="source",
                    artifact_kind="Source",
                    address=capture_ref.contract_address,
                    minted_coordinate=capture_ref.coordinate,
                )
            )
        for index, (raw_key, _value) in enumerate(sorted_dispositions):
            original = next(key for key in dispositions if _address(key, RefKind.CLAIM) == raw_key)
            expectations.append(
                _expectation(
                    original,
                    expected=RefKind.CLAIM,
                    payload_path=f"existing_claim_dispositions[{index}].claim_id",
                )
            )
        emitted = {
            "subject": ("statement.subject",),
            "predicate": ("statement.predicate",),
            "value": (
                "statement.object",
                (
                    "statement.object.address"
                    if isinstance(statement_object, SubjectClaimObject)
                    else "statement.object.content_base64"
                    if isinstance(statement_object, AuthoringExactContentObjectV1)
                    else "statement.object.value"
                ),
            ),
            "role": ("statement.role",),
            "rationale": ("rationale",),
            "supported_by": ("source",),
            "copied_from": ("source",),
            "self_source": ("source",),
            "qualifier": ("statement.qualifier",),
            "effective_period": ("statement.effective_from", "statement.effective_until"),
            "revises": ("claim_ref",),
            "dispositions": ("existing_claim_dispositions",),
            "subject_definition": ("dependency_drafts.subject",),
            "claim_type_definition": ("dependency_drafts.claim_type",),
        }
        decisions = {
            "subject": subject_name,
            "predicate": predicate_name,
            "value": (
                statement_object.address.model_dump(mode="json")
                if isinstance(statement_object, SubjectClaimObject)
                # The program stamp records the decision, not the body: exact
                # content is already stored and digested by the daemon, and a
                # second copy of it here would put the same bytes in the stamp.
                else statement_object.content_base64
                if isinstance(statement_object, AuthoringExactContentObjectV1)
                else statement_object.value
            ),
            "role": claim_role.value,
            "rationale": rationale,
            "source_branch": (
                "supported_by"
                if supported_by is not None
                else "copied_from"
                if copied_from is not None
                else "self_source"
            ),
            "source_id": (
                supported_by.source_id
                if isinstance(supported_by, EvidenceSelection)
                else copied_from.source_id
                if isinstance(copied_from, EvidenceSelection)
                else None
            ),
            "capture_digest": None if capture_ref is None else capture_ref.capture_digest,
            "self_source": self_source,
            "qualifier": qualifier,
            "effective_period": (
                None
                if effective_period is None
                else {
                    "starts_at": format_datetime(effective_period.starts_at),
                    "ends_at": format_datetime(effective_period.ends_at),
                }
            ),
            "revises": None if revises is None else _address(revises, RefKind.CLAIM),
            "dispositions": {
                identity: disposition.value for identity, disposition in sorted_dispositions
            },
            "dependency_drafts": payload.dependency_drafts.model_dump(mode="json"),
        }
        return ClaimDraft(
            self,
            payload,
            _sorted_expectations(expectations),
            _program_stamp("claim", decisions),
            DiagnosticSourceMap(
                entries_for_keywords(builder="claim", emitted=emitted, sites=sites)
            ),
        )

    def _claim_type_object_kind(
        self,
        *,
        predicate_name: str,
        predicate: str | ClaimTypeRef,
        claim_type_definition: ClaimTypeDraft | None,
        staged_object_kinds: Mapping[str, str] | None = None,
    ) -> Literal["literal", "subject", "exact_content"]:
        """Resolve the exact ClaimType before interpreting an untyped object.

        Three answers a change set already holds, tried before the accepted
        coordinate is read: the definition this Claim carries as a dependency
        draft, the ref a same-set definition returned, and the definitions the
        same set staged under other names. Only then is there anything to ask
        the daemon, and in a first generation there is no coordinate to ask at.
        """

        if claim_type_definition is not None:
            return claim_type_definition.definition.object_kind
        if isinstance(predicate, PendingClaimTypeRef):
            if predicate.object_kind not in _CLAIM_TYPE_OBJECT_KINDS:
                return "literal"
            return cast(
                Literal["literal", "subject", "exact_content"],
                predicate.object_kind,
            )
        staged = (staged_object_kinds or {}).get(predicate_name)
        if staged in _CLAIM_TYPE_OBJECT_KINDS:
            return cast(Literal["literal", "subject", "exact_content"], staged)
        coordinate = (
            predicate.coordinate if isinstance(predicate, ClaimTypeRef) else self.coordinate
        )
        if isinstance(predicate, ClaimTypeRef):
            self._assert_coordinate(coordinate)
        view = self._client.get_playbill_claim_type(
            self._instance_id,
            predicate_name,
            at=_api_coordinate(coordinate),
        )
        object_kind = view.envelope.get("object_kind")
        if object_kind not in _CLAIM_TYPE_OBJECT_KINDS:
            # An envelope kind this client does not know is daemon/client skew,
            # not a caller mistake. Falling back to the literal shape keeps the
            # daemon's preflight the single authority: it answers with the typed
            # `playbill.claim.object_kind_mismatch` refusal and its repair,
            # instead of the SDK raising an untyped, repair-less ValueError.
            return "literal"
        return cast(Literal["literal", "subject", "exact_content"], object_kind)

    def retire_claim(
        self,
        claim: str | ClaimRef,
        *,
        reason: ClaimRetirementReason,
        mode: Literal["preflight", "submit"] = "preflight",
        effective_until: datetime | None = None,
        dependents: Sequence[ClaimRetireDependentV1] = (),
    ) -> api.PlaybillClaimRetireResponse:
        """Preflight or submit one attributed, dependency-closed Claim retirement."""

        claim_address = _address(claim, RefKind.CLAIM)
        coordinate = claim.coordinate if isinstance(claim, ClaimRef) else self.coordinate
        request = ClaimRetireRequestV1(
            mode=mode,
            claim_ref=claim_address,
            reason=reason,
            effective_until=effective_until,
            expected_coordinate=coordinate,
            dependents=tuple(dependents),
        )
        claim_id = claim_address.removeprefix("Claim:")
        try:
            result = self._client.retire_playbill_claim(
                self._instance_id,
                claim_id,
                request=request.model_dump(mode="json"),
            )
        except CoreError as original:
            if (
                isinstance(claim, ClaimRef)
                or mode != "submit"
                or getattr(original, "error_code", None) != _RETIRE_CLOSURE_MISMATCH_CODE
            ):
                raise
            try:
                history = self._client.playbill_claim_history(self._instance_id, claim_id)
                cached = self._retirement_submissions.get(claim_id)
                if cached is None:
                    submitted_request, submitted_operation_digest = (
                        self._retirement_submission_from_history(
                            claim_id=claim_id,
                            request=request,
                            entries=history.entries,
                        )
                    )
                else:
                    self._retirement_submissions.move_to_end(claim_id)
                    submitted_request, submitted_operation_digest = cached
                replay_request = request.model_copy(
                    update={"expected_coordinate": submitted_request.expected_coordinate}
                )
                if replay_request != submitted_request:
                    raise ValueError("retirement request differs from submitted operation")
                replayed = self._client.retire_playbill_claim(
                    self._instance_id,
                    claim_id,
                    request=replay_request.model_dump(mode="json"),
                )
            except (CoreError, KeyError, TypeError, ValueError):
                raise original from None
            if (
                getattr(replayed, "outcome", None) != "already_retired"
                or replayed.operation_digest != submitted_operation_digest
            ):
                raise original
            self._retirement_submissions.pop(claim_id, None)
            return replayed
        if mode == "submit" and getattr(result, "outcome", None) == "proposed":
            self._retirement_submissions[claim_id] = (request, result.operation_digest)
            self._retirement_submissions.move_to_end(claim_id)
            while len(self._retirement_submissions) > _RETIREMENT_SUBMISSION_CACHE_LIMIT:
                self._retirement_submissions.popitem(last=False)
        return result

    def _retirement_submission_from_history(
        self,
        *,
        claim_id: str,
        request: ClaimRetireRequestV1,
        entries: Sequence[Mapping[str, Any]],
    ) -> tuple[ClaimRetireRequestV1, str]:
        """Recover one accepted retirement's original request coordinate and digest."""

        retirement = next(
            (entry for entry in entries if entry.get("lifecycle_state") == "retired"),
            None,
        )
        if retirement is None:
            raise ValueError("accepted Claim history has no retirement")
        candidate_digest = retirement.get("candidate_digest")
        predecessor_digest = retirement.get("predecessor_digest")
        if not isinstance(candidate_digest, str) or not isinstance(predecessor_digest, str):
            raise ValueError("accepted retirement history lacks candidate evidence")

        proposals = self._client.list_playbill_proposals(self._instance_id, status="settled")
        matches = tuple(
            entry
            for entry in proposals.entries
            if entry.candidate_digest == candidate_digest and entry.terminal_reason == "accepted"
        )
        if len(matches) != 1:
            raise ValueError("accepted retirement candidate does not name one proposal")
        inspection = self._client.inspect_playbill_proposal(
            self._instance_id, matches[0].proposal_id
        )
        proposal = inspection.proposal
        admission = proposal.get("admission")
        candidate = proposal.get("candidate")
        if not isinstance(admission, Mapping) or not isinstance(candidate, Mapping):
            raise ValueError("accepted retirement proposal evidence is incomplete")
        if candidate.get("candidate_digest") != candidate_digest:
            raise ValueError("accepted retirement proposal candidate differs from history")

        law_evidence = candidate.get("law_evidence")
        if not isinstance(law_evidence, list) or not law_evidence:
            raise ValueError("accepted retirement candidate lacks law coordinates")
        coordinates = {
            json.dumps(item.get("evaluation_coordinate"), sort_keys=True, separators=(",", ":"))
            for item in law_evidence
            if isinstance(item, Mapping) and isinstance(item.get("evaluation_coordinate"), Mapping)
        }
        if len(coordinates) != 1:
            raise ValueError("accepted retirement candidate mixes law coordinates")
        coordinate_payload = json.loads(next(iter(coordinates)))
        coordinate_payload["tag"] = "playbill-accepted-coordinate-v1"
        coordinate = AcceptedCoordinate.model_validate(coordinate_payload)
        if admission.get("proposed_base_oid") != coordinate.git_oid:
            raise ValueError("accepted retirement proposal base differs from its law coordinate")

        actor_id = admission.get("actor_id")
        target_ref = admission.get("target_ref")
        if not isinstance(actor_id, str) or not isinstance(target_ref, str):
            raise ValueError("accepted retirement proposal lacks operation attribution")
        target_prefix = f"refs/proposals/{actor_id}/claim-retire-"
        if not target_ref.startswith(target_prefix):
            raise ValueError("accepted retirement proposal has another operation family")
        operation_digest = "sha256:" + target_ref.removeprefix(target_prefix)
        Sha256Value.from_tagged(operation_digest)
        root = ClaimRetireDependentV1(
            artifact_identity=ArtifactIdentity(kind="Claim", name=claim_id),
            predecessor_digest=predecessor_digest,
            reason=request.reason,
            effective_until=request.effective_until,
        )
        reproduced = typed_digest(
            Sha256Value,
            _CLAIM_RETIRE_OPERATION_DOMAIN,
            {
                "actor_principal_id": actor_id,
                "expected_accepted_coordinate": coordinate.model_dump(mode="json"),
                "root": root.model_dump(mode="json"),
                "dependents": [item.model_dump(mode="json") for item in request.dependents],
            },
        ).tagged
        if reproduced != operation_digest:
            raise ValueError("retirement request differs from accepted operation")
        return request.model_copy(update={"expected_coordinate": coordinate}), operation_digest

    def procedure(
        self,
        *,
        definition: ProcedureDefinitionV3 | ProcedureDefinitionV4,
        activation_policy: ActivationPolicy | str,
        retire: bool,
        acquisition_policy: str | None = None,
    ) -> ProcedureDraft:
        """Author one Procedure, optionally pinning the policy its reads obey.

        `acquisition_policy` names an accepted `SourceAcquisitionPolicy` by its
        semantic name; lowering resolves that name and declares the exact pin on
        the Procedure envelope. A direct run reads its policy from that pin, so
        two Procedures whose Source aliases happen to agree are governed
        separately, and accepting an unrelated policy cannot change what an
        already accepted Procedure does.
        """

        sites = capture_keyword_sites("procedure", stacklevel=1)
        policy = _enum(activation_policy, ActivationPolicy, label="procedure activation policy")
        # `source` is served only by the graph-v4 observation path: a v3 Source
        # node names no interface or implementation, so nothing can plan its
        # Provider occurrence. Keep it out of the v3 allow-list rather than
        # letting authoring succeed on a graph no run lane can admit.
        allowed = {"state_tap", "transform", "project", "guard", "repeat", "halt"}
        if isinstance(definition, ProcedureDefinitionV4):
            allowed = allowed | {"source"}
        unsupported = tuple(node.node_id for node in definition.nodes if node.kind not in allowed)
        if unsupported:
            raise CapabilityNotServed(
                code="playbill.sdk.procedure_capability_not_served",
                capability=f"procedure nodes {unsupported}",
                repair=(
                    "Use only state_tap, transform, project, guard, repeat, and halt nodes "
                    "on the served SDK lane, plus source on a graph-v4 definition."
                ),
            )
        payload = ProcedureAuthoringPayloadV2(
            definition=definition.model_dump(mode="json", by_alias=True),
            activation_policy=policy.value,
            owned_contracts=(),
            acquisition_policy=acquisition_policy,
            retire=retire,
        )
        return ProcedureDraft(
            self,
            payload,
            (),
            _program_stamp(
                "procedure",
                {
                    "definition": definition.model_dump(mode="json", by_alias=True),
                    "activation_policy": policy.value,
                    "acquisition_policy": acquisition_policy,
                    "retire": retire,
                },
            ),
            DiagnosticSourceMap(
                entries_for_keywords(
                    builder="procedure",
                    emitted={
                        "definition": ("definition",),
                        "activation_policy": ("activation_policy",),
                        "acquisition_policy": ("acquisition_policy",),
                        "retire": ("retire",),
                    },
                    sites=sites,
                )
            ),
        )

    def accepted_procedure(self, procedure: str | ProcedureRef) -> Procedure:
        name = _address(procedure, RefKind.PROCEDURE)
        if isinstance(procedure, ProcedureRef):
            self._assert_coordinate(procedure.coordinate)
        return Procedure(self, name, self.coordinate)

    def run_line(
        self,
        line_identity_digest: str,
        *,
        occurrence_id: str | None = None,
    ) -> ProcedureRun:
        """Trigger one daemon-derived occurrence of an accepted Line."""

        result = self._client.run_playbill_line(
            self._instance_id,
            line_identity_digest,
            occurrence_id=occurrence_id,
            evaluation_time=self._evaluation_time(),
        )
        return ProcedureRun(self, result)

    def get(self, ref: str | TypedRef) -> KnowledgeCard:
        if isinstance(ref, SubjectRef):
            kind, identifier = _subject_parts(ref.address)
            subject_view = self._client.get_playbill_subject(
                self._instance_id,
                kind,
                identifier,
                at=_api_coordinate(ref.coordinate),
            )
            return KnowledgeCard(
                RefKind.SUBJECT,
                ref.address,
                _coordinate(subject_view.coordinate),
                subject_view,
            )
        if isinstance(ref, ClaimTypeRef):
            claim_type_view = self._client.get_playbill_claim_type(
                self._instance_id, ref.address, at=_api_coordinate(ref.coordinate)
            )
            return KnowledgeCard(
                RefKind.CLAIM_TYPE,
                ref.address,
                _coordinate(claim_type_view.coordinate),
                claim_type_view,
            )
        if isinstance(ref, ClaimRef):
            claim_view = self._client.get_playbill_claim(
                self._instance_id,
                ref.address,
                at=_api_coordinate(ref.coordinate),
                evaluation_time=self._evaluation_time(),
            )
            return KnowledgeCard(
                RefKind.CLAIM,
                ref.address,
                _coordinate(claim_view.coordinate),
                claim_view,
            )
        if isinstance(ref, QueryRef):
            query_view = self._client.get_playbill_query_definition(
                self._instance_id, ref.address, at=_api_coordinate(ref.coordinate)
            )
            return KnowledgeCard(
                RefKind.QUERY,
                ref.address,
                _coordinate(query_view.coordinate),
                query_view,
            )
        if isinstance(ref, ProcedureRef):
            self._assert_coordinate(ref.coordinate)
            return KnowledgeCard(
                RefKind.PROCEDURE,
                ref.address,
                self.coordinate,
                self.search(query=ref.address, kinds=("procedure",), statuses=()),
            )
        if isinstance(ref, SourceRef):
            self._assert_coordinate(ref.coordinate)
            context = self._client.playbill_source_context(self._instance_id)
            matches = [item for item in context.documents if item.get("source_id") == ref.address]
            if len(matches) != 1:
                raise ValueError(f"source {ref.address!r} did not resolve uniquely")
            return KnowledgeCard(
                RefKind.SOURCE,
                ref.address,
                _coordinate(context.accepted_coordinate),
                matches[0],
            )
        if not isinstance(ref, str):
            raise ReferenceKindError("unsupported typed reference")
        page = self.search(
            query=ref,
            kinds=("claim", "procedure"),
            statuses=(),
        )
        exact = [
            row
            for row in page.rows
            if ref
            in {
                row.get("identity"),
                row.get("name"),
                str(row.get("identity", "")).removeprefix("Claim:"),
            }
        ]
        if len(exact) != 1:
            raise ValueError(f"literal reference {ref!r} resolved to {len(exact)} exact rows")
        row_kind = exact[0].get("kind")
        card_kind = RefKind.PROCEDURE if row_kind == "procedure" else RefKind.CLAIM
        identity = (
            str(exact[0].get("identity", ref)).removeprefix("Claim:")
            if card_kind is RefKind.CLAIM
            else ref
        )
        return KnowledgeCard(card_kind, identity, page.coordinate, exact[0])

    def search(
        self,
        *,
        query: str,
        kinds: Collection[str],
        statuses: Collection[str],
    ) -> SearchPage:
        return self._search(mode="search", query=query, kinds=kinds, statuses=statuses)

    def list(
        self,
        *,
        kinds: Collection[str],
        statuses: Collection[str],
    ) -> SearchPage:
        return self._search(mode="list", query=None, kinds=kinds, statuses=statuses)

    def orient(self) -> SearchPage:
        return self._search(
            mode="orient",
            query=None,
            kinds=("claim", "demand", "procedure"),
            statuses=(),
        )

    def _search(
        self,
        *,
        mode: Literal["search", "list", "orient"],
        query: str | None,
        kinds: Collection[str],
        statuses: Collection[str],
        subject: Mapping[str, object] | None = None,
        cursor: Mapping[str, object] | None = None,
        at_active_coordinate: bool = True,
    ) -> SearchPage:
        result = self._client.search_playbill(
            self._instance_id,
            mode=mode,
            query=query,
            kinds=tuple(kinds),
            statuses=tuple(statuses),
            subject=None if subject is None else dict(subject),
            cursor=None if cursor is None else dict(cursor),
            at=(
                None
                if self._coordinate is None or not at_active_coordinate
                else _api_coordinate(self.coordinate)
            ),
            evaluation_time=self._evaluation_time(),
        )
        return SearchPage(
            coordinate=_coordinate(result.coordinate),
            evaluation_time=result.evaluation_time,
            rows=tuple(cast(dict[str, object], row) for row in result.rows),
            result_digest=result.result_digest,
            cursor=cast(dict[str, object] | None, result.next_cursor),
            truncated=result.truncated,
            orientation=cast(dict[str, object] | None, result.orientation),
        )

    def explain(self, ref: str | TypedRef) -> object:
        if isinstance(ref, ClaimRef) or (isinstance(ref, str) and ref.startswith("CLM-")):
            identity = ref.address if isinstance(ref, ClaimRef) else ref
            return self._client.explain_playbill_claim(
                self._instance_id,
                identity,
                at=_api_coordinate(
                    ref.coordinate if isinstance(ref, ClaimRef) else self.coordinate
                ),
                evaluation_time=self._evaluation_time(),
            )
        if isinstance(ref, SubjectRef):
            return self._client.explain_playbill_subject(
                self._instance_id,
                subject=_subject_address(ref.address).model_dump(mode="json"),
                at=_api_coordinate(ref.coordinate),
            )
        raise ReferenceKindError("explain requires a ClaimRef or SubjectRef in G6")

    def _append_attestation(
        self,
        *,
        prepared: PreparedClaimAttestationRequestV1,
        signer: ClaimAttestationV2Signer,
    ) -> ClaimAttestationAppendResultV1:
        return append_prepared_claim_attestation(
            self._client,
            self._instance_id,
            prepared=prepared,
            signer=signer,
        )

    def attest(
        self,
        claim: ClaimRef | str,
        *,
        stance: ClaimStance,
        signer: ClaimAttestationV2Signer,
        note: str | None = None,
        valid_until: datetime | None = None,
    ) -> ClaimAttestationAppendResultV1:
        """Sign that the caller examined the current exact Claim and append it once."""

        identity = claim.address if isinstance(claim, ClaimRef) else claim
        return self._append_attestation(
            prepared=PreparedClaimAttestationRequestV1(
                claim_id=identity.removeprefix("Claim:"),
                attestation_basis="examined_existing",
                stance=stance,
                referent_coordinate=claim.coordinate if isinstance(claim, ClaimRef) else None,
                attested_at=datetime.fromisoformat(self._evaluation_time()),
                valid_until=valid_until,
                note=note,
            ),
            signer=signer,
        )

    def attest_new_capture(
        self,
        request: PreparedClaimAttestationRequestV1,
        *,
        signer: ClaimAttestationV2Signer,
    ) -> ClaimAttestationAppendResultV1:
        """Append a pre-staged new-Capture observation after exact client signing."""

        if request.attestation_basis != "new_capture":
            raise ValueError("attest_new_capture requires attestation_basis='new_capture'")
        return self._append_attestation(prepared=request, signer=signer)

    def next(self, *, expiring_within: Duration) -> NextPage:
        requested_coordinate = _api_coordinate(self.coordinate)
        access_profile = self._access_profile.model_dump()
        observation, scanned_coordinate = observe_playbill_next_workspace_with_coverage(
            self._client,
            self._instance_id,
            self._workspace,
            observation=observe_playbill_next_workspace(self._workspace),
            coordinate=requested_coordinate,
            access_profile=access_profile,
        )
        result = self._client.next_playbill(
            self._instance_id,
            evaluation_time=self._evaluation_time(),
            access_profile=access_profile,
            at=scanned_coordinate or requested_coordinate,
            expiring_within=expiring_within.model_dump(),
            workspace_observation=observation,
        )
        return NextPage(
            coordinate=_coordinate(result.coordinate),
            evaluation_time=result.evaluation_time,
            items=tuple(cast(dict[str, object], item) for item in result.items),
            result_digest=result.result_digest,
            observed_domains=tuple(result.observed_domains),
            unobserved_domains=tuple(result.unobserved_domains),
            attestation_head_digest=result.attestation_head_digest,
        )

    def since(
        self,
        generation: int,
        *,
        max_rows: int = 100,
        max_bytes: int = 65_536,
        cursor: api.PlaybillSinceCursor | Mapping[str, object] | None = None,
    ) -> api.PlaybillSinceResult:
        """Read accepted ChangeSet members after one generation at this orientation."""

        return self._client.since_playbill(
            self._instance_id,
            generation=generation,
            access_profile=self._access_profile.model_dump(),
            at=None if cursor is not None else _api_coordinate(self.coordinate),
            max_rows=max_rows,
            max_bytes=max_bytes,
            cursor=cursor,
        )

    def curation_list(self) -> api.PlaybillCurationListResult:
        """Read the curation queue with one explicit attributed workspace scan."""

        access_profile = self._access_profile.model_dump()
        observation, _coordinate = observe_playbill_next_workspace_with_coverage(
            self._client,
            self._instance_id,
            self._workspace,
            observation=observe_playbill_next_workspace(self._workspace),
            access_profile=access_profile,
        )
        return self._client.list_playbill_curation(
            self._instance_id,
            evaluation_time=self._evaluation_time(),
            access_profile=access_profile,
            workspace_observation=observation,
        )

    def audit(
        self,
        *,
        claim_type_identities: tuple[str, ...] = (),
        subject_kinds: tuple[str, ...] = (),
        max_rows: int = 100,
        max_bytes: int = 65_536,
        cursor: api.PlaybillAuditCursor | Mapping[str, object] | None = None,
    ) -> api.PlaybillAuditResult:
        """Rank visible Claim verification work without changing governed state."""

        return self._client.audit_playbill(
            self._instance_id,
            evaluation_time=self._evaluation_time(),
            access_profile=self._access_profile.model_dump(),
            at=None if cursor is not None else _api_coordinate(self.coordinate),
            claim_type_identities=claim_type_identities,
            subject_kinds=subject_kinds,
            max_rows=max_rows,
            max_bytes=max_bytes,
            cursor=cursor,
        )

    def curation_overrule(
        self,
        *,
        item_id: str,
        expected_latest_event_digest: str,
        reason: str,
        attribution_refs: tuple[str, ...] = (),
    ) -> api.PlaybillCurationActionResult:
        """Record that a detector pattern is mechanically inapplicable."""

        return self._client.overrule_playbill_curation(
            self._instance_id,
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
            attribution_refs=attribution_refs,
        )

    def curation_accept_fixed(
        self,
        *,
        item_id: str,
        expected_latest_event_digest: str,
        reason: str,
        accepted_proposal_id: str,
        accepted_changeset_digest: str,
        attribution_refs: tuple[str, ...] = (),
    ) -> api.PlaybillCurationActionResult:
        """Link an item to an exact already-accepted resolving ChangeSet."""

        return self._client.accept_fixed_playbill_curation(
            self._instance_id,
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
            accepted_proposal_id=accepted_proposal_id,
            accepted_changeset_digest=accepted_changeset_digest,
            attribution_refs=attribution_refs,
        )

    def curation_suppress(
        self,
        *,
        item_id: str,
        expected_latest_event_digest: str,
        reason: str,
        scope: Literal["item", "pattern", "instance"],
        until_generation: int | None = None,
        attribution_refs: tuple[str, ...] = (),
    ) -> api.PlaybillCurationActionResult:
        """Hide matching open work without resolving or stopping detection."""

        return self._client.suppress_playbill_curation(
            self._instance_id,
            item_id=item_id,
            expected_latest_event_digest=expected_latest_event_digest,
            reason=reason,
            scope=scope,
            until_generation=until_generation,
            attribution_refs=attribution_refs,
        )

    def _assert_coordinate(self, coordinate: AcceptedCoordinate) -> None:
        if coordinate != self.coordinate:
            raise ValueError(
                "typed reference coordinate differs from the active orientation; refresh or "
                "use the reference in authoring so the daemon can report its successor"
            )

    def _evaluation_time(self) -> str:
        return cast(str, format_datetime(self._clock()))


class ProjectionBlocks:
    def __init__(self, playbill: Playbill) -> None:
        self._playbill = playbill

    def repin(
        self,
        source: str | SourceRef,
        block_id: str,
        *,
        claims: Sequence[str | ClaimRef] = (),
        queries: Sequence[
            str | QueryRef | tuple[str | QueryRef, Mapping[str, CanonicalValue]]
        ] = (),
        backing_digest: str | None = None,
        evaluation_time: datetime,
    ) -> ProjectionBlockStampV1:
        source_id = _address(source, RefKind.SOURCE)
        if isinstance(source, SourceRef):
            self._playbill._assert_coordinate(source.coordinate)
        claim_refs: list[str] = []
        for claim in claims:
            if isinstance(claim, ClaimRef):
                self._playbill._assert_coordinate(claim.coordinate)
            claim_refs.append(_address(claim, RefKind.CLAIM))
        query_refs: list[tuple[str, Mapping[str, object]]] = []
        for entry in queries:
            if isinstance(entry, tuple):
                query, parameters = entry
            else:
                query, parameters = entry, {}
            if isinstance(query, QueryRef):
                self._playbill._assert_coordinate(query.coordinate)
            query_refs.append((_address(query, RefKind.QUERY), parameters))
        return repin_projection_block(
            self._playbill._client,
            self._playbill._instance_id,
            workspace=self._playbill._workspace,
            source_id=source_id,
            block_id=block_id,
            claims=claim_refs,
            queries=query_refs,
            backing_digest=backing_digest,
            evaluation_time=evaluation_time,
            coordinate=self._playbill.coordinate,
        )

    def sync(
        self,
        *paths: str | Path,
        all: bool = False,
        check: bool = False,
        detach: Sequence[str | Path] = (),
        accept_local: Sequence[str | Path] = (),
        discard_local: Sequence[str | Path] | None = None,
    ) -> api.PlaybillBlockSyncResultV1:
        """Report every declared block; `accept_local` re-stamps one on its local prose.

        `discard_local` is the deprecated spelling of `accept_local`. It never
        discarded anything under the held-list model, and it is accepted for one
        release behind a `DeprecationWarning`.
        """

        if discard_local is not None:
            warnings.warn(
                _BLOCK_SYNC_DISCARD_LOCAL_DEPRECATION,
                DeprecationWarning,
                stacklevel=2,
            )
            accept_local = tuple(accept_local) + tuple(discard_local)
        return sync_projection_blocks(
            self._playbill._client,
            self._playbill._instance_id,
            workspace=self._playbill._workspace,
            paths=paths,
            all_sources=all,
            check=check,
            detach_paths=detach,
            accept_local_paths=accept_local,
        )


class Procedure:
    def __init__(self, playbill: Playbill, name: str, coordinate: AcceptedCoordinate) -> None:
        self._playbill = playbill
        self._name = name
        self._coordinate = coordinate

    @property
    def ref(self) -> ProcedureRef:
        return ProcedureRef(self._name, self._coordinate)

    def readiness(self) -> api.PlaybillProcedureReadiness:
        return self._playbill._client.playbill_procedure_readiness(
            self._playbill._instance_id,
            self._name,
            evaluation_time=self._playbill._evaluation_time(),
            at=_api_coordinate(self._coordinate),
        )

    def bind(
        self, *, bindings: Mapping[str | SlotRef, TypedRef]
    ) -> api.PlaybillProcedureBindResult:
        self._playbill._assert_coordinate(self._coordinate)
        rows: list[dict[str, object]] = []
        for key, value in bindings.items():
            slot = key if isinstance(key, str) else _address(key, RefKind.SLOT)
            if isinstance(value, SlotRef):
                raise ReferenceKindError("a slot cannot be bound to another slot")
            self._playbill._assert_coordinate(value.coordinate)
            target_kind = _REFERENCE_KINDS.get(value.kind)
            if target_kind is None:
                raise ReferenceKindError(f"cannot bind {value.kind.value} to a procedure slot")
            rows.append(
                {
                    "slot_name": slot,
                    "target": {"kind": target_kind, "name": value.address},
                }
            )
        rows.sort(key=lambda item: str(item["slot_name"]).encode("utf-8"))
        result = self._playbill._client.bind_playbill_procedure(
            self._playbill._instance_id, self._name, bindings=rows
        )
        return result

    def run(
        self,
        *,
        at: AcceptedCoordinate | None = None,
        **inputs: CanonicalValue,
    ) -> ProcedureRun:
        self._playbill._assert_coordinate(self._coordinate)
        normalized = normalize_canonical(inputs)
        result = self._playbill._client.run_playbill_procedure(
            self._playbill._instance_id,
            self._name,
            evaluation_time=self._playbill._evaluation_time(),
            at=None if at is None else _api_coordinate(at),
            input=normalized,
        )
        return ProcedureRun(self._playbill, result)


class ProcedureRun:
    def __init__(self, playbill: Playbill, raw: api.PlaybillProcedureRunState) -> None:
        self._playbill = playbill
        self._raw = raw

    @property
    def run_id(self) -> str | None:
        return self._raw.run_id

    @property
    def status(self) -> str:
        return self._raw.status

    @property
    def result(self) -> CanonicalValue:
        return cast(CanonicalValue, self._raw.result)

    @property
    def receipt(self) -> str | None:
        return self._raw.receipt_digest

    @property
    def coordinate(self) -> AcceptedCoordinate:
        return _coordinate(self._raw.coordinate)

    @property
    def track_record(self) -> object:
        result = self._playbill._client.search_playbill(
            self._playbill._instance_id,
            mode="search",
            query=str(self._raw.procedure_identity.get("name", "")),
            kinds=("procedure",),
            statuses=(),
            at=self._raw.coordinate,
            evaluation_time=self._raw.evaluation_time,
        )
        matches = [
            row
            for row in result.rows
            if row.get("identity") == self._raw.procedure_identity.get("qualified")
            or row.get("name") == self._raw.procedure_identity.get("name")
        ]
        return matches[0].get("track_record") if len(matches) == 1 else None

    def refresh(self) -> ProcedureRun:
        if self.run_id is None:
            return self
        self._raw = self._playbill._client.get_playbill_procedure_run(
            self._playbill._instance_id, self.run_id
        )
        return self


__all__ = [
    "ChangeSetDraft",
    "ClaimDraft",
    "ClaimTypeDraft",
    "Intent",
    "KnowledgeCard",
    "NextPage",
    "Playbill",
    "Prediction",
    "PredictionSettlement",
    "Procedure",
    "ProcedureDraft",
    "ProcedureRun",
    "Proposal",
    "Publication",
    "SDK_CONTRACT_SNAPSHOT_DIGEST",
    "SearchPage",
    "SubjectDraft",
    "carry",
    "re_author",
    "rescind",
    "retire",
]
