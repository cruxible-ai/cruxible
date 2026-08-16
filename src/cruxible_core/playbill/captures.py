"""Playbill-native Capture contracts and the bounded direct-authoring path."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from cruxible_core.playbill.artifacts import (
    ArtifactAuthority,
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_core.playbill.canonical import (
    ArtifactDigest,
    CasDigest,
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
    typed_digest,
)
from cruxible_core.playbill.cas import (
    BodyAccessContext,
    CasObjectMetadata,
)
from cruxible_core.playbill.diagnostics import CompilerDiagnostic
from cruxible_core.playbill.errors import PlaybillCasError, PlaybillFormatError
from cruxible_core.playbill.governance import PermissionTier, governance_identifier
from cruxible_core.playbill.semantic import ContentSpan, SemanticAddress
from cruxible_core.playbill.source_references import (
    CasSourceReferenceV1,
    EvidenceCommitmentV1,
    ExternalSourceReferenceV1,
    SourceReferenceV1,
    validate_source_commitment,
)

if TYPE_CHECKING:
    from cruxible_core.playbill.projection import AcceptedCoordinate

_CONTRACT_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
_RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,255}$")

DIRECT_SELF_ASSERTED_CONTRACT_ID = "playbill.direct-self-asserted-v1"
DIRECT_SOURCE_IDENTITY = "playbill.direct-authoring"
DIRECT_EXTERNAL_COORDINATE_TYPE = "direct-claim-external-coordinate-v1"
DIRECT_EXTERNAL_SELECTOR_TYPE = "direct-claim-external-selector-v1"
DIRECT_EXTERNAL_COORDINATE_TYPES = (
    "api-revision-v1",
    "cdc-position-v1",
    "database-snapshot-v1",
    "object-version-v1",
    "postgres-lsn-v1",
    "transaction-id-v1",
)
DIRECT_EXTERNAL_SELECTOR_TYPES = (
    "cdc-change-v1",
    "query-result-v1",
    "relation-primary-key-v1",
    "resource-key-v1",
)


class CaptureFormatError(PlaybillFormatError):
    """A Capture contract/envelope or its canonical location is invalid."""


class _StrictCaptureModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _sorted_unique(
    value: tuple[str, ...],
    *,
    label: str,
    nonempty: bool = True,
) -> tuple[str, ...]:
    if nonempty and not value:
        raise ValueError(f"{label} must not be empty")
    if value != tuple(sorted(set(value), key=lambda item: item.encode("utf-8"))):
        raise ValueError(f"{label} must be sorted and unique")
    return value


class CanonicalDurationV1(_StrictCaptureModel):
    tag: Literal["playbill-duration-v1"] = "playbill-duration-v1"
    microseconds: int = Field(ge=0)


class CaptureSelectionBudgetV1(_StrictCaptureModel):
    tag: Literal["playbill-capture-selection-budget-v1"] = "playbill-capture-selection-budget-v1"
    max_bytes: int = Field(ge=1)
    max_rows: int = Field(ge=1)
    max_items: int = Field(ge=1)


class CaptureRetentionErasurePolicyV1(_StrictCaptureModel):
    tag: Literal["playbill-capture-retention-erasure-policy-v1"] = (
        "playbill-capture-retention-erasure-policy-v1"
    )
    body_retention: Literal[
        "never_materialize",
        "optional",
        "required_for_duration",
    ]
    minimum_retention: CanonicalDurationV1 | None = None
    erasure: Literal["prohibited", "authorized_by_rule"]
    erasure_rule_digest: str | None = None
    selector_privacy: Literal["direct_allowed", "pseudonymous_required"]

    @field_validator("erasure_rule_digest")
    @classmethod
    def _erasure_digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _policy_shape(self) -> "CaptureRetentionErasurePolicyV1":
        required = self.body_retention == "required_for_duration"
        if required != (self.minimum_retention is not None):
            raise ValueError("required_for_duration requires exactly one minimum_retention")
        if required and self.minimum_retention is not None:
            if self.minimum_retention.microseconds == 0:
                raise ValueError("required body retention duration must be nonzero")
        authorized = self.erasure == "authorized_by_rule"
        if authorized != (self.erasure_rule_digest is not None):
            raise ValueError(
                "authorized erasure requires exactly one registered erasure-rule digest"
            )
        return self


def _pin_key(pin: ArtifactPin) -> tuple[bytes, bytes]:
    return pin.role.encode("utf-8"), pin.target.qualified.encode("utf-8")


class CaptureContractV1(_StrictCaptureModel):
    artifact_format: Literal["playbill-capture-contract-v1"] = "playbill-capture-contract-v1"
    identity: ArtifactIdentity
    allowed_source_kinds: tuple[Literal["ledger", "cas", "external"], ...]
    logical_source_identities: tuple[str, ...]
    coordinate_schema_pins: tuple[ArtifactPin, ...]
    selector_schema_pins: tuple[ArtifactPin, ...]
    commitment_canonicalizer: ArtifactPin
    allowed_materialization_modes: tuple[Literal["ledger", "cas", "external", "none"], ...]
    selection_budget: CaptureSelectionBudgetV1
    retention_erasure_policy: CaptureRetentionErasurePolicyV1
    replay_policy_digest: str
    epistemic_grade: Literal["observed", "derived", "predicted"]
    provenance_rule_digest: str
    evidence_kinds: tuple[str, ...]
    source_subject_mapping_digest: str
    authority: ArtifactAuthority
    pins: tuple[ArtifactPin, ...] = ()
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()

    @field_validator("allowed_source_kinds", "allowed_materialization_modes")
    @classmethod
    def _enum_sets(cls, value: tuple[str, ...], info: object) -> tuple[str, ...]:
        return _sorted_unique(value, label=str(getattr(info, "field_name", "contract set")))

    @field_validator("logical_source_identities")
    @classmethod
    def _source_identities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _sorted_unique(value, label="logical source identities")
        if any(not _CONTRACT_ID_RE.fullmatch(item) for item in value):
            raise ValueError("logical source identities must be canonical identifiers")
        return value

    @field_validator("evidence_kinds")
    @classmethod
    def _evidence_kinds(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _sorted_unique(value, label="evidence kinds")
        for item in value:
            governance_identifier(item, label="Capture evidence kind")
        return value

    @field_validator(
        "coordinate_schema_pins",
        "selector_schema_pins",
        "pins",
    )
    @classmethod
    def _pins(cls, value: tuple[ArtifactPin, ...], info: object) -> tuple[ArtifactPin, ...]:
        if value != tuple(sorted(value, key=_pin_key)):
            raise ValueError(f"{getattr(info, 'field_name', 'pins')} must be sorted")
        identities = tuple((item.role, item.target.qualified) for item in value)
        if len(identities) != len(set(identities)):
            raise ValueError("Capture contract pins must be unique by role and target")
        return value

    @field_validator(
        "replay_policy_digest",
        "provenance_rule_digest",
        "source_subject_mapping_digest",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _contract_shape(self) -> "CaptureContractV1":
        if self.identity.kind != "CaptureContract":
            raise ValueError("CaptureContract identity kind must be CaptureContract")
        if not _CONTRACT_ID_RE.fullmatch(self.identity.name):
            raise ValueError("CaptureContract identity name is not path-addressable")
        if (
            "ledger" in self.allowed_source_kinds
            and "ledger" not in self.allowed_materialization_modes
        ):
            raise ValueError("ledger Capture sources require ledger materialization support")
        if "cas" in self.allowed_source_kinds and "cas" not in self.allowed_materialization_modes:
            raise ValueError("CAS Capture sources require CAS materialization support")
        if "external" not in self.allowed_source_kinds and (
            {"external", "none"} & set(self.allowed_materialization_modes)
        ):
            raise ValueError("external/none materialization requires an external source kind")
        return self


def _built_in_pin(role: str, name: str) -> ArtifactPin:
    identity = ArtifactIdentity(kind="Contract", name=name)
    return ArtifactPin(
        role=role,
        target=identity,
        artifact_digest=typed_digest(
            ArtifactDigest,
            "playbill-built-in-contract-v1",
            {"identity": identity.qualified},
        ).tagged,
    )


DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT = CaptureContractV1(
    identity=ArtifactIdentity(
        kind="CaptureContract",
        name=DIRECT_SELF_ASSERTED_CONTRACT_ID,
    ),
    allowed_source_kinds=("cas", "external"),
    logical_source_identities=(DIRECT_SOURCE_IDENTITY,),
    coordinate_schema_pins=(
        _built_in_pin("coordinate-schema", "playbill.authenticated-request-coordinate-v1"),
        _built_in_pin("coordinate-schema", f"playbill.{DIRECT_EXTERNAL_COORDINATE_TYPE}"),
    ),
    selector_schema_pins=(
        _built_in_pin("selector-schema", f"playbill.{DIRECT_EXTERNAL_SELECTOR_TYPE}"),
        _built_in_pin("selector-schema", "playbill.direct-claim-source-selector-v1"),
    ),
    commitment_canonicalizer=_built_in_pin(
        "commitment-canonicalizer",
        "playbill.direct-claim-source-canonicalizer-v1",
    ),
    allowed_materialization_modes=("cas", "none"),
    selection_budget=CaptureSelectionBudgetV1(
        max_bytes=1024 * 1024,
        max_rows=1,
        max_items=1,
    ),
    retention_erasure_policy=CaptureRetentionErasurePolicyV1(
        body_retention="optional",
        erasure="prohibited",
        selector_privacy="direct_allowed",
    ),
    replay_policy_digest=typed_digest(
        Sha256Value,
        "playbill-capture-replay-policy-v1",
        {"policy": "direct-authenticated-request"},
    ).tagged,
    epistemic_grade="observed",
    provenance_rule_digest=typed_digest(
        Sha256Value,
        "playbill-capture-provenance-rule-v1",
        {"rule": "authenticated-self-asserted"},
    ).tagged,
    evidence_kinds=("self_asserted",),
    source_subject_mapping_digest=typed_digest(
        Sha256Value,
        "playbill-source-subject-mapping-v1",
        {"mapping": "direct-claim-statement"},
    ).tagged,
    authority=ArtifactAuthority(
        propose_roles=("owner",),
        approve_roles=("owner",),
    ),
)


def capture_contract_path(contract_id: str) -> str:
    if not _CONTRACT_ID_RE.fullmatch(contract_id):
        raise CaptureFormatError("CaptureContract identity is not path-addressable")
    return f"capture-contracts/{contract_id}.yaml"


def validate_capture_contract_path(contract: CaptureContractV1, path: str) -> str:
    expected = capture_contract_path(contract.identity.name)
    if path != expected:
        raise CaptureFormatError(
            f"CaptureContract identity/path disagreement: {contract.identity.qualified!r} "
            f"requires {expected!r}"
        )
    return path


def render_capture_contract(contract: CaptureContractV1) -> bytes:
    return canonical_bytes(contract.model_dump(mode="json")) + b"\n"


def parse_capture_contract(content: bytes, *, path: str) -> CaptureContractV1:
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, ValueError) as exc:
        raise CaptureFormatError("CaptureContract is not strict JSON") from exc
    if not isinstance(payload, dict) or payload.get("artifact_format") != (
        "playbill-capture-contract-v1"
    ):
        declared = payload.get("artifact_format") if isinstance(payload, dict) else None
        raise CaptureFormatError(f"unsupported CaptureContract artifact format: {declared!r}")
    try:
        contract = CaptureContractV1.model_validate(payload)
    except ValidationError as exc:
        raise CaptureFormatError("CaptureContract failed strict v1 validation") from exc
    if render_capture_contract(contract) != content:
        raise CaptureFormatError("CaptureContract is not in canonical wire form")
    validate_capture_contract_path(contract, path)
    return contract


def capture_contract_digest(contract: CaptureContractV1) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        contract.model_dump(mode="json"),
    )


class AcceptedCaptureContract(_StrictCaptureModel):
    path: str
    contract: CaptureContractV1
    artifact_digest: str

    @model_validator(mode="after")
    def _correspondence(self) -> "AcceptedCaptureContract":
        validate_capture_contract_path(self.contract, self.path)
        if self.artifact_digest != capture_contract_digest(self.contract).tagged:
            raise ValueError("accepted CaptureContract digest does not reproduce")
        return self


class CaptureContractLawResult(_StrictCaptureModel):
    verdict: Literal["accepted", "refused"]
    artifact_digest: str | None = None
    required_tier: PermissionTier | None = None
    approval_scope: tuple[str, ...] = ()
    diagnostics: tuple[CompilerDiagnostic, ...] = ()

    @model_validator(mode="after")
    def _shape(self) -> "CaptureContractLawResult":
        complete = (
            self.artifact_digest is not None
            and self.required_tier is not None
            and bool(self.approval_scope)
            and not self.diagnostics
        )
        if (self.verdict == "accepted") != complete:
            raise ValueError("CaptureContract law result shape differs from its verdict")
        return self


def _diagnostic(code: str, message: str, *, path: str) -> CompilerDiagnostic:
    return CompilerDiagnostic(
        code=code,
        severity="error",
        message=message,
        subject=SemanticAddress.whole_artifact(path),
    )


def evaluate_capture_contract_law(
    contract: CaptureContractV1,
    *,
    path: str,
    actor_roles: tuple[str, ...],
    predecessor: AcceptedCaptureContract | None,
) -> CaptureContractLawResult:
    """Activate only the frozen direct-authoring seed until PC-C generalizes authoring."""

    try:
        validate_capture_contract_path(contract, path)
    except CaptureFormatError as exc:
        return CaptureContractLawResult(
            verdict="refused",
            diagnostics=(
                _diagnostic("playbill.capture_contract.path_mismatch", str(exc), path=path),
            ),
        )
    if contract != DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT:
        return CaptureContractLawResult(
            verdict="refused",
            diagnostics=(
                _diagnostic(
                    "playbill.capture_contract.general_authoring_deferred",
                    "PC-B accepts only the byte-exact direct self-asserted seed contract.",
                    path=path,
                ),
            ),
        )
    if predecessor is not None:
        return CaptureContractLawResult(
            verdict="refused",
            diagnostics=(
                _diagnostic(
                    "playbill.capture_contract.seed_successor_unsupported",
                    "The PC-B direct seed contract is immutable; general succession is PC-C.",
                    path=path,
                ),
            ),
        )
    if contract.lifecycle != ArtifactLifecycle():
        return CaptureContractLawResult(
            verdict="refused",
            diagnostics=(
                _diagnostic(
                    "playbill.capture_contract.unexpected_predecessor",
                    "The direct seed must begin live without a predecessor.",
                    path=path,
                ),
            ),
        )
    if not set(actor_roles).intersection(contract.authority.propose_roles):
        return CaptureContractLawResult(
            verdict="refused",
            diagnostics=(
                _diagnostic(
                    "playbill.capture_contract.actor_unauthorized",
                    "The request actor lacks authority to install the direct seed contract.",
                    path=path,
                ),
            ),
        )
    return CaptureContractLawResult(
        verdict="accepted",
        artifact_digest=capture_contract_digest(contract).tagged,
        required_tier="governed_write",
        approval_scope=contract.authority.approve_roles,
    )


class CaptureRunCoordinateV1(_StrictCaptureModel):
    tag: Literal["playbill-capture-run-coordinate-v1"] = "playbill-capture-run-coordinate-v1"
    run_kind: Literal["procedure", "watcher", "provider"]
    run_id: str
    bound_generation: str
    executable_identity: ArtifactIdentity
    executable_digest: str

    @field_validator("run_id")
    @classmethod
    def _run_id(cls, value: str) -> str:
        if not _RUN_ID_RE.fullmatch(value):
            raise ValueError("Capture run_id is not canonical")
        return value

    @field_validator("bound_generation", "executable_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class SourceEffectiveTimeV1(_StrictCaptureModel):
    tag: Literal["playbill-source-effective-time-v1"] = "playbill-source-effective-time-v1"
    effective_from: datetime
    effective_until: datetime | None = None

    @model_validator(mode="after")
    def _interval(self) -> "SourceEffectiveTimeV1":
        if self.effective_from.tzinfo is None or self.effective_from.utcoffset() is None:
            raise ValueError("source effective time must be timezone-aware")
        if self.effective_until is not None:
            if self.effective_until.tzinfo is None or self.effective_until.utcoffset() is None:
                raise ValueError("source effective time must be timezone-aware")
            if self.effective_until <= self.effective_from:
                raise ValueError("source effective interval must be increasing")
        return self


class CaptureEnvelopeV1(_StrictCaptureModel):
    tag: Literal["playbill-capture-envelope-v1"] = "playbill-capture-envelope-v1"
    capture_contract_digest: str
    source: SourceReferenceV1
    commitment: EvidenceCommitmentV1
    run_coordinate: CaptureRunCoordinateV1
    run_receipt_digest: str
    producer: ArtifactIdentity
    producer_binding_digest: str
    observed_at: datetime
    source_effective_time: SourceEffectiveTimeV1 | None = None
    reducer_digest: str | None = None
    input_receipt_set_manifest_digest: str | None = None

    @field_validator(
        "capture_contract_digest",
        "run_receipt_digest",
        "producer_binding_digest",
        "reducer_digest",
        "input_receipt_set_manifest_digest",
    )
    @classmethod
    def _digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @field_validator("observed_at")
    @classmethod
    def _observed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Capture observed_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _envelope_shape(self) -> "CaptureEnvelopeV1":
        validate_source_commitment(self.source, self.commitment)
        if isinstance(self.source, ExternalSourceReferenceV1):
            if self.source.producer_binding_digest != self.producer_binding_digest:
                raise ValueError("external source and envelope producer bindings differ")
        derived = (
            self.reducer_digest is not None or self.input_receipt_set_manifest_digest is not None
        )
        if derived != (
            self.reducer_digest is not None and self.input_receipt_set_manifest_digest is not None
        ):
            raise ValueError("derived Captures require reducer and input receipt-set together")
        return self


def render_capture_envelope(envelope: CaptureEnvelopeV1) -> bytes:
    """Return the exact CAS object bytes; Capture envelopes carry no newline."""

    return canonical_bytes(envelope.model_dump(mode="json"))


def parse_capture_envelope(content: bytes) -> CaptureEnvelopeV1:
    try:
        envelope = CaptureEnvelopeV1.model_validate_json(content)
    except (ValueError, ValidationError) as exc:
        raise CaptureFormatError("Capture envelope failed strict v1 validation") from exc
    if render_capture_envelope(envelope) != content:
        raise CaptureFormatError("Capture envelope is not in canonical wire form")
    return envelope


def capture_digest(envelope: CaptureEnvelopeV1) -> CasDigest:
    return CasDigest(hashlib.sha256(render_capture_envelope(envelope)).hexdigest())


class DirectClaimSourceV1(_StrictCaptureModel):
    tag: Literal["playbill-direct-claim-source-v1"] = "playbill-direct-claim-source-v1"
    authored_by: str
    claim_id: str
    value: object
    rationale: str

    @field_validator("authored_by")
    @classmethod
    def _actor(cls, value: str) -> str:
        return governance_identifier(value, label="direct Capture author")

    @field_validator("value", mode="before")
    @classmethod
    def _value(cls, value: object) -> object:
        return normalize_canonical(value)

    @field_validator("rationale")
    @classmethod
    def _rationale(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("direct Claim rationale must not be empty")
        return value


class DirectByteSpanSelectionV1(_StrictCaptureModel):
    """An exact span over already-retained CAS bytes; no Document is required."""

    tag: Literal["playbill-direct-byte-span-selection-v1"] = (
        "playbill-direct-byte-span-selection-v1"
    )
    span: ContentSpan
    media_type: str | None = None

    @field_validator("media_type")
    @classmethod
    def _media_type(cls, value: str | None) -> str | None:
        if value is not None and ("/" not in value or any(char.isspace() for char in value)):
            raise ValueError("direct span media_type must use canonical type/subtype spelling")
        return value


class DirectExternalSelectionV1(_StrictCaptureModel):
    """Typed author-asserted external selection; PC-C adds adapter verification."""

    tag: Literal["playbill-direct-external-selection-v1"] = "playbill-direct-external-selection-v1"
    logical_source_identity: str
    coordinate_type: str
    coordinate: object
    selector_type: str
    selector: object
    commitment: EvidenceCommitmentV1

    @model_validator(mode="after")
    def _registered_shape(self) -> "DirectExternalSelectionV1":
        if self.coordinate_type not in DIRECT_EXTERNAL_COORDINATE_TYPES:
            raise ValueError("direct external selection uses an unregistered coordinate type")
        if self.selector_type not in DIRECT_EXTERNAL_SELECTOR_TYPES:
            raise ValueError("direct external selection uses an unregistered selector type")
        if self.commitment.materialization != "none":
            raise ValueError("PC-B direct external selections are attested-only metadata")
        if self.commitment.digest_kind == "exact_bytes":
            raise ValueError("an unmaterialized direct external selection cannot claim bytes")
        # Reuse the source-reference validators for logical identity, canonical values,
        # and secret/locator exclusion without accepting caller-authored bindings.
        ExternalSourceReferenceV1(
            source_identity=self.logical_source_identity,
            producer_binding_digest="sha256:" + "00" * 32,
            coordinate_type=self.coordinate_type,
            coordinate=self.coordinate,
            selector_type=self.selector_type,
            selector=self.selector,
            replayability="attested_only",
        )
        return self


DirectClaimSelectionV1 = Annotated[
    DirectByteSpanSelectionV1 | DirectExternalSelectionV1,
    Field(discriminator="tag"),
]


@runtime_checkable
class CaptureObjectStoreProtocol(Protocol):
    def store(self, content: bytes) -> CasObjectMetadata: ...

    def verify(self, digest: str) -> bool: ...

    def read(self, digest: str, *, access: BodyAccessContext) -> bytes: ...


class DirectCaptureBuildResult(_StrictCaptureModel):
    contract: CaptureContractV1
    contract_digest: str
    envelope: CaptureEnvelopeV1
    capture_digest: str
    source_body_digest: str
    source_body_materialized: bool


def _direct_source_bytes(source: DirectClaimSourceV1) -> bytes:
    return canonical_bytes(source.model_dump(mode="json"))


def _canonical_datetime(value: datetime) -> str:
    """Use the frozen Pydantic JSON representation in non-model preimages."""

    return value.isoformat()


def _direct_binding_digest(
    *,
    actor_id: str,
    accepted_coordinate: AcceptedCoordinate,
) -> str:
    return typed_digest(
        Sha256Value,
        "playbill-direct-producer-binding-v1",
        {
            "actor_id": actor_id,
            "accepted_coordinate": accepted_coordinate.model_dump(mode="json"),
        },
    ).tagged


def _store_capture_envelope(
    *,
    store: CaptureObjectStoreProtocol,
    envelope: CaptureEnvelopeV1,
    source_body_digest: str,
    source_body_materialized: bool,
) -> DirectCaptureBuildResult:
    envelope_bytes = render_capture_envelope(envelope)
    stored_envelope = store.store(envelope_bytes)
    expected_capture_digest = capture_digest(envelope).tagged
    if stored_envelope.digest != expected_capture_digest:
        raise PlaybillCasError("Capture envelope CAS digest did not reproduce")
    return DirectCaptureBuildResult(
        contract=DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
        contract_digest=envelope.capture_contract_digest,
        envelope=envelope,
        capture_digest=expected_capture_digest,
        source_body_digest=source_body_digest,
        source_body_materialized=source_body_materialized,
    )


def build_direct_claim_capture(
    *,
    store: CaptureObjectStoreProtocol,
    actor_id: str,
    claim_id: str,
    value: object,
    rationale: str,
    observed_at: datetime,
    accepted_coordinate: AcceptedCoordinate,
    materialize_source: bool = True,
) -> DirectCaptureBuildResult:
    """Create one bounded self-asserted Capture from authenticated service inputs."""

    source = DirectClaimSourceV1(
        authored_by=actor_id,
        claim_id=claim_id,
        value=value,
        rationale=rationale,
    )
    source_bytes = _direct_source_bytes(source)
    if len(source_bytes) > DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.selection_budget.max_bytes:
        raise CaptureFormatError("direct Claim source exceeds its accepted byte budget")
    source_digest = CasDigest(hashlib.sha256(source_bytes).hexdigest()).tagged
    contract_digest = capture_contract_digest(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT).tagged
    binding_digest = _direct_binding_digest(
        actor_id=actor_id,
        accepted_coordinate=accepted_coordinate,
    )
    receipt_digest = typed_digest(
        Sha256Value,
        "playbill-direct-capture-receipt-v1",
        {
            "actor_id": actor_id,
            "claim_id": claim_id,
            "observed_at": _canonical_datetime(observed_at),
            "source_digest": source_digest,
        },
    ).tagged
    if materialize_source:
        stored = store.store(source_bytes)
        if stored.digest != source_digest:
            raise PlaybillCasError("direct source CAS digest did not reproduce")
        source_reference: CasSourceReferenceV1 | ExternalSourceReferenceV1 = CasSourceReferenceV1(
            content_digest=source_digest
        )
        commitment = EvidenceCommitmentV1(
            digest_kind="exact_bytes",
            digest=source_digest,
            byte_length=len(source_bytes),
            materialization="cas",
        )
    else:
        source_reference = ExternalSourceReferenceV1(
            source_identity=DIRECT_SOURCE_IDENTITY,
            producer_binding_digest=binding_digest,
            coordinate_type="authenticated-request-v1",
            coordinate={
                "accepted_coordinate": accepted_coordinate.model_dump(mode="json"),
                "observed_at": _canonical_datetime(observed_at),
            },
            selector_type="direct-claim-source-v1",
            selector={"claim_id": claim_id},
            replayability="attested_only",
        )
        commitment = EvidenceCommitmentV1(
            digest_kind="canonical_value",
            digest=source_digest,
            materialization="none",
        )
    envelope = CaptureEnvelopeV1(
        capture_contract_digest=contract_digest,
        source=source_reference,
        commitment=commitment,
        run_coordinate=CaptureRunCoordinateV1(
            run_kind="provider",
            run_id=f"direct:{claim_id.casefold()}",
            bound_generation=accepted_coordinate.generation_root,
            executable_identity=DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.identity,
            executable_digest=contract_digest,
        ),
        run_receipt_digest=receipt_digest,
        producer=ArtifactIdentity(kind="Principal", name=actor_id),
        producer_binding_digest=binding_digest,
        observed_at=observed_at,
    )
    return _store_capture_envelope(
        store=store,
        envelope=envelope,
        source_body_digest=source_digest,
        source_body_materialized=materialize_source,
    )


def build_direct_claim_selection_capture(
    *,
    store: CaptureObjectStoreProtocol,
    actor_id: str,
    claim_id: str,
    rationale: str,
    observed_at: datetime,
    accepted_coordinate: AcceptedCoordinate,
    selection: DirectClaimSelectionV1,
) -> DirectCaptureBuildResult:
    """Bind one exact span or typed external selector as self-asserted evidence."""

    binding_digest = _direct_binding_digest(
        actor_id=actor_id,
        accepted_coordinate=accepted_coordinate,
    )
    contract_digest = capture_contract_digest(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT).tagged
    if isinstance(selection, DirectByteSpanSelectionV1):
        if not store.verify(selection.span.content_digest):
            raise CaptureFormatError("selected span content is unavailable in CAS")
        source_bytes = store.read(
            selection.span.content_digest,
            access=BodyAccessContext(principal_id="playbill-service", can_read_body=True),
        )
        if selection.span.end_byte > len(source_bytes):
            raise CaptureFormatError("selected span exceeds its exact CAS body")
        source_reference: CasSourceReferenceV1 | ExternalSourceReferenceV1 = CasSourceReferenceV1(
            content_digest=selection.span.content_digest
        )
        commitment = EvidenceCommitmentV1(
            digest_kind="exact_bytes",
            digest=selection.span.content_digest,
            byte_length=len(source_bytes),
            materialization="cas",
        )
        source_digest = selection.span.content_digest
        materialized = True
    else:
        source_reference = ExternalSourceReferenceV1(
            source_identity=DIRECT_SOURCE_IDENTITY,
            producer_binding_digest=binding_digest,
            coordinate_type=DIRECT_EXTERNAL_COORDINATE_TYPE,
            coordinate={
                "logical_source_identity": selection.logical_source_identity,
                "source_coordinate": selection.coordinate,
                "source_coordinate_type": selection.coordinate_type,
            },
            selector_type=DIRECT_EXTERNAL_SELECTOR_TYPE,
            selector={
                "claim_id": claim_id,
                "source_selector": selection.selector,
                "source_selector_type": selection.selector_type,
            },
            replayability="attested_only",
        )
        commitment = selection.commitment
        source_digest = selection.commitment.digest
        materialized = False
    receipt_digest = typed_digest(
        Sha256Value,
        "playbill-direct-selection-capture-receipt-v1",
        {
            "actor_id": actor_id,
            "claim_id": claim_id,
            "observed_at": _canonical_datetime(observed_at),
            "rationale": rationale,
            "selection": selection.model_dump(mode="json"),
        },
    ).tagged
    envelope = CaptureEnvelopeV1(
        capture_contract_digest=contract_digest,
        source=source_reference,
        commitment=commitment,
        run_coordinate=CaptureRunCoordinateV1(
            run_kind="provider",
            run_id=f"direct-selection:{claim_id.casefold()}",
            bound_generation=accepted_coordinate.generation_root,
            executable_identity=DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.identity,
            executable_digest=contract_digest,
        ),
        run_receipt_digest=receipt_digest,
        producer=ArtifactIdentity(kind="Principal", name=actor_id),
        producer_binding_digest=binding_digest,
        observed_at=observed_at,
    )
    return _store_capture_envelope(
        store=store,
        envelope=envelope,
        source_body_digest=source_digest,
        source_body_materialized=materialized,
    )


def verify_capture(
    digest: str,
    *,
    store: CaptureObjectStoreProtocol,
    contract: CaptureContractV1,
) -> CaptureEnvelopeV1:
    """Replay one envelope and the proof obligations available in PC-B."""

    CasDigest.from_tagged(digest)
    content = store.read(
        digest,
        access=BodyAccessContext(principal_id="playbill-compiler", can_read_body=True),
    )
    envelope = parse_capture_envelope(content)
    if capture_digest(envelope).tagged != digest:
        raise CaptureFormatError("Capture envelope digest does not reproduce")
    expected_contract = capture_contract_digest(contract).tagged
    if envelope.capture_contract_digest != expected_contract:
        raise CaptureFormatError("Capture envelope names a different contract digest")
    if envelope.run_coordinate.executable_identity != contract.identity or (
        envelope.run_coordinate.executable_digest != expected_contract
    ):
        raise CaptureFormatError("Capture run coordinate differs from its contract")
    if envelope.source.kind not in contract.allowed_source_kinds:
        raise CaptureFormatError("Capture source kind is not permitted by its contract")
    if envelope.commitment.materialization not in contract.allowed_materialization_modes:
        raise CaptureFormatError("Capture materialization is not permitted by its contract")
    if isinstance(envelope.source, ExternalSourceReferenceV1):
        if envelope.source.source_identity not in contract.logical_source_identities:
            raise CaptureFormatError("Capture logical source is not declared by its contract")
        if contract == DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT:
            schemas = (
                envelope.source.coordinate_type,
                envelope.source.selector_type,
            )
            if schemas not in {
                ("authenticated-request-v1", "direct-claim-source-v1"),
                (DIRECT_EXTERNAL_COORDINATE_TYPE, DIRECT_EXTERNAL_SELECTOR_TYPE),
            }:
                raise CaptureFormatError("direct Capture source schemas are not registered")
            if schemas == (DIRECT_EXTERNAL_COORDINATE_TYPE, DIRECT_EXTERNAL_SELECTOR_TYPE):
                coordinate = envelope.source.coordinate
                selector = envelope.source.selector
                if not isinstance(coordinate, dict) or not isinstance(selector, dict):
                    raise CaptureFormatError("direct external selection metadata is malformed")
                if (
                    coordinate.get("source_coordinate_type")
                    not in (DIRECT_EXTERNAL_COORDINATE_TYPES)
                    or selector.get("source_selector_type") not in DIRECT_EXTERNAL_SELECTOR_TYPES
                ):
                    raise CaptureFormatError("direct external selection schema is not registered")
                claim_id = selector.get("claim_id")
                if not isinstance(claim_id, str) or not claim_id.startswith("CLM-"):
                    raise CaptureFormatError("direct external selection has no Claim identity")
    if isinstance(envelope.source, CasSourceReferenceV1):
        if envelope.source.content_digest != envelope.commitment.digest:
            raise CaptureFormatError("Capture CAS source differs from its commitment")
        if not store.verify(envelope.source.content_digest):
            raise CaptureFormatError("Capture source material is unavailable")
        source_bytes = store.read(
            envelope.source.content_digest,
            access=BodyAccessContext(principal_id="playbill-compiler", can_read_body=True),
        )
        if len(source_bytes) != envelope.commitment.byte_length:
            raise CaptureFormatError("Capture source byte length does not reproduce")
    if contract.epistemic_grade != "derived" and (
        envelope.reducer_digest is not None
        or envelope.input_receipt_set_manifest_digest is not None
    ):
        raise CaptureFormatError("non-derived Capture cannot carry derivation receipts")
    return envelope


__all__ = [
    "AcceptedCaptureContract",
    "CanonicalDurationV1",
    "CaptureContractLawResult",
    "CaptureContractV1",
    "CaptureEnvelopeV1",
    "CaptureFormatError",
    "CaptureObjectStoreProtocol",
    "CaptureRetentionErasurePolicyV1",
    "CaptureRunCoordinateV1",
    "CaptureSelectionBudgetV1",
    "DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT",
    "DIRECT_SELF_ASSERTED_CONTRACT_ID",
    "DIRECT_EXTERNAL_COORDINATE_TYPES",
    "DIRECT_EXTERNAL_SELECTOR_TYPES",
    "DirectCaptureBuildResult",
    "DirectByteSpanSelectionV1",
    "DirectClaimSelectionV1",
    "DirectClaimSourceV1",
    "DirectExternalSelectionV1",
    "SourceEffectiveTimeV1",
    "build_direct_claim_capture",
    "build_direct_claim_selection_capture",
    "capture_contract_digest",
    "capture_contract_path",
    "capture_digest",
    "evaluate_capture_contract_law",
    "parse_capture_contract",
    "parse_capture_envelope",
    "render_capture_contract",
    "render_capture_envelope",
    "validate_capture_contract_path",
    "verify_capture",
]
