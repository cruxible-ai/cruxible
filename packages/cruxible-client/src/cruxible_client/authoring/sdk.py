"""Synchronous, agent-oriented authoring facade over the Playbill wire ISA."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from collections import OrderedDict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Literal, TypeVar, cast

from pydantic import SecretStr, TypeAdapter

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
from cruxible_client.authoring.insertions import (
    apply_playbill_publication,
    replace_publication_file,
)
from cruxible_client.authoring.sdk_types import (
    AccessProfile,
    ActivationPolicy,
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
    ProcedureRef,
    QueryRef,
    ReferenceKindError,
    ReferentSensitivity,
    RefKind,
    SlotRef,
    SourceRef,
    SubjectRef,
    TypedRef,
)
from cruxible_client.authoring.selectors import (
    EvidenceSelection,
    FileSelector,
    InsertionSelection,
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
)
from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_client.contracts.authoring.models import (
    AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST,
    AUTHORING_SDK_VERSION,
    AuthoringClaimStatementV1,
    AuthoringExistingClaimDispositionV1,
    AuthoringProgramOperationV1,
    AuthoringProgramStampV1,
    AuthoringReferenceExpectationV1,
    ClaimAuthoringPayloadV1,
    ClaimAuthoringPayloadV2,
    ClaimAuthoringPayloadV3,
    ClaimDependencyDraftsV1,
    ExistingCaptureCitationSourceV1,
    ProcedureAuthoringPayloadV2,
    PublicationSourceObservationV2,
    SelfSourceBodyV1,
    SubjectAuthoringPayloadV1,
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
from cruxible_client.contracts.procedures.models import ProcedureDefinitionV3
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import SubjectShell
from cruxible_client.contracts.temporal import format_datetime
from cruxible_client.errors import CoreError
from cruxible_client.transport.http import CruxibleClient

SDK_CONTRACT_SNAPSHOT_DIGEST = AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST

_SUBJECT_RE = re.compile(
    r"^(?P<kind>[a-z][a-z0-9_]{0,63}(?:\.[a-z][a-z0-9_]{0,63})*)/"
    r"(?P<identifier>[a-z][a-z0-9_.-]{0,255})$"
)
_CLAIM_ADAPTER: TypeAdapter[ClaimArtifactAny] = TypeAdapter(ClaimArtifactAny)
_RETIRE_CLOSURE_MISMATCH_CODE = "playbill.claim.retire_closure_mismatch"
_CLAIM_RETIRE_OPERATION_DOMAIN = "playbill-claim-retire-operation-v1"
_RETIREMENT_SUBMISSION_CACHE_LIMIT = 128


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
class ProcedureDraft(_IntentDraft):
    pass


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
        expectation = self._raw.get("insertion_expectation")
        if not isinstance(expectation, Mapping):
            self._raw = self._playbill._client.get_playbill_authoring_intent(
                self._playbill._instance_id, self.intent_id
            ).intent
            expectation = self._raw.get("insertion_expectation")
        if not isinstance(expectation, Mapping):
            return None
        return Publication(self, dict(expectation))

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
    def __init__(self, intent: Intent, expectation: dict[str, object]) -> None:
        self._intent = intent
        self._expectation = expectation

    @property
    def state(self) -> str:
        return str(self._expectation.get("state", "terminal"))

    def _path(self) -> Path:
        target = self._expectation.get("target")
        patch = self._expectation.get("patch")
        source = target if isinstance(target, Mapping) else patch
        if not isinstance(source, Mapping) or not isinstance(source.get("source_id"), str):
            raise ValueError("insertion expectation omitted its source")
        return self._intent._playbill._sources.path_for_source(str(source["source_id"]))

    def prepare(self) -> Publication:
        path = self._path()
        content = path.read_bytes()
        target = cast(Mapping[str, object], self._expectation["target"])
        observation = PublicationSourceObservationV2(
            source_id=cast(str, target["source_id"]),
            content_base64=base64.b64encode(content).decode("ascii"),
            content_digest="sha256:" + hashlib.sha256(content).hexdigest(),
            byte_length=len(content),
        )
        result = self._intent._playbill._client.prepare_playbill_authoring_publication(
            self._intent._playbill._instance_id,
            self._intent.intent_id,
            observation=observation.model_dump(mode="json"),
        )
        self._intent._raw = result.intent
        self._expectation = result.expectation
        return self

    def apply(self) -> Publication:
        self.prepare()
        if self.state == "bound":
            return self
        if self.state != "prepared":
            raise ValueError(f"publication cannot apply from state {self.state!r}")
        path = self._path()
        payload = self._intent._draft.payload
        if not isinstance(
            payload, ClaimAuthoringPayloadV2 | ClaimAuthoringPayloadV3
        ) or not isinstance(payload.source, SelfSourceBodyV1):
            raise ValueError("publication requires a retained self-source body")
        expected = path.read_bytes()
        application = apply_playbill_publication(
            expected,
            intent_id=self._intent.intent_id,
            expectation=self._expectation,
            retained_body=payload.source.content,
        )
        if application.outcome == "applied":
            replace_publication_file(
                path,
                expected=expected,
                replacement=application.content,
            )
        result = self._intent._playbill._client.confirm_playbill_authoring_insertion(
            self._intent._playbill._instance_id,
            self._intent.intent_id,
            observation=application.observation,
        )
        self._intent._raw = result.intent
        self._expectation = result.expectation
        return self

    def status(self) -> str:
        self._intent._raw = self._intent._playbill._client.get_playbill_authoring_intent(
            self._intent._playbill._instance_id, self._intent.intent_id
        ).intent
        expectation = self._intent._raw.get("insertion_expectation")
        if isinstance(expectation, Mapping):
            self._expectation = dict(expectation)
        return self.state

    def abandon(self) -> Publication:
        result = self._intent._playbill._client.abandon_playbill_authoring_insertion(
            self._intent._playbill._instance_id, self._intent.intent_id
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
        self._sources = WorkspaceSources(self._workspace)
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
            from cruxible_client.compatibility import check_daemon_compatibility

            check_daemon_compatibility(client)
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

    def activate(
        self,
        proposal_id: str,
        *,
        no_sync: bool = False,
    ) -> api.PlaybillWorkspaceActivationResult:
        """Activate one proposal and refresh this workspace's configured floor.

        Activation was the one step of the authoring loop `Playbill` did not
        carry: callers had to build a second `CruxibleClient` and reach for
        `activate_with_workspace_refresh` themselves, passing the workspace path
        they had already given `connect`.
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

    def claim(
        self,
        *,
        subject: str | SubjectRef,
        predicate: str | ClaimTypeRef,
        value: CanonicalValue | SubjectRef,
        role: ClaimRole | str,
        rationale: str,
        supported_by: EvidenceSelection | CaptureRef | None,
        copied_from: EvidenceSelection | CaptureRef | None,
        self_source: str | None,
        qualifier: str | None,
        effective_period: EffectivePeriod | None,
        revises: str | ClaimRef | None,
        dispositions: Mapping[str | ClaimRef, Disposition | str],
        publish_to: InsertionSelection | None,
        subject_definition: SubjectDraft | None,
        claim_type_definition: ClaimTypeDraft | None,
    ) -> ClaimDraft:
        sites = capture_keyword_sites("claim", stacklevel=1)
        claim_role = _enum(role, ClaimRole, label="claim role")
        resolved_dispositions = {
            key: _enum(value, Disposition, label="claim disposition")
            for key, value in dispositions.items()
        }
        branches = tuple(item is not None for item in (supported_by, copied_from, self_source))
        if sum(branches) != 1:
            raise ValueError("exactly one of supported_by, copied_from, or self_source is required")
        if publish_to is not None and self_source is None:
            raise ValueError("publish_to is legal only for self_source claims")
        subject_name = _address(subject, RefKind.SUBJECT)
        if isinstance(subject, str):
            # PC-HR moved accepted artifacts to .json without retiring the
            # pre-PC-HR .yaml authoring shorthand.
            if subject_name.endswith(".json"):
                subject_name = subject_name.removesuffix(".json")
            elif subject_name.endswith(".yaml"):
                subject_name = subject_name.removesuffix(".yaml")
        predicate_name = _address(predicate, RefKind.CLAIM_TYPE)
        if isinstance(value, SubjectRef):
            self._assert_coordinate(value.coordinate)
            statement_object: LiteralClaimObject | SubjectClaimObject = SubjectClaimObject(
                address=_subject_address(value.address)
            )
        elif isinstance(value, str) and _SUBJECT_RE.fullmatch(value):
            statement_object = SubjectClaimObject(address=_subject_address(value))
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
            insertion_target=(
                None
                if publish_to is None
                else publish_to.target(cast(str, self_source).encode("utf-8"))
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
            "publish_to": ("insertion_target",),
            "subject_definition": ("dependency_drafts.subject",),
            "claim_type_definition": ("dependency_drafts.claim_type",),
        }
        decisions = {
            "subject": subject_name,
            "predicate": predicate_name,
            "value": (
                statement_object.address.model_dump(mode="json")
                if isinstance(statement_object, SubjectClaimObject)
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
            "publication": (
                None
                if publish_to is None
                else {
                    "source_id": publish_to.source_id,
                    "operation": publish_to.operation.value,
                    "anchor": publish_to.anchor_text,
                }
            ),
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
        definition: ProcedureDefinitionV3,
        activation_policy: ActivationPolicy | str,
        retire: bool,
    ) -> ProcedureDraft:
        sites = capture_keyword_sites("procedure", stacklevel=1)
        policy = _enum(activation_policy, ActivationPolicy, label="procedure activation policy")
        allowed = {"state_tap", "transform", "project", "guard", "repeat", "halt"}
        unsupported = tuple(node.node_id for node in definition.nodes if node.kind not in allowed)
        if unsupported:
            raise CapabilityNotServed(
                code="playbill.sdk.procedure_capability_not_served",
                capability=f"procedure nodes {unsupported}",
                repair=(
                    "Use only state_tap, transform, project, guard, repeat, and halt nodes "
                    "on the served SDK lane."
                ),
            )
        payload = ProcedureAuthoringPayloadV2(
            definition=definition.model_dump(mode="json", by_alias=True),
            activation_policy=policy.value,
            owned_contracts=(),
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
                    "retire": retire,
                },
            ),
            DiagnosticSourceMap(
                entries_for_keywords(
                    builder="procedure",
                    emitted={
                        "definition": ("definition",),
                        "activation_policy": ("activation_policy",),
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
        at_active_coordinate: bool = True,
    ) -> SearchPage:
        result = self._client.search_playbill(
            self._instance_id,
            mode=mode,
            query=query,
            kinds=tuple(kinds),
            statuses=tuple(statuses),
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
        discard_local: Sequence[str | Path] = (),
    ) -> api.PlaybillBlockSyncResultV1:
        return sync_projection_blocks(
            self._playbill._client,
            self._playbill._instance_id,
            workspace=self._playbill._workspace,
            paths=paths,
            all_sources=all,
            check=check,
            detach_paths=detach,
            discard_local_paths=discard_local,
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
    "ClaimDraft",
    "ClaimTypeDraft",
    "Intent",
    "KnowledgeCard",
    "NextPage",
    "Playbill",
    "Procedure",
    "ProcedureDraft",
    "ProcedureRun",
    "Proposal",
    "Publication",
    "SDK_CONTRACT_SNAPSHOT_DIGEST",
    "SearchPage",
    "SubjectDraft",
]
