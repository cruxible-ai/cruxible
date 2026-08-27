"""Playbill-native Capture contracts and the bounded direct-authoring path."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from cruxible_client.contracts.artifacts import (
    ArtifactAuthority,
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_client.contracts.canonical import (
    ArtifactDigest,
    CasDigest,
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
    typed_digest,
)
from cruxible_client.contracts.cas_contracts import (
    BodyAccessContext,
    CasObjectMetadata,
)
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.errors import PlaybillCasError, PlaybillFormatError
from cruxible_client.contracts.governance import PermissionTier, governance_identifier
from cruxible_client.contracts.semantic import ContentSpan, SemanticAddress
from cruxible_client.contracts.source_references import (
    CasSourceReferenceV1,
    EvidenceCommitmentV1,
    ExternalSourceReferenceV1,
    LedgerSourceReferenceV1,
    SourceReferenceV1,
    validate_source_commitment,
)

if TYPE_CHECKING:
    from cruxible_client.contracts.projection import AcceptedCoordinate

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

COORDINATOR_SELF_SOURCE_CONTRACT_ID = "playbill.coordinator-self-source-v1"
COORDINATOR_SELF_SOURCE_IDENTITY = "playbill.coordinator-authoring"
COORDINATOR_SELF_SOURCE_REPLAY_POLICY = "playbill.coordinator-retained-cas-v1"
COORDINATOR_SELF_SOURCE_PROVENANCE_RULE = "playbill.coordinator-proposer-observed-v1"
COORDINATOR_SELF_SOURCE_SUBJECT_MAPPING = "playbill.coordinator-claim-self-source-v1"

FOREIGN_SOURCE_CONTRACT_PREFIX = "playbill.foreign-source."
FOREIGN_SOURCE_COORDINATE_TYPE = "foreign-source-snapshot-v1"
FOREIGN_SOURCE_SELECTOR_TYPE = "foreign-source-span-v1"
FOREIGN_SOURCE_PROVENANCE_RULE = "playbill.external.proposer-asserted-v1"
FOREIGN_SOURCE_REPLAY_POLICY = "playbill.external.retained-selection-replay-v1"
FOREIGN_SOURCE_SUBJECT_MAPPING = "playbill.external.claim-statement-span-v1"
FOREIGN_SOURCE_MAX_BYTES = 1024 * 1024


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


def capture_component_pin(role: str, name: str) -> ArtifactPin:
    """Return one compiler-owned, digest-addressed Capture component pin."""

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


_CAPTURE_COMPONENT_PIN_NAMES: tuple[tuple[str, str], ...] = (
    ("commitment-canonicalizer", "canonical-json-v1"),
    ("commitment-canonicalizer", "sha256-bytes-v1"),
    ("coordinate-grammar", "playbill.database-snapshot-coordinate-v1"),
    ("coordinate-schema", "database-snapshot-v1"),
    ("coordinate-schema", FOREIGN_SOURCE_COORDINATE_TYPE),
    ("coordinate-schema", "postgres-lsn-v1"),
    ("erasure-rule", "playbill.capture-erasure-authority-v1"),
    ("proof-adapter", "playbill.database-snapshot-proof-v1"),
    ("provenance-rule", "playbill.external.daemon-fetched-v1"),
    ("provenance-rule", COORDINATOR_SELF_SOURCE_PROVENANCE_RULE),
    ("provenance-rule", FOREIGN_SOURCE_PROVENANCE_RULE),
    ("replay-policy", "playbill.external.exact-replay-v1"),
    ("replay-policy", COORDINATOR_SELF_SOURCE_REPLAY_POLICY),
    ("replay-policy", FOREIGN_SOURCE_REPLAY_POLICY),
    ("selector-schema", FOREIGN_SOURCE_SELECTOR_TYPE),
    ("selector-schema", "query-result-v1"),
    ("selector-schema", "relation-primary-key-v1"),
    ("source-subject-mapping", FOREIGN_SOURCE_SUBJECT_MAPPING),
    ("source-subject-mapping", COORDINATOR_SELF_SOURCE_SUBJECT_MAPPING),
    ("source-subject-mapping", "playbill.external.record-subject-v1"),
)


class CaptureComponentRegistry:
    """Fail-closed compiler registry for non-artifact Capture semantics."""

    def __init__(self, pins: tuple[ArtifactPin, ...]) -> None:
        entries: dict[tuple[str, str], str] = {}
        for pin in pins:
            key = (pin.role, pin.target.qualified)
            if pin.target.kind != "Contract" or key in entries:
                raise ValueError("Capture component registry entries must be unique Contracts")
            entries[key] = pin.artifact_digest
        self._entries = entries

    def resolves(self, pin: ArtifactPin) -> bool:
        return self._entries.get((pin.role, pin.target.qualified)) == pin.artifact_digest


PLAYBILL_CAPTURE_COMPONENTS = CaptureComponentRegistry(
    tuple(capture_component_pin(role, name) for role, name in _CAPTURE_COMPONENT_PIN_NAMES)
)


DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT = CaptureContractV1(
    identity=ArtifactIdentity(
        kind="CaptureContract",
        name=DIRECT_SELF_ASSERTED_CONTRACT_ID,
    ),
    allowed_source_kinds=("cas", "external"),
    logical_source_identities=(DIRECT_SOURCE_IDENTITY,),
    coordinate_schema_pins=(
        capture_component_pin("coordinate-schema", "playbill.authenticated-request-coordinate-v1"),
        capture_component_pin("coordinate-schema", f"playbill.{DIRECT_EXTERNAL_COORDINATE_TYPE}"),
    ),
    selector_schema_pins=(
        capture_component_pin("selector-schema", f"playbill.{DIRECT_EXTERNAL_SELECTOR_TYPE}"),
        capture_component_pin("selector-schema", "playbill.direct-claim-source-selector-v1"),
    ),
    commitment_canonicalizer=capture_component_pin(
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


_COORDINATOR_REPLAY_PIN = capture_component_pin(
    "replay-policy",
    COORDINATOR_SELF_SOURCE_REPLAY_POLICY,
)
_COORDINATOR_PROVENANCE_PIN = capture_component_pin(
    "provenance-rule",
    COORDINATOR_SELF_SOURCE_PROVENANCE_RULE,
)
_COORDINATOR_MAPPING_PIN = capture_component_pin(
    "source-subject-mapping",
    COORDINATOR_SELF_SOURCE_SUBJECT_MAPPING,
)

COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT = CaptureContractV1(
    identity=ArtifactIdentity(
        kind="CaptureContract",
        name=COORDINATOR_SELF_SOURCE_CONTRACT_ID,
    ),
    allowed_source_kinds=("cas",),
    logical_source_identities=(COORDINATOR_SELF_SOURCE_IDENTITY,),
    coordinate_schema_pins=(),
    selector_schema_pins=(),
    commitment_canonicalizer=capture_component_pin(
        "commitment-canonicalizer",
        "sha256-bytes-v1",
    ),
    allowed_materialization_modes=("cas",),
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
    replay_policy_digest=_COORDINATOR_REPLAY_PIN.artifact_digest,
    epistemic_grade="observed",
    provenance_rule_digest=_COORDINATOR_PROVENANCE_PIN.artifact_digest,
    evidence_kinds=("self_asserted",),
    source_subject_mapping_digest=_COORDINATOR_MAPPING_PIN.artifact_digest,
    authority=ArtifactAuthority(
        propose_roles=("owner",),
        approve_roles=("owner",),
    ),
    pins=tuple(
        sorted(
            (
                _COORDINATOR_MAPPING_PIN,
                _COORDINATOR_PROVENANCE_PIN,
                _COORDINATOR_REPLAY_PIN,
            ),
            key=_pin_key,
        )
    ),
)


def foreign_source_contract_id(logical_source_identity: str) -> str:
    """Name the CaptureContract one foreign logical source is governed under.

    One contract per logical source, rather than one contract listing many.
    ``logical_source_identities`` is an enumerated tuple, so a shared contract
    would have to be succeeded every time a corpus grew a file -- and succession
    needs the exact predecessor digest and authority over it. A per-source
    contract is instead always *new*: an authoring that governs a source for the
    first time writes it, and every later authoring against the same source
    writes byte-identical content that deduplicates against the accepted base.
    """

    if not _CONTRACT_ID_RE.fullmatch(logical_source_identity):
        raise CaptureFormatError("foreign logical source identity must be canonical and lowercase")
    contract_id = f"{FOREIGN_SOURCE_CONTRACT_PREFIX}{logical_source_identity}"
    if not _CONTRACT_ID_RE.fullmatch(contract_id):
        raise CaptureFormatError("foreign logical source identity is not path-addressable")
    return contract_id


def foreign_source_capture_contract(logical_source_identity: str) -> CaptureContractV1:
    """Build the self-asserted CaptureContract governing one foreign logical source.

    Every component this contract names is registered in the compiler registry
    by reviewed code, so it passes the same fail-closed component and rule
    checks any other external contract does -- no exemption, no placeholder
    digest. The registered names are the honest ones: the provenance rule is
    proposer-asserted rather than daemon-fetched, and the replay policy promises
    only what retained bytes can deliver. Its external reference is therefore
    attested-only: the daemon can reproduce exactly the bytes it was shown and
    can re-read the foreign source never.
    """

    replay_pin = capture_component_pin("replay-policy", FOREIGN_SOURCE_REPLAY_POLICY)
    provenance_pin = capture_component_pin("provenance-rule", FOREIGN_SOURCE_PROVENANCE_RULE)
    mapping_pin = capture_component_pin("source-subject-mapping", FOREIGN_SOURCE_SUBJECT_MAPPING)
    return CaptureContractV1(
        identity=ArtifactIdentity(
            kind="CaptureContract",
            name=foreign_source_contract_id(logical_source_identity),
        ),
        allowed_source_kinds=("external",),
        logical_source_identities=(logical_source_identity,),
        coordinate_schema_pins=(
            capture_component_pin("coordinate-schema", FOREIGN_SOURCE_COORDINATE_TYPE),
        ),
        selector_schema_pins=(
            capture_component_pin("selector-schema", FOREIGN_SOURCE_SELECTOR_TYPE),
        ),
        commitment_canonicalizer=capture_component_pin(
            "commitment-canonicalizer",
            "sha256-bytes-v1",
        ),
        allowed_materialization_modes=("cas",),
        selection_budget=CaptureSelectionBudgetV1(
            max_bytes=FOREIGN_SOURCE_MAX_BYTES,
            max_rows=1,
            max_items=1,
        ),
        retention_erasure_policy=CaptureRetentionErasurePolicyV1(
            body_retention="optional",
            erasure="prohibited",
            selector_privacy="direct_allowed",
        ),
        replay_policy_digest=replay_pin.artifact_digest,
        epistemic_grade="observed",
        provenance_rule_digest=provenance_pin.artifact_digest,
        evidence_kinds=("self_asserted",),
        source_subject_mapping_digest=mapping_pin.artifact_digest,
        authority=ArtifactAuthority(
            propose_roles=("owner",),
            approve_roles=("owner",),
        ),
        pins=tuple(sorted((replay_pin, provenance_pin, mapping_pin), key=_pin_key)),
    )


_SELF_ASSERTED_PROVENANCE_RULE_DIGESTS: frozenset[str] = frozenset(
    {
        DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT.provenance_rule_digest,
        COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT.provenance_rule_digest,
        capture_component_pin("provenance-rule", FOREIGN_SOURCE_PROVENANCE_RULE).artifact_digest,
    }
)


def capture_contract_is_self_asserted(contract: CaptureContractV1) -> bool:
    """Whether a contract's declared provenance rule is proposer-supplied.

    The grade follows the contract's own registered provenance rule rather than
    an identity comparison against one constant, so a second self-asserted
    contract cannot silently be graded as though a daemon had fetched it. Only a
    contract that pins a registered rule can carry one of these digests -- the
    rule-registry check refuses any other -- so this stays exactly as narrow as
    the identity comparison it generalizes.
    """

    return contract.provenance_rule_digest in _SELF_ASSERTED_PROVENANCE_RULE_DIGESTS


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
    """Evaluate the complete v1 contract without granting source or Claim authority."""

    try:
        validate_capture_contract_path(contract, path)
    except CaptureFormatError as exc:
        return CaptureContractLawResult(
            verdict="refused",
            diagnostics=(
                _diagnostic("playbill.capture_contract.path_mismatch", str(exc), path=path),
            ),
        )
    if (
        contract.identity.name == COORDINATOR_SELF_SOURCE_CONTRACT_ID
        and contract != COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT
    ):
        return CaptureContractLawResult(
            verdict="refused",
            diagnostics=(
                _diagnostic(
                    "playbill.capture_contract.coordinator_profile_mismatch",
                    "The coordinator self-source identity is reserved for its exact "
                    "retained profile.",
                    path=path,
                ),
            ),
        )
    if predecessor is None and contract.lifecycle.predecessor_digest is not None:
        return CaptureContractLawResult(
            verdict="refused",
            diagnostics=(
                _diagnostic(
                    "playbill.capture_contract.predecessor_missing",
                    "A new CaptureContract cannot name a predecessor.",
                    path=path,
                ),
            ),
        )
    if predecessor is not None:
        if contract.identity != predecessor.contract.identity:
            return CaptureContractLawResult(
                verdict="refused",
                diagnostics=(
                    _diagnostic(
                        "playbill.capture_contract.identity_changed",
                        "A CaptureContract successor must preserve stable identity.",
                        path=path,
                    ),
                ),
            )
        if contract.lifecycle.predecessor_digest != predecessor.artifact_digest:
            return CaptureContractLawResult(
                verdict="refused",
                diagnostics=(
                    _diagnostic(
                        "playbill.capture_contract.predecessor_mismatch",
                        "A CaptureContract successor must pin the exact predecessor digest.",
                        path=path,
                    ),
                ),
            )
    registry_roles = {
        (pin.role, pin.artifact_digest)
        for pin in (
            *contract.coordinate_schema_pins,
            *contract.selector_schema_pins,
            contract.commitment_canonicalizer,
            *contract.pins,
        )
    }
    component_pins = (
        *contract.coordinate_schema_pins,
        *contract.selector_schema_pins,
        contract.commitment_canonicalizer,
        *(pin for pin in contract.pins if pin.target.kind == "Contract"),
    )
    unresolved_components = tuple(
        pin for pin in component_pins if not PLAYBILL_CAPTURE_COMPONENTS.resolves(pin)
    )
    if unresolved_components and contract != DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT:
        return CaptureContractLawResult(
            verdict="refused",
            diagnostics=(
                _diagnostic(
                    "playbill.capture_contract.component_registry_unresolved",
                    "A Capture component identifier/digest is absent from the compiler registry.",
                    path=path,
                ),
            ),
        )
    required_registry_entries = {
        ("replay-policy", contract.replay_policy_digest),
        ("provenance-rule", contract.provenance_rule_digest),
        ("source-subject-mapping", contract.source_subject_mapping_digest),
    }
    if not required_registry_entries.issubset(registry_roles) and (
        contract != DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT
    ):
        return CaptureContractLawResult(
            verdict="refused",
            diagnostics=(
                _diagnostic(
                    "playbill.capture_contract.rule_registry_unresolved",
                    "Replay, provenance, and source-subject rules require exact governed pins.",
                    path=path,
                ),
            ),
        )
    if "external" in contract.allowed_source_kinds and (
        not contract.coordinate_schema_pins or not contract.selector_schema_pins
    ):
        return CaptureContractLawResult(
            verdict="refused",
            diagnostics=(
                _diagnostic(
                    "playbill.capture_contract.external_schema_missing",
                    "External CaptureContracts require coordinate and selector schema pins.",
                    path=path,
                ),
            ),
        )
    erasure_digest = contract.retention_erasure_policy.erasure_rule_digest
    if erasure_digest is not None and ("erasure-rule", erasure_digest) not in registry_roles:
        return CaptureContractLawResult(
            verdict="refused",
            diagnostics=(
                _diagnostic(
                    "playbill.capture_contract.erasure_rule_unresolved",
                    "Authorized erasure requires an exact governed erasure-rule pin.",
                    path=path,
                ),
            ),
        )
    return CaptureContractLawResult(
        verdict="accepted",
        artifact_digest=capture_contract_digest(contract).tagged,
        required_tier="governed_write",
        approval_scope=(),
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


class InputReceiptSetManifestV1(_StrictCaptureModel):
    """Content-addressed exact inputs required by a derived Capture."""

    tag: Literal["playbill-input-receipt-set-manifest-v1"] = (
        "playbill-input-receipt-set-manifest-v1"
    )
    input_receipt_digests: tuple[str, ...] = ()
    input_capture_digests: tuple[str, ...] = ()
    input_claim_artifact_digests: tuple[str, ...] = ()

    @field_validator(
        "input_receipt_digests",
        "input_capture_digests",
        "input_claim_artifact_digests",
    )
    @classmethod
    def _digests(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("ascii"))):
            raise ValueError("input receipt-set digests must be sorted and unique")
        for item in value:
            Sha256Value.from_tagged(item)
        return value

    @model_validator(mode="after")
    def _nonempty(self) -> "InputReceiptSetManifestV1":
        if not any(
            (
                self.input_receipt_digests,
                self.input_capture_digests,
                self.input_claim_artifact_digests,
            )
        ):
            raise ValueError("input receipt-set manifest must name at least one exact input")
        return self


def render_input_receipt_set_manifest(manifest: InputReceiptSetManifestV1) -> bytes:
    return canonical_bytes(manifest.model_dump(mode="json"))


def input_receipt_set_manifest_digest(manifest: InputReceiptSetManifestV1) -> CasDigest:
    return CasDigest(hashlib.sha256(render_input_receipt_set_manifest(manifest)).hexdigest())


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


class DirectForeignSourceSelectionV1(_StrictCaptureModel):
    """An exact span of a foreign logical source, committed exactly as presented.

    The proposer names the logical source, hands over the source bytes it read,
    and points at the window inside them; the daemon commits to the bytes it was
    shown and to nothing else. That is weaker than an acquisition -- nothing was
    fetched and nothing can be re-read -- and it is the whole point: the
    resulting Capture cites a *logical* source rather than content, so an edit to
    that source is measurable as drift and a relocation within it is not.
    """

    tag: Literal["playbill-direct-foreign-source-selection-v1"] = (
        "playbill-direct-foreign-source-selection-v1"
    )
    logical_source_identity: str
    span: ContentSpan
    media_type: str | None = None

    @field_validator("logical_source_identity")
    @classmethod
    def _logical_source_identity(cls, value: str) -> str:
        try:
            foreign_source_contract_id(value)
        except CaptureFormatError as exc:
            raise ValueError(str(exc)) from exc
        return value

    @field_validator("media_type")
    @classmethod
    def _media_type(cls, value: str | None) -> str | None:
        if value is not None and ("/" not in value or any(char.isspace() for char in value)):
            raise ValueError("foreign span media_type must use canonical type/subtype spelling")
        return value

    @model_validator(mode="after")
    def _selection(self) -> "DirectForeignSourceSelectionV1":
        if self.span.end_byte <= self.span.start_byte:
            raise ValueError("a foreign source selection must cover at least one byte")
        return self


DirectClaimSelectionV1 = Annotated[
    DirectByteSpanSelectionV1 | DirectExternalSelectionV1 | DirectForeignSourceSelectionV1,
    Field(discriminator="tag"),
]


@runtime_checkable
class CaptureObjectStoreProtocol(Protocol):
    def store(self, content: bytes) -> CasObjectMetadata: ...

    def verify(self, digest: str) -> bool: ...

    def read(self, digest: str, *, access: BodyAccessContext) -> bytes: ...


@runtime_checkable
class LedgerMaterialResolverProtocol(Protocol):
    def read_ledger_source(self, source: LedgerSourceReferenceV1) -> bytes: ...


class DirectCaptureBuildResult(_StrictCaptureModel):
    contract: CaptureContractV1
    contract_digest: str
    envelope: CaptureEnvelopeV1
    capture_digest: str
    source_body_digest: str
    source_body_materialized: bool


def capture_is_direct_self_source(
    envelope: CaptureEnvelopeV1,
    *,
    contract: CaptureContractV1,
    store: CaptureObjectStoreProtocol,
    claim_id: str,
) -> bool:
    """Recognize only the direct body builder's Claim-bound source shape."""

    if (
        contract.identity.name != DIRECT_SELF_ASSERTED_CONTRACT_ID
        or envelope.run_coordinate.run_id != f"direct:{claim_id.casefold()}"
    ):
        return False
    if isinstance(envelope.source, ExternalSourceReferenceV1):
        return (
            envelope.source.coordinate_type == "authenticated-request-v1"
            and envelope.source.selector_type == "direct-claim-source-v1"
            and isinstance(envelope.source.selector, dict)
            and envelope.source.selector.get("claim_id") == claim_id
        )
    if not isinstance(envelope.source, CasSourceReferenceV1):
        return False
    try:
        content = store.read(
            envelope.source.content_digest,
            access=BodyAccessContext(principal_id="playbill-compiler", can_read_body=True),
        )
        source = DirectClaimSourceV1.model_validate_json(content)
    except (PlaybillCasError, ValidationError):
        return False
    return (
        source.claim_id == claim_id and canonical_bytes(source.model_dump(mode="json")) == content
    )


def capture_is_coordinator_self_source(
    envelope: CaptureEnvelopeV1,
    *,
    contract: CaptureContractV1,
    claim_id: str,
) -> bool:
    """Recognize the mandatory-retained coordinator profile at its Claim binding."""

    return (
        contract == COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT
        and isinstance(envelope.source, CasSourceReferenceV1)
        and envelope.commitment.materialization == "cas"
        and envelope.run_coordinate.run_id == f"coordinator-self-source:{claim_id.casefold()}"
        and envelope.run_coordinate.executable_identity == contract.identity
        and envelope.run_coordinate.executable_digest == capture_contract_digest(contract).tagged
    )


class CaptureBuildResult(_StrictCaptureModel):
    contract_digest: str
    envelope: CaptureEnvelopeV1
    capture_digest: str
    commitment_digest: str
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
    contract: CaptureContractV1,
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
        contract=contract,
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
        contract=DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
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
    selection: DirectByteSpanSelectionV1 | DirectExternalSelectionV1,
) -> DirectCaptureBuildResult:
    """Bind one exact span or typed external selector as self-asserted evidence.

    Both forms this builder accepts are content-addressed or unmaterialized, so
    neither names a logical source. A foreign-source selection does name one and
    is built by :func:`build_foreign_source_capture` under its own contract; it
    is refused here rather than being quietly signed under this one.
    """

    if not isinstance(selection, DirectByteSpanSelectionV1 | DirectExternalSelectionV1):
        raise CaptureFormatError(
            "a logical-source selection is not admissible under the direct CaptureContract"
        )
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
        contract=DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT,
        envelope=envelope,
        source_body_digest=source_digest,
        source_body_materialized=materialized,
    )


def build_foreign_source_capture(
    *,
    store: CaptureObjectStoreProtocol,
    actor_id: str,
    claim_id: str,
    rationale: str,
    observed_at: datetime,
    accepted_coordinate: AcceptedCoordinate,
    selection: DirectForeignSourceSelectionV1,
) -> DirectCaptureBuildResult:
    """Commit one exact span of a foreign logical source as self-asserted evidence.

    Three things are bound and they are deliberately different things. The
    *coordinate* names the whole source snapshot the proposer presented, by
    content digest and length, so a reader can tell which revision was read. The
    *selector* names the window inside that snapshot the Claim is about. The
    *commitment* is over the selected bytes alone -- not the snapshot -- because
    that is the unit coverage looks for when the file later moves or changes.
    """

    contract = foreign_source_capture_contract(selection.logical_source_identity)
    contract_digest_value = capture_contract_digest(contract).tagged
    if not store.verify(selection.span.content_digest):
        raise CaptureFormatError("presented foreign source content is unavailable in CAS")
    source_bytes = store.read(
        selection.span.content_digest,
        access=BodyAccessContext(principal_id="playbill-service", can_read_body=True),
    )
    if selection.span.end_byte > len(source_bytes):
        raise CaptureFormatError("selected span exceeds the presented foreign source bytes")
    selected = source_bytes[selection.span.start_byte : selection.span.end_byte]
    if len(selected) > contract.selection_budget.max_bytes:
        raise CaptureFormatError("foreign source selection exceeds its accepted byte budget")
    selection_digest = CasDigest(hashlib.sha256(selected).hexdigest()).tagged
    stored = store.store(selected)
    if stored.digest != selection_digest:
        raise PlaybillCasError("foreign source selection CAS digest did not reproduce")
    binding_digest = _direct_binding_digest(
        actor_id=actor_id,
        accepted_coordinate=accepted_coordinate,
    )
    receipt_digest = typed_digest(
        Sha256Value,
        "playbill-foreign-source-capture-receipt-v1",
        {
            "actor_id": actor_id,
            "claim_id": claim_id,
            "observed_at": _canonical_datetime(observed_at),
            "rationale": rationale,
            "selection": selection.model_dump(mode="json"),
        },
    ).tagged
    envelope = CaptureEnvelopeV1(
        capture_contract_digest=contract_digest_value,
        source=ExternalSourceReferenceV1(
            source_identity=selection.logical_source_identity,
            producer_binding_digest=binding_digest,
            coordinate_type=FOREIGN_SOURCE_COORDINATE_TYPE,
            coordinate={
                "source_byte_length": len(source_bytes),
                "source_content_digest": selection.span.content_digest,
            },
            selector_type=FOREIGN_SOURCE_SELECTOR_TYPE,
            selector={
                "claim_id": claim_id,
                "end_byte": selection.span.end_byte,
                "start_byte": selection.span.start_byte,
            },
            replayability="attested_only",
        ),
        commitment=EvidenceCommitmentV1(
            digest_kind="exact_bytes",
            digest=selection_digest,
            byte_length=len(selected),
            materialization="cas",
        ),
        run_coordinate=CaptureRunCoordinateV1(
            run_kind="provider",
            run_id=f"foreign-source:{claim_id.casefold()}",
            bound_generation=accepted_coordinate.generation_root,
            executable_identity=contract.identity,
            executable_digest=contract_digest_value,
        ),
        run_receipt_digest=receipt_digest,
        producer=ArtifactIdentity(kind="Principal", name=actor_id),
        producer_binding_digest=binding_digest,
        observed_at=observed_at,
    )
    return _store_capture_envelope(
        store=store,
        contract=contract,
        envelope=envelope,
        source_body_digest=selection_digest,
        source_body_materialized=True,
    )


def build_working_selection_capture(
    *,
    store: CaptureObjectStoreProtocol,
    actor_id: str,
    claim_id: str,
    rationale: str,
    observed_at: datetime,
    accepted_coordinate: AcceptedCoordinate,
    source_id: str,
    coordinate: object,
    selector: object,
    selected_content: bytes,
) -> DirectCaptureBuildResult:
    """Commit the bounded bytes from one typed proposer-observed working selection.

    The whole source is deliberately absent. The coordinate and selector record
    exactly what the client observed, while the retained commitment is only over
    the selected bytes that the daemon can reproduce.
    """

    contract = foreign_source_capture_contract(source_id)
    if not selected_content:
        raise CaptureFormatError("working source selection must retain at least one byte")
    if len(selected_content) > contract.selection_budget.max_bytes:
        raise CaptureFormatError("working source selection exceeds its accepted byte budget")
    stored = store.store(selected_content)
    binding_digest = _direct_binding_digest(
        actor_id=actor_id,
        accepted_coordinate=accepted_coordinate,
    )
    contract_digest_value = capture_contract_digest(contract).tagged
    receipt_digest = typed_digest(
        Sha256Value,
        "playbill-working-selection-capture-receipt-v1",
        {
            "actor_id": actor_id,
            "claim_id": claim_id,
            "coordinate": coordinate,
            "observed_at": _canonical_datetime(observed_at),
            "rationale": rationale,
            "selected_bytes_digest": stored.digest,
            "selector": selector,
            "source_id": source_id,
        },
    ).tagged
    envelope = CaptureEnvelopeV1(
        capture_contract_digest=contract_digest_value,
        source=ExternalSourceReferenceV1(
            source_identity=source_id,
            producer_binding_digest=binding_digest,
            coordinate_type=FOREIGN_SOURCE_COORDINATE_TYPE,
            coordinate=coordinate,
            selector_type=FOREIGN_SOURCE_SELECTOR_TYPE,
            selector={"claim_id": claim_id, "working_selection": selector},
            replayability="attested_only",
        ),
        commitment=EvidenceCommitmentV1(
            digest_kind="exact_bytes",
            digest=stored.digest,
            byte_length=len(selected_content),
            materialization="cas",
        ),
        run_coordinate=CaptureRunCoordinateV1(
            run_kind="provider",
            run_id=f"foreign-source:{claim_id.casefold()}",
            bound_generation=accepted_coordinate.generation_root,
            executable_identity=contract.identity,
            executable_digest=contract_digest_value,
        ),
        run_receipt_digest=receipt_digest,
        producer=ArtifactIdentity(kind="Principal", name=actor_id),
        producer_binding_digest=binding_digest,
        observed_at=observed_at,
    )
    return _store_capture_envelope(
        store=store,
        contract=contract,
        envelope=envelope,
        source_body_digest=stored.digest,
        source_body_materialized=True,
    )


def _store_general_capture(
    *,
    store: CaptureObjectStoreProtocol,
    contract: CaptureContractV1,
    envelope: CaptureEnvelopeV1,
    source_body_materialized: bool,
) -> CaptureBuildResult:
    metadata = store.store(render_capture_envelope(envelope))
    expected = capture_digest(envelope).tagged
    if metadata.digest != expected:
        raise PlaybillCasError("Capture envelope CAS digest did not reproduce")
    return CaptureBuildResult(
        contract_digest=capture_contract_digest(contract).tagged,
        envelope=envelope,
        capture_digest=expected,
        commitment_digest=envelope.commitment.digest,
        source_body_materialized=source_body_materialized,
    )


def build_cas_capture(
    *,
    store: CaptureObjectStoreProtocol,
    contract: CaptureContractV1,
    source_body: bytes,
    run_coordinate: CaptureRunCoordinateV1,
    run_receipt_digest: str,
    producer: ArtifactIdentity,
    producer_binding_digest: str,
    observed_at: datetime,
    source_effective_time: SourceEffectiveTimeV1 | None = None,
) -> CaptureBuildResult:
    """Create an exact bounded CAS observation under one accepted contract."""

    if "cas" not in contract.allowed_source_kinds or "cas" not in (
        contract.allowed_materialization_modes
    ):
        raise CaptureFormatError("CaptureContract does not permit CAS observations")
    if len(source_body) > contract.selection_budget.max_bytes:
        raise CaptureFormatError("CAS observation exceeds its CaptureContract byte budget")
    body = store.store(source_body)
    envelope = CaptureEnvelopeV1(
        capture_contract_digest=capture_contract_digest(contract).tagged,
        source=CasSourceReferenceV1(content_digest=body.digest),
        commitment=EvidenceCommitmentV1(
            digest_kind="exact_bytes",
            digest=body.digest,
            byte_length=len(source_body),
            materialization="cas",
        ),
        run_coordinate=run_coordinate,
        run_receipt_digest=run_receipt_digest,
        producer=producer,
        producer_binding_digest=producer_binding_digest,
        observed_at=observed_at,
        source_effective_time=source_effective_time,
    )
    return _store_general_capture(
        store=store,
        contract=contract,
        envelope=envelope,
        source_body_materialized=True,
    )


def build_coordinator_self_source_capture(
    *,
    store: CaptureObjectStoreProtocol,
    actor_id: str,
    claim_id: str,
    body: bytes,
    observed_at: datetime,
    accepted_coordinate: AcceptedCoordinate,
) -> CaptureBuildResult:
    """Retain exact self-source bytes before minting their coordinator envelope."""

    governance_identifier(actor_id, label="coordinator self-source actor")
    if not re.fullmatch(r"CLM-[0-9a-f]{32}", claim_id):
        raise CaptureFormatError("coordinator self-source requires a Claim identity")
    contract = COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT
    contract_digest_value = capture_contract_digest(contract).tagged
    body_digest = CasDigest(hashlib.sha256(body).hexdigest()).tagged
    producer_binding_digest = typed_digest(
        Sha256Value,
        "playbill-coordinator-self-source-binding-v1",
        {
            "actor_id": actor_id,
            "claim_id": claim_id,
            "generation_root": accepted_coordinate.generation_root,
        },
    ).tagged
    receipt_digest = typed_digest(
        Sha256Value,
        "playbill-coordinator-self-source-receipt-v1",
        {
            "actor_id": actor_id,
            "body_digest": body_digest,
            "claim_id": claim_id,
            "observed_at": _canonical_datetime(observed_at),
        },
    ).tagged
    return build_cas_capture(
        store=store,
        contract=contract,
        source_body=body,
        run_coordinate=CaptureRunCoordinateV1(
            run_kind="provider",
            run_id=f"coordinator-self-source:{claim_id.casefold()}",
            bound_generation=accepted_coordinate.generation_root,
            executable_identity=contract.identity,
            executable_digest=contract_digest_value,
        ),
        run_receipt_digest=receipt_digest,
        producer=ArtifactIdentity(kind="Principal", name=actor_id),
        producer_binding_digest=producer_binding_digest,
        observed_at=observed_at,
    )


def build_ledger_capture(
    *,
    store: CaptureObjectStoreProtocol,
    contract: CaptureContractV1,
    source: LedgerSourceReferenceV1,
    source_body: bytes,
    run_coordinate: CaptureRunCoordinateV1,
    run_receipt_digest: str,
    producer: ArtifactIdentity,
    producer_binding_digest: str,
    observed_at: datetime,
    source_effective_time: SourceEffectiveTimeV1 | None = None,
) -> CaptureBuildResult:
    """Bind exact accepted ledger bytes without copying them into the body CAS."""

    if "ledger" not in contract.allowed_source_kinds or "ledger" not in (
        contract.allowed_materialization_modes
    ):
        raise CaptureFormatError("CaptureContract does not permit ledger observations")
    if len(source_body) > contract.selection_budget.max_bytes:
        raise CaptureFormatError("ledger observation exceeds its CaptureContract byte budget")
    body_digest = CasDigest(hashlib.sha256(source_body).hexdigest()).tagged
    envelope = CaptureEnvelopeV1(
        capture_contract_digest=capture_contract_digest(contract).tagged,
        source=source,
        commitment=EvidenceCommitmentV1(
            digest_kind="exact_bytes",
            digest=body_digest,
            byte_length=len(source_body),
            materialization="ledger",
        ),
        run_coordinate=run_coordinate,
        run_receipt_digest=run_receipt_digest,
        producer=producer,
        producer_binding_digest=producer_binding_digest,
        observed_at=observed_at,
        source_effective_time=source_effective_time,
    )
    return _store_general_capture(
        store=store,
        contract=contract,
        envelope=envelope,
        source_body_materialized=False,
    )


def build_derived_cas_capture(
    *,
    store: CaptureObjectStoreProtocol,
    contract: CaptureContractV1,
    output_body: bytes,
    manifest: InputReceiptSetManifestV1,
    reducer_digest: str,
    run_coordinate: CaptureRunCoordinateV1,
    run_receipt_digest: str,
    producer: ArtifactIdentity,
    producer_binding_digest: str,
    observed_at: datetime,
    source_effective_time: SourceEffectiveTimeV1 | None = None,
) -> CaptureBuildResult:
    """Create a derived Capture with an exact content-addressed input receipt set."""

    if contract.epistemic_grade != "derived":
        raise CaptureFormatError("only a derived CaptureContract may build a derived Capture")
    ArtifactDigest.from_tagged(reducer_digest)
    if len(output_body) > contract.selection_budget.max_bytes:
        raise CaptureFormatError("derived output exceeds its CaptureContract byte budget")
    manifest_bytes = render_input_receipt_set_manifest(manifest)
    stored_manifest = store.store(manifest_bytes)
    expected_manifest = input_receipt_set_manifest_digest(manifest).tagged
    if stored_manifest.digest != expected_manifest:
        raise PlaybillCasError("input receipt-set manifest CAS digest did not reproduce")
    output = store.store(output_body)
    envelope = CaptureEnvelopeV1(
        capture_contract_digest=capture_contract_digest(contract).tagged,
        source=CasSourceReferenceV1(content_digest=output.digest),
        commitment=EvidenceCommitmentV1(
            digest_kind="exact_bytes",
            digest=output.digest,
            byte_length=len(output_body),
            materialization="cas",
        ),
        run_coordinate=run_coordinate,
        run_receipt_digest=run_receipt_digest,
        producer=producer,
        producer_binding_digest=producer_binding_digest,
        observed_at=observed_at,
        source_effective_time=source_effective_time,
        reducer_digest=reducer_digest,
        input_receipt_set_manifest_digest=expected_manifest,
    )
    return _store_general_capture(
        store=store,
        contract=contract,
        envelope=envelope,
        source_body_materialized=True,
    )


def verify_capture(
    digest: str,
    *,
    store: CaptureObjectStoreProtocol,
    contract: CaptureContractV1,
    ledger_resolver: LedgerMaterialResolverProtocol | None = None,
    producer_artifact_digests: Mapping[str, str] | None = None,
) -> CaptureEnvelopeV1:
    """Replay one envelope and every proof available for its source kind."""

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
    executable = envelope.run_coordinate.executable_identity
    if executable == contract.identity:
        if envelope.run_coordinate.executable_digest != expected_contract:
            raise CaptureFormatError("Capture run coordinate differs from its contract")
    elif executable == envelope.producer:
        if (
            executable.kind != "Provider"
            or producer_artifact_digests is None
            or (
                producer_artifact_digests.get(executable.qualified)
                != envelope.run_coordinate.executable_digest
            )
        ):
            raise CaptureFormatError("Capture producer does not resolve at its exact digest")
    else:
        raise CaptureFormatError("Capture executable is neither its contract nor producer")
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
        elif envelope.source.coordinate_type not in {
            pin.target.name for pin in contract.coordinate_schema_pins
        } or envelope.source.selector_type not in {
            pin.target.name for pin in contract.selector_schema_pins
        }:
            raise CaptureFormatError("external Capture source schemas are not exactly pinned")
        if envelope.commitment.materialization == "cas":
            if not store.verify(envelope.commitment.digest):
                raise CaptureFormatError("bounded external Capture material is unavailable")
            material = store.read(
                envelope.commitment.digest,
                access=BodyAccessContext(
                    principal_id="playbill-compiler",
                    can_read_body=True,
                ),
            )
            if len(material) > contract.selection_budget.max_bytes:
                raise CaptureFormatError("external Capture material exceeds its contract")
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
        if len(source_bytes) > contract.selection_budget.max_bytes:
            raise CaptureFormatError("Capture source exceeds its contract byte budget")
    if isinstance(envelope.source, LedgerSourceReferenceV1):
        if ledger_resolver is None:
            raise CaptureFormatError("ledger Capture verification requires exact ledger bytes")
        source_bytes = ledger_resolver.read_ledger_source(envelope.source)
        if CasDigest(hashlib.sha256(source_bytes).hexdigest()).tagged != (
            envelope.commitment.digest
        ):
            raise CaptureFormatError("ledger Capture bytes differ from their commitment")
        if len(source_bytes) != envelope.commitment.byte_length:
            raise CaptureFormatError("ledger Capture byte length does not reproduce")
    if contract.epistemic_grade != "derived" and (
        envelope.reducer_digest is not None
        or envelope.input_receipt_set_manifest_digest is not None
    ):
        raise CaptureFormatError("non-derived Capture cannot carry derivation receipts")
    if contract.epistemic_grade == "derived":
        manifest_digest = envelope.input_receipt_set_manifest_digest
        if envelope.reducer_digest is None or manifest_digest is None:
            raise CaptureFormatError("derived Capture is missing reducer receipt-set proof")
        if not store.verify(manifest_digest):
            raise CaptureFormatError("derived Capture input receipt-set is unavailable")
        manifest_bytes = store.read(
            manifest_digest,
            access=BodyAccessContext(principal_id="playbill-compiler", can_read_body=True),
        )
        try:
            manifest = InputReceiptSetManifestV1.model_validate_json(manifest_bytes)
        except ValidationError as exc:
            raise CaptureFormatError("derived Capture receipt-set manifest is invalid") from exc
        if render_input_receipt_set_manifest(manifest) != manifest_bytes or (
            input_receipt_set_manifest_digest(manifest).tagged != manifest_digest
        ):
            raise CaptureFormatError("derived Capture receipt-set manifest does not reproduce")
    return envelope


__all__ = [
    "AcceptedCaptureContract",
    "CanonicalDurationV1",
    "CaptureContractLawResult",
    "CaptureContractV1",
    "CaptureBuildResult",
    "CaptureComponentRegistry",
    "CaptureEnvelopeV1",
    "CaptureFormatError",
    "CaptureObjectStoreProtocol",
    "CaptureRetentionErasurePolicyV1",
    "CaptureRunCoordinateV1",
    "CaptureSelectionBudgetV1",
    "COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT",
    "COORDINATOR_SELF_SOURCE_CONTRACT_ID",
    "COORDINATOR_SELF_SOURCE_IDENTITY",
    "DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT",
    "DIRECT_SELF_ASSERTED_CONTRACT_ID",
    "DIRECT_EXTERNAL_COORDINATE_TYPES",
    "DIRECT_EXTERNAL_SELECTOR_TYPES",
    "FOREIGN_SOURCE_CONTRACT_PREFIX",
    "FOREIGN_SOURCE_COORDINATE_TYPE",
    "FOREIGN_SOURCE_MAX_BYTES",
    "FOREIGN_SOURCE_PROVENANCE_RULE",
    "FOREIGN_SOURCE_REPLAY_POLICY",
    "FOREIGN_SOURCE_SELECTOR_TYPE",
    "FOREIGN_SOURCE_SUBJECT_MAPPING",
    "PLAYBILL_CAPTURE_COMPONENTS",
    "DirectCaptureBuildResult",
    "DirectByteSpanSelectionV1",
    "DirectClaimSelectionV1",
    "DirectClaimSourceV1",
    "DirectExternalSelectionV1",
    "DirectForeignSourceSelectionV1",
    "InputReceiptSetManifestV1",
    "LedgerMaterialResolverProtocol",
    "SourceEffectiveTimeV1",
    "build_direct_claim_capture",
    "build_direct_claim_selection_capture",
    "build_coordinator_self_source_capture",
    "build_cas_capture",
    "build_derived_cas_capture",
    "build_foreign_source_capture",
    "build_working_selection_capture",
    "build_ledger_capture",
    "capture_contract_digest",
    "capture_contract_is_self_asserted",
    "capture_is_coordinator_self_source",
    "capture_is_direct_self_source",
    "capture_contract_path",
    "capture_component_pin",
    "capture_digest",
    "foreign_source_capture_contract",
    "foreign_source_contract_id",
    "evaluate_capture_contract_law",
    "parse_capture_contract",
    "parse_capture_envelope",
    "input_receipt_set_manifest_digest",
    "render_input_receipt_set_manifest",
    "render_capture_contract",
    "render_capture_envelope",
    "validate_capture_contract_path",
    "verify_capture",
]
