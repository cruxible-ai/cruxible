"""Capture contracts and the bounded direct-authoring path."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Final, Literal, Protocol, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from cruxible_client.contracts.artifacts import (
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactNameRef,
    ArtifactPin,
)
from cruxible_client.contracts.canonical import (
    CURRENT_ARTIFACT_CODEC,
    ArtifactCodec,
    ArtifactDigest,
    CasDigest,
    Sha256Value,
    artifact_bytes_for_path,
    artifact_path_for_codec,
    artifact_path_matches,
    canonical_bytes,
    model_canonical_bytes,
    normalize_canonical,
    pretty_canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.capture_erasure import CaptureErasureRecord, RecordedCaptureErasure
from cruxible_client.contracts.cas_contracts import (
    BodyAccessContext,
    CasObjectMetadata,
)
from cruxible_client.contracts.content_memo import ParseMemo, sealed_memo
from cruxible_client.contracts.diagnostics import CompilerDiagnostic
from cruxible_client.contracts.errors import CasError, FormatError
from cruxible_client.contracts.governance import PermissionTier, governance_identifier
from cruxible_client.contracts.provider_execution import (
    ProviderEgressObservation,
    ProviderExternalOccurrencePlan,
    ProviderInvocationOutputDigest,
    ProviderInvocationReceipt,
    ProviderSecretBindingIdentity,
    ProviderSecretReceiptReference,
    ProviderSecretReference,
    provider_external_occurrence_plan_digest,
    provider_invocation_output_digest,
    provider_invocation_output_digest_of_bytes,
    provider_invocation_receipt_digest,
    provider_secret_binding_identity_digest,
)
from cruxible_client.contracts.semantic import ContentSpan, SemanticAddress
from cruxible_client.contracts.source_references import (
    CasSourceReference,
    EvidenceCommitment,
    ExternalSourceReference,
    LedgerSourceReference,
    SourceReference,
    validate_source_commitment,
)
from cruxible_client.contracts.workspace_file import (
    WORKSPACE_FILE_INTERFACE_DIGESTS,
    SourceReadReceipt,
    source_read_receipt_digest,
)

if TYPE_CHECKING:
    from cruxible_client.contracts.projection import AcceptedCoordinate

_CONTRACT_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")
_RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,255}$")
_RUN_ID_V2_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")

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


class CaptureFormatError(FormatError):
    """A Capture contract/envelope or its canonical location is invalid."""


class CaptureObservedContentMismatch(CaptureFormatError):
    """A Source result's stated content digest is not the digest of the content it carries."""

    error_code = "cruxible.capture.observed_content_mismatch"


#: The ``web.fetch`` v3 operation definition digest (``cruxible-provider-web`` 0.2.x):
#: its material carries the origin's body (``retrieved.body_base64``) and its
#: digest (``retrieved.body_sha256``), which core recomputes before trusting.
WEB_FETCH_V3_INTERFACE_DIGEST: Final = (
    "sha256:f09e1553b05d72a9e58ed3a260417c41992ccd34b2f2541958c5733723ac2121"
)


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


class CanonicalDuration(_StrictCaptureModel):
    tag: Literal["playbill-duration-v1"] = "playbill-duration-v1"
    microseconds: int = Field(ge=0)


class CaptureSelectionBudget(_StrictCaptureModel):
    tag: Literal["playbill-capture-selection-budget-v1"] = "playbill-capture-selection-budget-v1"
    max_bytes: int = Field(ge=1)
    max_rows: int = Field(ge=1)
    max_items: int = Field(ge=1)


class CaptureRetentionErasurePolicy(_StrictCaptureModel):
    tag: Literal["playbill-capture-retention-erasure-policy-v1"] = (
        "playbill-capture-retention-erasure-policy-v1"
    )
    body_retention: Literal[
        "never_materialize",
        "optional",
        "required_for_duration",
    ]
    minimum_retention: CanonicalDuration | None = None
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
    def _policy_shape(self) -> "CaptureRetentionErasurePolicy":
        required = self.body_retention == "required_for_duration"
        if required != (self.minimum_retention is not None):
            raise ValueError("required_for_duration requires exactly one minimum_retention")
        if required and self.minimum_retention is not None:
            if self.minimum_retention.microseconds == 0:
                raise ValueError("required body retention duration must be nonzero")
        authorized = self.erasure == "authorized_by_rule"
        if authorized != (self.erasure_rule_digest is not None):
            raise ValueError(
                "authorized erasure requires exactly one registered erasure-rule digest; "
                "build the contract with authorize_erasure(contract)"
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
    selection_budget: CaptureSelectionBudget
    retention_erasure_policy: CaptureRetentionErasurePolicy
    replay_policy_digest: str
    epistemic_grade: Literal["observed", "derived", "predicted"]
    provenance_rule_digest: str
    evidence_kinds: tuple[str, ...]
    source_subject_mapping_digest: str
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


CaptureShape = Literal["http_response", "procedure_output", "foreign_span", "database_snapshot"]
CaptureReplay = Literal["exact", "attested"]


class CaptureContractComponents(_StrictCaptureModel):
    coordinate_schema_pins: tuple[ArtifactPin, ...]
    selector_schema_pins: tuple[ArtifactPin, ...]
    commitment_canonicalizer: ArtifactPin
    replay_policy_digest: str
    provenance_rule_digest: str
    source_subject_mapping_digest: str
    pins: tuple[ArtifactPin, ...] = ()

    @field_validator("coordinate_schema_pins", "selector_schema_pins", "pins")
    @classmethod
    def _pins(cls, value: tuple[ArtifactPin, ...], info: object) -> tuple[ArtifactPin, ...]:
        return CaptureContractV1._pins(value, info)

    @field_validator(
        "replay_policy_digest", "provenance_rule_digest", "source_subject_mapping_digest"
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        return CaptureContractV1._digest(value)


class CaptureContract(_StrictCaptureModel):
    """Current stored contract: shape and policy, with frozen expanded components."""

    artifact_format: Literal["cruxible-capture-contract-v2"] = "cruxible-capture-contract-v2"
    identity: ArtifactIdentity
    shape: CaptureShape | Literal["coordinator_self_source"]
    replay: CaptureReplay
    components: CaptureContractComponents
    selection_budget: CaptureSelectionBudget
    retention_erasure_policy: CaptureRetentionErasurePolicy
    epistemic_grade: Literal["observed", "derived", "predicted"] = "observed"
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()

    @model_validator(mode="after")
    def _shape(self) -> "CaptureContract":
        if self.identity.kind != "CaptureContract" or not _CONTRACT_ID_RE.fullmatch(
            self.identity.name
        ):
            raise ValueError(
                "CaptureContract identity must name a path-addressable CaptureContract"
            )
        if (
            self.shape == "coordinator_self_source"
            and self.identity.name != COORDINATOR_SELF_SOURCE_CONTRACT_ID
        ):
            raise ValueError(
                "coordinator self-source is a reserved constant, not an authored shape"
            )
        if self.epistemic_grade == "derived" and self.shape != "procedure_output":
            raise ValueError("derived grade requires procedure_output")
        return self

    @property
    def coordinate_schema_pins(self) -> tuple[ArtifactPin, ...]:
        return self.components.coordinate_schema_pins

    @property
    def selector_schema_pins(self) -> tuple[ArtifactPin, ...]:
        return self.components.selector_schema_pins

    @property
    def commitment_canonicalizer(self) -> ArtifactPin:
        return self.components.commitment_canonicalizer

    @property
    def replay_policy_digest(self) -> str:
        return self.components.replay_policy_digest

    @property
    def provenance_rule_digest(self) -> str:
        return self.components.provenance_rule_digest

    @property
    def source_subject_mapping_digest(self) -> str:
        return self.components.source_subject_mapping_digest

    @property
    def pins(self) -> tuple[ArtifactPin, ...]:
        return self.components.pins


class NamedCaptureComponents(_StrictCaptureModel):
    coordinate_schemas: tuple[ArtifactNameRef, ...]
    selector_schemas: tuple[ArtifactNameRef, ...]
    commitment_canonicalizer: ArtifactNameRef
    replay_policy: ArtifactNameRef
    provenance_rule: ArtifactNameRef
    source_subject_mapping: ArtifactNameRef
    coordinate_grammar: ArtifactNameRef | None = None
    proof_adapter: ArtifactNameRef | None = None


class NamedCaptureRetentionPolicy(_StrictCaptureModel):
    body_retention: Literal["never_materialize", "optional", "required_for_duration"]
    minimum_retention: CanonicalDuration | None = None
    erasure: Literal["prohibited", "authorized_by_rule"]
    erasure_rule: ArtifactNameRef | None = None
    selector_privacy: Literal["direct_allowed", "pseudonymous_required"]


class NamedCaptureContract(_StrictCaptureModel):
    artifact_format: Literal["cruxible-capture-contract-v3"] = "cruxible-capture-contract-v3"
    identity: ArtifactIdentity
    shape: CaptureShape | Literal["coordinator_self_source"]
    replay: CaptureReplay
    components: NamedCaptureComponents
    selection_budget: CaptureSelectionBudget
    retention_erasure_policy: NamedCaptureRetentionPolicy
    epistemic_grade: Literal["observed", "derived", "predicted"] = "observed"
    lifecycle: ArtifactLifecycle = ArtifactLifecycle()

    @model_validator(mode="after")
    def _shape(self) -> NamedCaptureContract:
        if self.identity.kind != "CaptureContract" or not _CONTRACT_ID_RE.fullmatch(
            self.identity.name
        ):
            raise ValueError("CaptureContract identity must be path-addressable")
        if (
            self.shape == "coordinator_self_source"
            and self.identity.name != COORDINATOR_SELF_SOURCE_CONTRACT_ID
        ):
            raise ValueError("coordinator self-source is reserved")
        if self.epistemic_grade == "derived" and self.shape != "procedure_output":
            raise ValueError("derived grade requires procedure_output")
        capture_components(self)
        capture_retention_policy(self)
        return self


CaptureContractAny = CaptureContractV1 | CaptureContract | NamedCaptureContract


def capture_components(contract: CaptureContractAny) -> CaptureContractComponents:
    """Interpret named compiler components; historical stored digests stay exact."""
    if isinstance(contract, CaptureContract):
        return contract.components
    if isinstance(contract, CaptureContractV1):
        return CaptureContractComponents.model_validate(
            {name: getattr(contract, name) for name in CaptureContractComponents.model_fields}
        )
    components = contract.components

    def pin(role: str, ref: ArtifactNameRef) -> ArtifactPin:
        value = capture_component_pin(role, ref.target.name)
        if ref.target.kind != "Contract" or not CAPTURE_COMPONENTS.resolves(value):
            raise CaptureFormatError(f"unknown compiler Capture component: {ref.target.qualified}")
        return value

    rules = [
        pin("replay-policy", components.replay_policy),
        pin("provenance-rule", components.provenance_rule),
        pin("source-subject-mapping", components.source_subject_mapping),
    ]
    for role, ref in (
        ("coordinate-grammar", components.coordinate_grammar),
        ("proof-adapter", components.proof_adapter),
        ("erasure-rule", contract.retention_erasure_policy.erasure_rule),
    ):
        if ref is not None:
            rules.append(pin(role, ref))
    return CaptureContractComponents(
        coordinate_schema_pins=tuple(
            pin("coordinate-schema", r) for r in components.coordinate_schemas
        ),
        selector_schema_pins=tuple(pin("selector-schema", r) for r in components.selector_schemas),
        commitment_canonicalizer=pin(
            "commitment-canonicalizer", components.commitment_canonicalizer
        ),
        replay_policy_digest=rules[0].artifact_digest,
        provenance_rule_digest=rules[1].artifact_digest,
        source_subject_mapping_digest=rules[2].artifact_digest,
        pins=tuple(sorted(rules, key=_pin_key)),
    )


def capture_retention_policy(contract: CaptureContractAny) -> CaptureRetentionErasurePolicy:
    if not isinstance(contract, NamedCaptureContract):
        return contract.retention_erasure_policy
    value = contract.retention_erasure_policy
    data = value.model_dump(mode="python", exclude={"erasure_rule"})
    if value.erasure_rule is not None:
        ref = value.erasure_rule
        rule = capture_component_pin("erasure-rule", ref.target.name)
        if ref.target.kind != "Contract" or not CAPTURE_COMPONENTS.resolves(rule):
            raise CaptureFormatError("unknown compiler Capture erasure rule")
        data["erasure_rule_digest"] = rule.artifact_digest
    return CaptureRetentionErasurePolicy.model_validate(data)


def name_capture_contract(contract: CaptureContract) -> NamedCaptureContract:
    components = contract.components

    def ref(pin: ArtifactPin) -> ArtifactNameRef:
        return ArtifactNameRef(target=pin.target)

    def rule(role: str) -> ArtifactNameRef | None:
        return next((ref(pin) for pin in components.pins if pin.role == role), None)

    def required(role: str) -> ArtifactNameRef:
        result = rule(role)
        if result is None:
            raise CaptureFormatError(f"CaptureContract lacks {role}")
        return result

    named = NamedCaptureComponents(
        coordinate_schemas=tuple(map(ref, components.coordinate_schema_pins)),
        selector_schemas=tuple(map(ref, components.selector_schema_pins)),
        commitment_canonicalizer=ref(components.commitment_canonicalizer),
        replay_policy=required("replay-policy"),
        provenance_rule=required("provenance-rule"),
        source_subject_mapping=required("source-subject-mapping"),
        coordinate_grammar=rule("coordinate-grammar"),
        proof_adapter=rule("proof-adapter"),
    )
    policy = contract.retention_erasure_policy.model_dump(
        mode="python", exclude={"tag", "erasure_rule_digest"}
    )
    policy["erasure_rule"] = rule("erasure-rule")
    return NamedCaptureContract.model_validate(
        {
            **contract.model_dump(
                mode="python", exclude={"artifact_format", "components", "retention_erasure_policy"}
            ),
            "components": named,
            "retention_erasure_policy": policy,
        }
    )


def contract_source_kinds(contract: CaptureContractAny) -> tuple[str, ...]:
    if isinstance(contract, CaptureContractV1):
        return contract.allowed_source_kinds
    return {
        "http_response": ("external",),
        "procedure_output": ("cas",),
        "foreign_span": ("external",),
        "database_snapshot": ("external", "ledger"),
        "coordinator_self_source": ("cas",),
    }[contract.shape]


def contract_materialization_modes(contract: CaptureContractAny) -> tuple[str, ...]:
    if isinstance(contract, CaptureContractV1):
        return contract.allowed_materialization_modes
    return ("cas", "ledger") if contract.shape == "database_snapshot" else ("cas",)


def capture_shape_components(
    shape: CaptureShape, replay: CaptureReplay
) -> CaptureContractComponents:
    """Lower the closed engine vocabulary once; accepted bytes keep the expansion."""
    coordinate, selector, canonicalizer, provenance, mapping = {
        "http_response": (
            ("http-response-v1",),
            ("whole-response-v1",),
            "canonical-json-v1",
            "playbill.external.daemon-fetched-v1",
            "playbill.external.record-subject-v1",
        ),
        "procedure_output": (
            (),
            (),
            "canonical-json-v1",
            "playbill.external.daemon-fetched-v1",
            "playbill.external.record-subject-v1",
        ),
        "foreign_span": (
            (FOREIGN_SOURCE_COORDINATE_TYPE,),
            (FOREIGN_SOURCE_SELECTOR_TYPE,),
            "sha256-bytes-v1",
            FOREIGN_SOURCE_PROVENANCE_RULE,
            FOREIGN_SOURCE_SUBJECT_MAPPING,
        ),
        "database_snapshot": (
            ("database-snapshot-v1",),
            ("query-result-v1", "relation-primary-key-v1"),
            "canonical-json-v1",
            "playbill.external.daemon-fetched-v1",
            "playbill.external.record-subject-v1",
        ),
    }[shape]
    replay_name = (
        "playbill.external.exact-replay-v1" if replay == "exact" else FOREIGN_SOURCE_REPLAY_POLICY
    )
    pins = [
        capture_component_pin("replay-policy", replay_name),
        capture_component_pin("provenance-rule", provenance),
        capture_component_pin("source-subject-mapping", mapping),
    ]
    if shape == "database_snapshot":
        pins.extend(
            (
                capture_component_pin(
                    "coordinate-grammar", "playbill.database-snapshot-coordinate-v1"
                ),
                capture_component_pin("proof-adapter", "playbill.database-snapshot-proof-v1"),
            )
        )
    return CaptureContractComponents(
        coordinate_schema_pins=tuple(
            capture_component_pin("coordinate-schema", name) for name in coordinate
        ),
        selector_schema_pins=tuple(
            capture_component_pin("selector-schema", name) for name in selector
        ),
        commitment_canonicalizer=capture_component_pin("commitment-canonicalizer", canonicalizer),
        replay_policy_digest=pins[0].artifact_digest,
        provenance_rule_digest=pins[1].artifact_digest,
        source_subject_mapping_digest=pins[2].artifact_digest,
        pins=tuple(sorted(pins, key=_pin_key)),
    )


def _light_builtin(
    contract: CaptureContractV1,
    *,
    shape: CaptureShape | Literal["coordinator_self_source"],
    replay: CaptureReplay,
) -> CaptureContract:
    return CaptureContract(
        identity=contract.identity,
        shape=shape,
        replay=replay,
        components=CaptureContractComponents.model_validate(
            {name: getattr(contract, name) for name in CaptureContractComponents.model_fields}
        ),
        selection_budget=contract.selection_budget,
        retention_erasure_policy=capture_retention_policy(contract),
        epistemic_grade=contract.epistemic_grade,
        lifecycle=contract.lifecycle,
    )


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
    ("coordinate-schema", "http-response-v1"),
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
    ("selector-schema", "whole-response-v1"),
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


CAPTURE_COMPONENTS = CaptureComponentRegistry(
    tuple(capture_component_pin(role, name) for role, name in _CAPTURE_COMPONENT_PIN_NAMES)
)

_ERASURE_RULE_PIN = capture_component_pin("erasure-rule", "playbill.capture-erasure-authority-v1")


def authorize_erasure(contract: CaptureContractAny) -> CaptureContractAny:
    """Return ``contract`` declaring that its Captures' retained bodies may be erased.

    This is the one way to opt a CaptureContract into ``cruxible capture erase``:
    it sets the retention policy's ``erasure`` to ``authorized_by_rule`` with the
    registered erasure rule's digest and adds that rule's pin, the exact pair the
    contract law requires. Every other field is kept, and a contract that already
    authorizes erasure comes back unchanged. The declaration is fixed for the
    contract's identity, so apply it before the contract is first accepted; an
    accepted contract that prohibits erasure needs a successor under a new name.
    """

    if isinstance(contract, NamedCaptureContract):
        named_policy = contract.retention_erasure_policy.model_copy(
            update={
                "erasure": "authorized_by_rule",
                "erasure_rule": ArtifactNameRef(target=_ERASURE_RULE_PIN.target),
            }
        )
        return NamedCaptureContract.model_validate(
            {
                **contract.model_dump(mode="python"),
                "retention_erasure_policy": named_policy,
            }
        )
    policy = capture_retention_policy(contract).model_copy(
        update={
            "erasure": "authorized_by_rule",
            "erasure_rule_digest": _ERASURE_RULE_PIN.artifact_digest,
        }
    )
    pins = tuple(
        sorted(
            {
                _pin_key(pin): pin
                for pin in (*capture_components(contract).pins, _ERASURE_RULE_PIN)
            }.values(),
            key=_pin_key,
        )
    )
    if isinstance(contract, CaptureContract):
        return contract.model_copy(
            update={
                "retention_erasure_policy": policy,
                "components": contract.components.model_copy(update={"pins": pins}),
            }
        )
    return CaptureContractV1.model_validate(
        {
            **contract.model_dump(mode="python"),
            "retention_erasure_policy": named_policy,
            "pins": pins,
        }
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
    selection_budget=CaptureSelectionBudget(
        max_bytes=1024 * 1024,
        max_rows=1,
        max_items=1,
    ),
    retention_erasure_policy=CaptureRetentionErasurePolicy(
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

COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT_V1 = CaptureContractV1(
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
    selection_budget=CaptureSelectionBudget(
        max_bytes=1024 * 1024,
        max_rows=1,
        max_items=1,
    ),
    retention_erasure_policy=CaptureRetentionErasurePolicy(
        body_retention="optional",
        erasure="prohibited",
        selector_privacy="direct_allowed",
    ),
    replay_policy_digest=_COORDINATOR_REPLAY_PIN.artifact_digest,
    epistemic_grade="observed",
    provenance_rule_digest=_COORDINATOR_PROVENANCE_PIN.artifact_digest,
    evidence_kinds=("self_asserted",),
    source_subject_mapping_digest=_COORDINATOR_MAPPING_PIN.artifact_digest,
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


def foreign_source_capture_contract_v1(logical_source_identity: str) -> CaptureContractV1:
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
        selection_budget=CaptureSelectionBudget(
            max_bytes=FOREIGN_SOURCE_MAX_BYTES,
            max_rows=1,
            max_items=1,
        ),
        retention_erasure_policy=CaptureRetentionErasurePolicy(
            body_retention="optional",
            erasure="prohibited",
            selector_privacy="direct_allowed",
        ),
        replay_policy_digest=replay_pin.artifact_digest,
        epistemic_grade="observed",
        provenance_rule_digest=provenance_pin.artifact_digest,
        evidence_kinds=("self_asserted",),
        source_subject_mapping_digest=mapping_pin.artifact_digest,
        pins=tuple(sorted((replay_pin, provenance_pin, mapping_pin), key=_pin_key)),
    )


COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT = _light_builtin(
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT_V1, shape="coordinator_self_source", replay="exact"
)
COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACTS = (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT_V1,
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    name_capture_contract(COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT),
)


def foreign_source_capture_contract(logical_source_identity: str) -> NamedCaptureContract:
    return name_capture_contract(
        _light_builtin(
            foreign_source_capture_contract_v1(logical_source_identity),
            shape="foreign_span",
            replay="attested",
        )
    )


@sealed_memo
def is_coordinator_self_source_contract(contract: CaptureContractAny) -> bool:
    if contract.identity.name != COORDINATOR_SELF_SOURCE_CONTRACT_ID:
        return False
    return (
        contract.model_copy(
            update={"lifecycle": contract.lifecycle.model_copy(update={"predecessor_digest": None})}
        )
        in COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACTS
    )


def builtin_contract_for_write(
    current: CaptureContract | NamedCaptureContract,
    previous: CaptureContractAny | None,
    *,
    named: bool = True,
) -> CaptureContract | NamedCaptureContract:
    """Install a compatible successor; evidence keeps its exact original version."""
    chosen = (
        (name_capture_contract(current) if isinstance(current, CaptureContract) else current)
        if named
        else current
    )
    if previous is None:
        return chosen
    if isinstance(previous, NamedCaptureContract) and named:
        return previous
    if isinstance(previous, CaptureContract) and not named:
        return previous
    broken = capture_contract_successor_break(previous, chosen)
    if broken is not None or previous.lifecycle.state != "live":
        raise CaptureFormatError(
            f"built-in contract cannot succeed {broken or 'retired profile'}; use a new name"
        )
    return chosen.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=capture_contract_digest(previous).tagged,
            )
        }
    )


_SELF_ASSERTED_PROVENANCE_RULE_DIGESTS: frozenset[str] = frozenset(
    {
        capture_components(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT).provenance_rule_digest,
        capture_components(COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT).provenance_rule_digest,
        capture_component_pin("provenance-rule", FOREIGN_SOURCE_PROVENANCE_RULE).artifact_digest,
    }
)


def capture_contract_is_self_asserted(contract: CaptureContractAny) -> bool:
    """Whether a contract's declared provenance rule is proposer-supplied.

    The grade follows the contract's own registered provenance rule rather than
    an identity comparison against one constant, so a second self-asserted
    contract cannot silently be graded as though a daemon had fetched it. Only a
    contract that pins a registered rule can carry one of these digests -- the
    rule-registry check refuses any other -- so this stays exactly as narrow as
    the identity comparison it generalizes.
    """

    return (
        capture_components(contract).provenance_rule_digest
        in _SELF_ASSERTED_PROVENANCE_RULE_DIGESTS
    )


def capture_contract_path(contract_id: str) -> str:
    if not _CONTRACT_ID_RE.fullmatch(contract_id):
        raise CaptureFormatError("CaptureContract identity is not path-addressable")
    return f"capture-contracts/{contract_id}.json"


def validate_capture_contract_path(
    contract: CaptureContractAny,
    path: str,
    *,
    codec: ArtifactCodec = CURRENT_ARTIFACT_CODEC,
) -> str:
    expected = capture_contract_path(contract.identity.name)
    if not artifact_path_matches(expected, path, codec=codec):
        raise CaptureFormatError(
            f"CaptureContract identity/path disagreement: {contract.identity.qualified!r} "
            f"requires {artifact_path_for_codec(expected, codec)!r}"
        )
    return path


def render_capture_contract(contract: CaptureContractAny) -> bytes:
    return pretty_canonical_bytes(contract.model_dump(mode="json"))


_CAPTURE_CONTRACT_PARSES: ParseMemo[CaptureContractAny] = ParseMemo(
    capacity=1024, max_bytes=16 * 1024 * 1024
)


def parse_capture_contract(
    content: bytes,
    *,
    path: str,
    codec: ArtifactCodec = CURRENT_ARTIFACT_CODEC,
) -> CaptureContractAny:
    """Parse once per exact (path, codec, bytes); see ``content_memo``."""
    return _CAPTURE_CONTRACT_PARSES.parse(
        content, path, codec, lambda: _parse_capture_contract(content, path=path, codec=codec)
    )


def _parse_capture_contract(
    content: bytes,
    *,
    path: str,
    codec: ArtifactCodec = CURRENT_ARTIFACT_CODEC,
) -> CaptureContractAny:
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, ValueError) as exc:
        raise CaptureFormatError("CaptureContract is not strict JSON") from exc
    if not isinstance(payload, dict) or payload.get("artifact_format") not in (
        {
            "playbill-capture-contract-v1",
            "cruxible-capture-contract-v2",
            "cruxible-capture-contract-v3",
        }
    ):
        declared = payload.get("artifact_format") if isinstance(payload, dict) else None
        raise CaptureFormatError(f"unsupported CaptureContract artifact format: {declared!r}")
    try:
        contract: CaptureContractAny = TypeAdapter(CaptureContractAny).validate_python(payload)
    except ValidationError as exc:
        raise CaptureFormatError("CaptureContract failed strict contract validation") from exc
    validate_capture_contract_path(contract, path, codec=codec)
    if artifact_bytes_for_path(render_capture_contract(contract), path, codec=codec) != content:
        raise CaptureFormatError("CaptureContract is not in canonical wire form")
    return contract


@sealed_memo
def capture_contract_digest(contract: CaptureContractAny) -> ArtifactDigest:
    return typed_digest(
        ArtifactDigest,
        "playbill-envelope-v1",
        contract.model_dump(mode="json"),
    )


class AcceptedCaptureContract(_StrictCaptureModel):
    path: str
    contract: CaptureContractAny
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


_EQUAL_ON_SUCCESSION: tuple[str, ...] = (
    "epistemic_grade",
    "commitment_canonicalizer",
    "coordinate_schema_pins",
    "selector_schema_pins",
    "replay_policy_digest",
    "provenance_rule_digest",
    "source_subject_mapping_digest",
    "retention_erasure_policy",
    "pins",
)
_WIDEN_ON_SUCCESSION: tuple[str, ...] = (
    "allowed_source_kinds",
    "allowed_materialization_modes",
    "logical_source_identities",
    "evidence_kinds",
)


def capture_contract_successor_break(
    previous: CaptureContractAny,
    successor: CaptureContractAny,
) -> str | None:
    """Name the first field a live successor changes incompatibly, or None.

    Every accepted version of a contract is admitted by the rules that name its
    identity, so evidence captured under any version must mean the same thing.
    A successor may widen what it accepts and raise its budgets; everything that
    decides what a Capture commits to, how it replays, what its provenance is and
    which subject it binds must stay identical. Anything else is a new identity.
    """

    for name in _EQUAL_ON_SUCCESSION:

        def value(contract: CaptureContractAny) -> object:
            if name in CaptureContractComponents.model_fields:
                return getattr(capture_components(contract), name)
            if name == "retention_erasure_policy":
                return capture_retention_policy(contract)
            return getattr(contract, name)

        if value(previous) != value(successor):
            return name
    if isinstance(previous, CaptureContract | NamedCaptureContract) and isinstance(
        successor, CaptureContractV1
    ):
        return "artifact_format"
    if isinstance(previous, CaptureContract | NamedCaptureContract) and isinstance(
        successor, CaptureContract | NamedCaptureContract
    ):
        if previous.shape != successor.shape or previous.replay != successor.replay:
            return "shape/replay"
    if isinstance(previous, CaptureContractV1) and isinstance(successor, CaptureContractV1):
        for name in _WIDEN_ON_SUCCESSION:
            if not set(getattr(previous, name)).issubset(getattr(successor, name)):
                return name
    elif contract_source_kinds(previous) != contract_source_kinds(successor):
        return "allowed_source_kinds"
    elif contract_materialization_modes(previous) != contract_materialization_modes(successor):
        return "allowed_materialization_modes"
    for name in ("max_bytes", "max_rows", "max_items"):
        if getattr(successor.selection_budget, name) < getattr(previous.selection_budget, name):
            return f"selection_budget.{name}"
    return None


def evaluate_capture_contract_law(
    contract: CaptureContractAny,
    *,
    path: str,
    predecessor: AcceptedCaptureContract | None,
    compatible_succession: bool = False,
) -> CaptureContractLawResult:
    """Evaluate the complete v1 contract without granting source or Claim authority.

    `compatible_succession` is the revision-4 law: a live successor must be
    compatible with its predecessor and a retired contract cannot be revived.
    Retirement is its own transition and is never judged for compatibility.
    """

    try:
        validate_capture_contract_path(contract, path)
    except CaptureFormatError as exc:
        return CaptureContractLawResult(
            verdict="refused",
            diagnostics=(
                _diagnostic("cruxible.capture_contract.path_mismatch", str(exc), path=path),
            ),
        )
    if (
        contract.identity.name == COORDINATOR_SELF_SOURCE_CONTRACT_ID
        and not is_coordinator_self_source_contract(contract)
    ):
        return CaptureContractLawResult(
            verdict="refused",
            diagnostics=(
                _diagnostic(
                    "cruxible.capture_contract.coordinator_profile_mismatch",
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
                    "cruxible.capture_contract.predecessor_missing",
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
                        "cruxible.capture_contract.identity_changed",
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
                        "cruxible.capture_contract.predecessor_mismatch",
                        "A CaptureContract successor must pin the exact predecessor digest.",
                        path=path,
                    ),
                ),
            )
        if compatible_succession and contract.lifecycle.state == "live":
            if predecessor.contract.lifecycle.state == "retired":
                return CaptureContractLawResult(
                    verdict="refused",
                    diagnostics=(
                        _diagnostic(
                            "cruxible.capture_contract.revival_refused",
                            "A retired CaptureContract cannot be revived; propose a new "
                            "contract identity instead.",
                            path=path,
                        ),
                    ),
                )
            broken = capture_contract_successor_break(predecessor.contract, contract)
            if broken is not None:
                return CaptureContractLawResult(
                    verdict="refused",
                    diagnostics=(
                        _diagnostic(
                            "cruxible.capture_contract.incompatible_successor",
                            f"A CaptureContract successor changes {broken!r} incompatibly. "
                            "Evidence captured under every version of a contract is admitted "
                            "alike, so a breaking change is a new contract identity: propose it "
                            "under a new name and update the evidence rules that should admit it.",
                            path=path,
                        ),
                    ),
                )
    if (
        isinstance(contract, CaptureContract | NamedCaptureContract)
        and contract.shape != "coordinator_self_source"
    ):
        expected = capture_shape_components(contract.shape, contract.replay)
        if capture_retention_policy(contract).erasure == "authorized_by_rule":
            expected = expected.model_copy(
                update={"pins": tuple(sorted((*expected.pins, _ERASURE_RULE_PIN), key=_pin_key))}
            )
        # Built-in foreign contracts retain their original identical decomposition.
        if capture_components(contract) != expected:
            return CaptureContractLawResult(
                verdict="refused",
                diagnostics=(
                    _diagnostic(
                        "cruxible.capture_contract.shape_components_mismatch",
                        "Components must be the engine expansion of shape and replay; "
                        "author the input record.",
                        path=path,
                    ),
                ),
            )
    registry_roles = {
        (pin.role, pin.artifact_digest)
        for pin in (
            *capture_components(contract).coordinate_schema_pins,
            *capture_components(contract).selector_schema_pins,
            capture_components(contract).commitment_canonicalizer,
            *capture_components(contract).pins,
        )
    }
    component_pins = (
        *capture_components(contract).coordinate_schema_pins,
        *capture_components(contract).selector_schema_pins,
        capture_components(contract).commitment_canonicalizer,
        *(pin for pin in capture_components(contract).pins if pin.target.kind == "Contract"),
    )
    unresolved_components = tuple(
        pin for pin in component_pins if not CAPTURE_COMPONENTS.resolves(pin)
    )
    if unresolved_components and contract != DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT:
        return CaptureContractLawResult(
            verdict="refused",
            diagnostics=(
                _diagnostic(
                    "cruxible.capture_contract.component_registry_unresolved",
                    "A Capture component identifier/digest is absent from the compiler registry.",
                    path=path,
                ),
            ),
        )
    required_registry_entries = {
        ("replay-policy", capture_components(contract).replay_policy_digest),
        ("provenance-rule", capture_components(contract).provenance_rule_digest),
        ("source-subject-mapping", capture_components(contract).source_subject_mapping_digest),
    }
    if not required_registry_entries.issubset(registry_roles) and (
        contract != DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT
    ):
        return CaptureContractLawResult(
            verdict="refused",
            diagnostics=(
                _diagnostic(
                    "cruxible.capture_contract.rule_registry_unresolved",
                    "Replay, provenance, and source-subject rules require exact governed pins.",
                    path=path,
                ),
            ),
        )
    if "external" in contract_source_kinds(contract) and (
        not capture_components(contract).coordinate_schema_pins
        or not capture_components(contract).selector_schema_pins
    ):
        return CaptureContractLawResult(
            verdict="refused",
            diagnostics=(
                _diagnostic(
                    "cruxible.capture_contract.external_schema_missing",
                    "External CaptureContracts require coordinate and selector schema pins.",
                    path=path,
                ),
            ),
        )
    erasure_digest = capture_retention_policy(contract).erasure_rule_digest
    if erasure_digest is not None and ("erasure-rule", erasure_digest) not in registry_roles:
        return CaptureContractLawResult(
            verdict="refused",
            diagnostics=(
                _diagnostic(
                    "cruxible.capture_contract.erasure_rule_unresolved",
                    "Authorized erasure requires the registered erasure rule's exact pin; "
                    "build the contract with authorize_erasure(contract) from "
                    "cruxible_client.contracts.captures, which sets the policy and the pin "
                    "together.",
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


class CaptureRunCoordinate(_StrictCaptureModel):
    """Capture-v2 run coordinate with the Provider lane's widened run-id grammar."""

    tag: Literal["playbill-capture-run-coordinate-v2"] = "playbill-capture-run-coordinate-v2"
    run_kind: Literal["procedure", "watcher", "provider"]
    run_id: str
    bound_generation: str
    executable_identity: ArtifactIdentity
    executable_digest: str

    @field_validator("run_id")
    @classmethod
    def _run_id(cls, value: str) -> str:
        if not _RUN_ID_V2_RE.fullmatch(value):
            raise ValueError("Capture v2 run_id is not canonical")
        return value

    @field_validator("bound_generation", "executable_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class SourceEffectiveTime(_StrictCaptureModel):
    tag: Literal["playbill-source-effective-time-v1"] = "playbill-source-effective-time-v1"
    effective_from: datetime
    effective_until: datetime | None = None

    @model_validator(mode="after")
    def _interval(self) -> "SourceEffectiveTime":
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
    source: SourceReference
    commitment: EvidenceCommitment
    run_coordinate: CaptureRunCoordinateV1
    run_receipt_digest: str
    producer: ArtifactIdentity
    producer_binding_digest: str
    observed_at: datetime
    source_effective_time: SourceEffectiveTime | None = None
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
        if isinstance(self.source, ExternalSourceReference):
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


class ProviderInvocationCaptureEvidence(_StrictCaptureModel):
    """Complete governed and measured evidence for one Source Provider call."""

    tag: Literal["playbill-capture-provider-invocation-evidence-v1"] = (
        "playbill-capture-provider-invocation-evidence-v1"
    )
    provider_artifact_digest: str
    external_occurrence_plan_digest: str
    interface_artifact_digest: str
    interface_id: str
    interface_digest: str
    implementation_digest: str
    materialization_digest: str
    # Historical, mirrored from the invocation receipt: present only for a call
    # admitted before core stopped classifying inputs.
    input_bucket: str | None = Field(default=None, exclude_if=lambda value: value is None)
    invocation_receipt_digest: str
    source_read_receipt_digest: str | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    secret_references: tuple[ProviderSecretReference, ...] = ()
    egress: ProviderEgressObservation

    @field_validator(
        "provider_artifact_digest",
        "external_occurrence_plan_digest",
        "interface_artifact_digest",
        "interface_digest",
        "implementation_digest",
        "materialization_digest",
        "invocation_receipt_digest",
        "source_read_receipt_digest",
    )
    @classmethod
    def _digests(cls, value: str | None) -> str | None:
        if value is None:
            return None
        Sha256Value.from_tagged(value)
        return value

    @field_validator("secret_references")
    @classmethod
    def _secret_references(
        cls,
        value: tuple[ProviderSecretReference, ...],
    ) -> tuple[ProviderSecretReference, ...]:
        def key(item: ProviderSecretReference) -> tuple[bytes, bytes, bytes, bytes]:
            return (
                item.realm.encode("utf-8"),
                item.name.encode("utf-8"),
                item.epoch.encode("utf-8"),
                item.purpose.encode("utf-8"),
            )

        if value != tuple(sorted(value, key=key)) or len(
            {(item.realm, item.name, item.epoch, item.purpose) for item in value}
        ) != len(value):
            raise ValueError("Capture secret references must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _workspace_receipt(self) -> "ProviderInvocationCaptureEvidence":
        if (self.interface_digest in WORKSPACE_FILE_INTERFACE_DIGESTS) != (
            self.source_read_receipt_digest is not None
        ):
            raise ValueError(
                "workspace.file evidence requires exactly one source-read receipt digest"
            )
        return self


class ProcedureEgressCaptureEvidence(_StrictCaptureModel):
    """Procedure producer-receipt evidence with no Provider-only fields."""

    tag: Literal["playbill-capture-procedure-egress-evidence-v1"] = (
        "playbill-capture-procedure-egress-evidence-v1"
    )
    procedure_producer_receipt_digest: str

    @field_validator("procedure_producer_receipt_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


@runtime_checkable
class ProcedureProducerReceiptProtocol(Protocol):
    """Client-side structural view of one daemon-owned Procedure producer receipt."""

    @property
    def tag(self) -> str: ...

    @property
    def admission_binding_digest(self) -> str: ...

    @property
    def run_id(self) -> str: ...

    @property
    def accepted_coordinate(self) -> AcceptedCoordinate: ...

    @property
    def procedure_identity(self) -> ArtifactIdentity: ...

    @property
    def procedure_artifact_digest(self) -> str: ...

    @property
    def terminal_node_id(self) -> str: ...

    @property
    def item_manifest_digests(self) -> tuple[str, ...]: ...

    @property
    def capture_contract_digest(self) -> str: ...


@dataclass(frozen=True)
class CaptureProductionRecord:
    """A daemon journal record naming one Capture a run produced, exactly.

    A Source acquisition the run selected records ``produced_capture`` with the
    Capture digest, its contract digest and the invocation receipt; a delivered
    ``emit_capture`` terminal names each child Capture with the terminal's
    producer receipt. ``run_id`` only locates the record; the proof is the
    exact digests it names.
    """

    capture_digest: str
    capture_contract_digest: str
    run_id: str
    #: The producer receipt the record binds the Capture to, when it names one.
    receipt_digest: str | None


@dataclass(frozen=True)
class VerifiedCaptureMemo:
    """One Capture verification a request context already did, and what it relied on.

    Everything it records is bound to the Capture digest -- the envelope, its
    material and its journal records are immutable -- except what the caller
    supplies: the contract version and producer artifact it was verified
    against, and whether it was held to production proof. A later verification
    reuses it only when those agree, and a lenient verification never answers a
    strict one.
    """

    capture_digest: str
    envelope: CaptureEnvelopeV1 | CaptureEnvelope
    contract_digest: str
    #: The accepted producer artifact digest consulted; None when the
    #: executable is the contract itself.
    executable_digest: str | None
    production_proven: bool
    #: Facts derived from the Capture's bytes (the cited source window), each
    #: kept with the body availability it was derived under and reused only
    #: while that availability still holds.
    derived: dict[str, object] = field(default_factory=dict, compare=False, repr=False)


class ProducerReceiptResolverProtocol(Protocol):
    """Resolve only receipts and production records the daemon journal holds.

    One resolver is one request's verification context: it also keeps each
    Capture verification it finished, so N citations of one Capture verify it
    once.
    """

    def producer_receipt(
        self,
        digest: str,
        *,
        run_id: str,
    ) -> ProviderProducerReceiptResolution | ProcedureProducerReceiptProtocol | None:
        """The receipt with this digest; ``run_id`` locates the run that recorded it."""
        ...

    def produced_capture(
        self,
        capture_digest: str,
        *,
        run_id: str,
    ) -> tuple[CaptureProductionRecord, ...]:
        """Every daemon record naming this exact Capture digest as produced."""
        ...

    def recall_verified(self, capture_digest: str) -> VerifiedCaptureMemo | None:
        """The verification this context already finished for the Capture, if any."""
        ...

    def remember_verified(self, memo: VerifiedCaptureMemo) -> None:
        """Keep one finished verification; a strict one is never replaced by a lenient one."""
        ...


@runtime_checkable
class _RequestCaptureProofEpoch(Protocol):
    """An optional request owner binds journal proof to recorded body changes."""

    def _capture_proof_epoch(self) -> object | None: ...


def _request_journal_proof_still_answers(
    memo: VerifiedCaptureMemo,
    resolver: ProducerReceiptResolverProtocol,
    *,
    require_production_proof: bool,
) -> bool:
    epoch = (
        resolver._capture_proof_epoch() if isinstance(resolver, _RequestCaptureProofEpoch) else None
    )
    if epoch is not None and memo.derived.get("journal_proof_epoch") == epoch:
        return not require_production_proof or memo.production_proven
    proven = _journal_proof_still_answers(
        memo, resolver, require_production_proof=require_production_proof
    )
    if proven and epoch is not None:
        memo.derived["journal_proof_epoch"] = epoch
    return proven


CaptureProductionGap = Literal["no_production_record", "v1_envelope"]


class CaptureProductionUnproven(CaptureFormatError):
    """A Capture cited as new evidence has no daemon proof that a run produced it.

    ``no_production_record``: no journal record names the exact Capture digest
    (bytes stored outside a run, or a Source read the acquisition rule did not
    select). ``v1_envelope``: a version-1 envelope under a contract that is not
    self-asserted carries no producer receipt, so nothing can prove it.
    """

    def __init__(self, reason: CaptureProductionGap, message: str) -> None:
        super().__init__(message)
        self.reason: CaptureProductionGap = reason


@dataclass(frozen=True)
class ProviderProducerReceiptResolution:
    """Daemon-resolved Provider receipt and its exact admitted occurrence."""

    receipt: ProviderInvocationReceipt
    occurrence: ProviderExternalOccurrencePlan
    source_read_receipt: SourceReadReceipt | None = None


def _procedure_producer_receipt_digest(
    receipt: ProcedureProducerReceiptProtocol,
) -> str:
    return typed_digest(
        Sha256Value,
        "playbill-procedure-producer-receipt-v1",
        {
            "admission_binding_digest": receipt.admission_binding_digest,
            "run_id": receipt.run_id,
            "accepted_coordinate": receipt.accepted_coordinate.model_dump(mode="json"),
            "procedure_identity": receipt.procedure_identity.model_dump(mode="json"),
            "procedure_artifact_digest": receipt.procedure_artifact_digest,
            "terminal_node_id": receipt.terminal_node_id,
            "item_manifest_digests": list(receipt.item_manifest_digests),
            "capture_contract_digest": receipt.capture_contract_digest,
        },
    ).tagged


class ProviderResultToExternalCapture(_StrictCaptureModel):
    """Canonical Source Provider output from which core reconstructs a Capture."""

    tag: Literal["playbill-provider-result-to-external-capture-v1"] = (
        "playbill-provider-result-to-external-capture-v1"
    )
    source_identity: str
    coordinate_type: str
    coordinate: object
    selector_type: str
    selector: object
    replayability: Literal["exact", "attested_only"]
    content_encoding: Literal["base64"] = "base64"
    content_base64: str
    byte_length: int = Field(ge=0)
    bytes_digest: str
    observed_at: datetime = Field(description="Reads EVALUATION INSTANT.")
    source_effective_time: SourceEffectiveTime | None = Field(
        default=None,
        description="Nested half-open interval reads VALIDITY WINDOW.",
    )

    _canonical_values = field_validator("coordinate", "selector", mode="before")(
        normalize_canonical
    )

    @field_validator("bytes_digest")
    @classmethod
    def _bytes_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("observed_at")
    @classmethod
    def _observed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Provider Capture observed_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _content(self) -> "ProviderResultToExternalCapture":
        try:
            content = base64.b64decode(self.content_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Provider Capture content is not canonical base64") from exc
        if base64.b64encode(content).decode("ascii") != self.content_base64:
            raise ValueError("Provider Capture content is not canonical base64")
        if len(content) != self.byte_length:
            raise ValueError("Provider Capture byte length does not reproduce")
        if CasDigest(hashlib.sha256(content).hexdigest()).tagged != self.bytes_digest:
            raise ValueError("Provider Capture bytes digest does not reproduce")
        ExternalSourceReference(
            source_identity=self.source_identity,
            producer_binding_digest="sha256:" + "0" * 64,
            coordinate_type=self.coordinate_type,
            coordinate=self.coordinate,
            selector_type=self.selector_type,
            selector=self.selector,
            replayability=self.replayability,
        )
        return self


CaptureProductionEvidence = Annotated[
    ProviderInvocationCaptureEvidence | ProcedureEgressCaptureEvidence,
    Field(discriminator="tag"),
]


class CaptureEnvelope(_StrictCaptureModel):
    """Capture successor binding one exact topological producer receipt."""

    tag: Literal["playbill-capture-envelope-v2"] = "playbill-capture-envelope-v2"
    capture_contract_digest: str
    source: SourceReference
    commitment: EvidenceCommitment
    run_coordinate: CaptureRunCoordinate
    producer_receipt_digest: str
    producer: ArtifactIdentity
    producer_binding_digest: str
    observed_at: datetime = Field(description="Reads EVALUATION INSTANT.")
    source_effective_time: SourceEffectiveTime | None = Field(
        default=None,
        description="Nested half-open interval reads VALIDITY WINDOW.",
    )
    reducer_digest: str | None = None
    input_receipt_set_manifest_digest: str | None = None
    production_evidence: CaptureProductionEvidence
    #: What was observed, by content: the bytes a ``workspace.file`` read (its
    #: source-read receipt's ``bytes_digest``) or the body a ``web.fetch`` v3
    #: origin sent (``retrieved.body_sha256``, recomputed by core). Two Captures
    #: of the same content share it however their envelopes differ. Absent
    #: otherwise (and on every envelope written before it), so existing bytes
    #: are unchanged; ``observed_content`` then reads the commitment digest.
    observed_content_digest: str | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @field_validator(
        "capture_contract_digest",
        "producer_receipt_digest",
        "producer_binding_digest",
        "reducer_digest",
        "input_receipt_set_manifest_digest",
        "observed_content_digest",
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
    def _envelope_shape(self) -> "CaptureEnvelope":
        validate_source_commitment(self.source, self.commitment)
        if isinstance(self.source, ExternalSourceReference) and (
            self.source.producer_binding_digest != self.producer_binding_digest
        ):
            raise ValueError("external source and envelope producer bindings differ")
        derived = (
            self.reducer_digest is not None or self.input_receipt_set_manifest_digest is not None
        )
        if derived != (
            self.reducer_digest is not None and self.input_receipt_set_manifest_digest is not None
        ):
            raise ValueError("derived Captures require reducer and input receipt-set together")
        evidence = self.production_evidence
        if isinstance(evidence, ProviderInvocationCaptureEvidence):
            if self.producer.kind != "Provider":
                raise ValueError("provider Capture production evidence does not correspond")
            if self.run_coordinate.run_kind != "provider":
                raise ValueError("provider Capture run_kind does not correspond")
            if self.run_coordinate.executable_identity != self.producer:
                raise ValueError("provider Capture executable_identity does not correspond")
            if self.run_coordinate.executable_digest != evidence.provider_artifact_digest:
                raise ValueError("provider Capture provider_artifact_digest does not correspond")
            if self.producer_binding_digest != evidence.external_occurrence_plan_digest:
                raise ValueError(
                    "provider Capture external_occurrence_plan_digest does not correspond"
                )
            if self.producer_receipt_digest != evidence.invocation_receipt_digest:
                raise ValueError("provider Capture invocation_receipt_digest does not correspond")
        elif (
            self.producer.kind != "Procedure"
            or self.run_coordinate.run_kind != "procedure"
            or self.run_coordinate.executable_identity != self.producer
            or self.run_coordinate.executable_digest != self.producer_binding_digest
            or self.producer_receipt_digest != evidence.procedure_producer_receipt_digest
        ):
            raise ValueError("procedure Capture production evidence does not correspond")
        return self


CaptureEnvelopeAny = Annotated[
    CaptureEnvelopeV1 | CaptureEnvelope,
    Field(discriminator="tag"),
]
_CAPTURE_ENVELOPE_ADAPTER: TypeAdapter[CaptureEnvelopeV1 | CaptureEnvelope] = TypeAdapter(
    CaptureEnvelopeAny
)


def render_capture_envelope(envelope: CaptureEnvelopeV1 | CaptureEnvelope) -> bytes:
    """Return the exact CAS object bytes; Capture envelopes carry no newline."""

    return model_canonical_bytes(envelope)


#: Envelopes parsed once per exact bytes and shared sealed: one Capture cited by a
#: whole fan-out of Claims is read back once per citing Claim.
_CAPTURE_ENVELOPE_PARSES: ParseMemo[CaptureEnvelopeV1 | CaptureEnvelope] = ParseMemo(
    capacity=4096, max_bytes=32 * 1024 * 1024
)


def parse_capture_envelope(content: bytes) -> CaptureEnvelopeV1 | CaptureEnvelope:
    """Parse one Capture envelope once per exact bytes; see ``content_memo``."""

    return _CAPTURE_ENVELOPE_PARSES.parse(
        content, "capture-envelope", None, lambda: _parse_capture_envelope(content)
    )


def _parse_capture_envelope(content: bytes) -> CaptureEnvelopeV1 | CaptureEnvelope:
    try:
        envelope = _CAPTURE_ENVELOPE_ADAPTER.validate_json(content)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_input=False)
        context = errors[0].get("ctx") if len(errors) == 1 else None
        clause = context.get("error") if isinstance(context, dict) else None
        detail = (
            str(clause)
            if isinstance(clause, ValueError)
            else "Capture envelope failed strict versioned validation"
        )
        raise CaptureFormatError(detail) from exc
    except ValueError as exc:
        raise CaptureFormatError("Capture envelope failed strict versioned validation") from exc
    if render_capture_envelope(envelope) != content:
        raise CaptureFormatError("Capture envelope is not in canonical wire form")
    return envelope


@sealed_memo
def capture_digest(envelope: CaptureEnvelopeV1 | CaptureEnvelope) -> CasDigest:
    return CasDigest(hashlib.sha256(render_capture_envelope(envelope)).hexdigest())


class InputReceiptSetManifest(_StrictCaptureModel):
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
    def _nonempty(self) -> "InputReceiptSetManifest":
        if not any(
            (
                self.input_receipt_digests,
                self.input_capture_digests,
                self.input_claim_artifact_digests,
            )
        ):
            raise ValueError("input receipt-set manifest must name at least one exact input")
        return self


def render_input_receipt_set_manifest(manifest: InputReceiptSetManifest) -> bytes:
    return canonical_bytes(manifest.model_dump(mode="json"))


def input_receipt_set_manifest_digest(manifest: InputReceiptSetManifest) -> CasDigest:
    return CasDigest(hashlib.sha256(render_input_receipt_set_manifest(manifest)).hexdigest())


class DirectClaimSource(_StrictCaptureModel):
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


class DirectByteSpanSelection(_StrictCaptureModel):
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


class DirectExternalSelection(_StrictCaptureModel):
    """Typed author-asserted external selection; PC-C adds adapter verification."""

    tag: Literal["playbill-direct-external-selection-v1"] = "playbill-direct-external-selection-v1"
    logical_source_identity: str
    coordinate_type: str
    coordinate: object
    selector_type: str
    selector: object
    commitment: EvidenceCommitment

    @model_validator(mode="after")
    def _registered_shape(self) -> "DirectExternalSelection":
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
        ExternalSourceReference(
            source_identity=self.logical_source_identity,
            producer_binding_digest="sha256:" + "00" * 32,
            coordinate_type=self.coordinate_type,
            coordinate=self.coordinate,
            selector_type=self.selector_type,
            selector=self.selector,
            replayability="attested_only",
        )
        return self


class DirectForeignSourceSelection(_StrictCaptureModel):
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
    def _selection(self) -> "DirectForeignSourceSelection":
        if self.span.end_byte <= self.span.start_byte:
            raise ValueError("a foreign source selection must cover at least one byte")
        return self


DirectClaimSelection = Annotated[
    DirectByteSpanSelection | DirectExternalSelection | DirectForeignSourceSelection,
    Field(discriminator="tag"),
]


@runtime_checkable
class CaptureObjectStoreProtocol(Protocol):
    def store(self, content: bytes) -> CasObjectMetadata: ...

    def verify(self, digest: str) -> bool: ...

    def read(self, digest: str, *, access: BodyAccessContext) -> bytes: ...


@runtime_checkable
class LedgerMaterialResolverProtocol(Protocol):
    def read_ledger_source(self, source: LedgerSourceReference) -> bytes: ...


class DirectCaptureBuildResult(_StrictCaptureModel):
    contract: CaptureContractAny
    contract_digest: str
    envelope: CaptureEnvelopeV1
    capture_digest: str
    source_body_digest: str
    source_body_materialized: bool


def capture_is_direct_self_source(
    envelope: CaptureEnvelopeAny,
    *,
    contract: CaptureContractAny,
    store: CaptureObjectStoreProtocol,
    claim_id: str,
) -> bool:
    """Recognize only the direct body builder's Claim-bound source shape."""

    if (
        contract.identity.name != DIRECT_SELF_ASSERTED_CONTRACT_ID
        or envelope.run_coordinate.run_id != f"direct:{claim_id.casefold()}"
    ):
        return False
    if isinstance(envelope.source, ExternalSourceReference):
        return (
            envelope.source.coordinate_type == "authenticated-request-v1"
            and envelope.source.selector_type == "direct-claim-source-v1"
            and isinstance(envelope.source.selector, dict)
            and envelope.source.selector.get("claim_id") == claim_id
        )
    if not isinstance(envelope.source, CasSourceReference):
        return False
    try:
        content = store.read(
            envelope.source.content_digest,
            access=BodyAccessContext(principal_id="playbill-compiler", can_read_body=True),
        )
        source = DirectClaimSource.model_validate_json(content)
    except (CasError, ValidationError):
        return False
    return (
        source.claim_id == claim_id and canonical_bytes(source.model_dump(mode="json")) == content
    )


def capture_is_coordinator_self_source(
    envelope: CaptureEnvelopeAny,
    *,
    contract: CaptureContractAny,
    claim_id: str,
) -> bool:
    """Recognize the mandatory-retained coordinator profile at its Claim binding."""

    return (
        is_coordinator_self_source_contract(contract)
        and isinstance(envelope.source, CasSourceReference)
        and envelope.commitment.materialization == "cas"
        and envelope.run_coordinate.run_id == f"coordinator-self-source:{claim_id.casefold()}"
        and envelope.run_coordinate.executable_identity == contract.identity
        and envelope.run_coordinate.executable_digest == capture_contract_digest(contract).tagged
    )


def capture_is_direct_selection_bound(
    envelope: CaptureEnvelopeAny,
    *,
    contract: CaptureContractAny,
    claim_id: str,
) -> bool:
    """Recognize the direct-selection builder's Claim binding.

    A direct-selection Capture points at the selected bytes rather than a
    serialized DirectClaimSource, so its binding lives in the frozen run
    coordinate (and, for unmaterialized external selections, the selector).
    """

    if (
        contract != DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT
        or envelope.run_coordinate.run_kind != "provider"
        or envelope.run_coordinate.run_id != f"direct-selection:{claim_id.casefold()}"
        or envelope.run_coordinate.executable_identity != contract.identity
        or envelope.run_coordinate.executable_digest != capture_contract_digest(contract).tagged
    ):
        return False
    if isinstance(envelope.source, ExternalSourceReference):
        selector = envelope.source.selector
        return isinstance(selector, dict) and selector.get("claim_id") == claim_id
    return isinstance(envelope.source, CasSourceReference)


CaptureReuseClassification = Literal[
    "claim_bound",
    "claim_bound_mismatch",
    "shareable",
    "not_shareable",
]


def classify_capture_reuse(
    envelope: CaptureEnvelopeAny,
    *,
    contract: CaptureContractAny,
    store: CaptureObjectStoreProtocol,
    claim_id: str,
) -> CaptureReuseClassification:
    """Classify reuse with the same fail-closed Claim-family law at every boundary.

    Contract identity and source-subject mapping are independent scope signals.
    Either Claim-self signal makes the Capture Claim-bound; conflicting or forged
    family combinations therefore fail closed instead of falling through to the
    otherwise-observed shareable class.
    """

    identity_family = (
        "direct"
        if contract.identity.name == DIRECT_SELF_ASSERTED_CONTRACT_ID
        else (
            "coordinator" if contract.identity.name == COORDINATOR_SELF_SOURCE_CONTRACT_ID else None
        )
    )
    mapping_family = (
        "direct"
        if capture_components(contract).source_subject_mapping_digest
        == capture_components(DIRECT_SELF_ASSERTED_CAPTURE_CONTRACT).source_subject_mapping_digest
        else (
            "coordinator"
            if capture_components(contract).source_subject_mapping_digest
            == capture_components(
                COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT
            ).source_subject_mapping_digest
            else None
        )
    )
    family = identity_family or mapping_family
    if family is not None:
        if identity_family != family or mapping_family != family:
            return "claim_bound_mismatch"
        matches = (
            family == "direct"
            and (
                capture_is_direct_self_source(
                    envelope,
                    contract=contract,
                    store=store,
                    claim_id=claim_id,
                )
                or capture_is_direct_selection_bound(
                    envelope,
                    contract=contract,
                    claim_id=claim_id,
                )
            )
        ) or (
            family == "coordinator"
            and capture_is_coordinator_self_source(
                envelope,
                contract=contract,
                claim_id=claim_id,
            )
        )
        return "claim_bound" if matches else "claim_bound_mismatch"
    return "shareable" if contract.epistemic_grade == "observed" else "not_shareable"


class CaptureBuildResult(_StrictCaptureModel):
    contract_digest: str
    envelope: CaptureEnvelopeV1 | CaptureEnvelope
    capture_digest: str
    commitment_digest: str
    source_body_materialized: bool


def _direct_source_bytes(source: DirectClaimSource) -> bytes:
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
    contract: CaptureContractAny,
    envelope: CaptureEnvelopeV1,
    source_body_digest: str,
    source_body_materialized: bool,
) -> DirectCaptureBuildResult:
    envelope_bytes = render_capture_envelope(envelope)
    stored_envelope = store.store(envelope_bytes)
    expected_capture_digest = capture_digest(envelope).tagged
    if stored_envelope.digest != expected_capture_digest:
        raise CasError("Capture envelope CAS digest did not reproduce")
    return DirectCaptureBuildResult(
        contract=contract,
        contract_digest=envelope.capture_contract_digest,
        envelope=envelope,
        capture_digest=expected_capture_digest,
        source_body_digest=source_body_digest,
        source_body_materialized=source_body_materialized,
    )


def build_foreign_source_capture(
    *,
    store: CaptureObjectStoreProtocol,
    actor_id: str,
    claim_id: str,
    rationale: str,
    observed_at: datetime,
    accepted_coordinate: AcceptedCoordinate,
    selection: DirectForeignSourceSelection,
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
        raise CasError("foreign source selection CAS digest did not reproduce")
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
        source=ExternalSourceReference(
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
        commitment=EvidenceCommitment(
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
    accepted_contract: CaptureContractAny | None = None,
) -> DirectCaptureBuildResult:
    """Commit the bounded bytes from one typed proposer-observed working selection.

    The whole source is deliberately absent. The coordinate and selector record
    exactly what the client observed, while the retained commitment is only over
    the selected bytes that the daemon can reproduce.

    `accepted_contract` is the source's contract as accepted now, when it has
    been improved through a successor; the Capture then records that version.
    Without it the Capture uses the source's deterministic first version.
    """

    contract: CaptureContractAny = foreign_source_capture_contract(source_id)
    if accepted_contract is not None:
        if accepted_contract.identity != contract.identity:
            raise CaptureFormatError("accepted contract is not this source's contract")
        contract = accepted_contract
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
        source=ExternalSourceReference(
            source_identity=source_id,
            producer_binding_digest=binding_digest,
            coordinate_type=FOREIGN_SOURCE_COORDINATE_TYPE,
            coordinate=coordinate,
            selector_type=FOREIGN_SOURCE_SELECTOR_TYPE,
            selector={"claim_id": claim_id, "working_selection": selector},
            replayability="attested_only",
        ),
        commitment=EvidenceCommitment(
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
    contract: CaptureContractAny,
    envelope: CaptureEnvelopeV1 | CaptureEnvelope,
    source_body_materialized: bool,
) -> CaptureBuildResult:
    metadata = store.store(render_capture_envelope(envelope))
    expected = capture_digest(envelope).tagged
    if metadata.digest != expected:
        raise CasError("Capture envelope CAS digest did not reproduce")
    return CaptureBuildResult(
        contract_digest=capture_contract_digest(contract).tagged,
        envelope=envelope,
        capture_digest=expected,
        commitment_digest=envelope.commitment.digest,
        source_body_materialized=source_body_materialized,
    )


def provider_capture_receipt_matches_occurrence(
    receipt: ProviderInvocationReceipt,
    occurrence: ProviderExternalOccurrencePlan,
) -> bool:
    expected_secret_receipts = tuple(
        sorted(
            (
                ProviderSecretReceiptReference(
                    binding_identity_digest=provider_secret_binding_identity_digest(
                        ProviderSecretBindingIdentity(
                            realm=reference.realm,
                            name=reference.name,
                        )
                    ),
                    purpose=reference.purpose,
                )
                for reference in occurrence.secret_plan.references
            ),
            key=lambda item: item.binding_identity_digest.encode("ascii"),
        )
    )
    local = occurrence.local_execution
    return (
        receipt.occurrence_path == occurrence.occurrence_path
        and receipt.provider_artifact_digest == occurrence.provider_artifact_digest
        and receipt.interface_id == occurrence.interface_id
        and receipt.interface_digest == occurrence.interface_digest
        and receipt.implementation_digest == occurrence.implementation_digest
        and receipt.materialization_digest == local.materialization_digest
        and receipt.deployment_digest == local.deployment_digest
        and receipt.capture_contract_digest == occurrence.capture_contract_digest
        and receipt.budget_translation == occurrence.budget_translation
        and receipt.secret_references == expected_secret_receipts
    )


ObservedContentBasis = Literal["workspace_file_bytes", "web_fetch_body", "commitment"]


@dataclass(frozen=True)
class ObservedContent:
    """What one Capture observed, by content, and what that identity rests on.

    ``workspace_file_bytes``: the bytes a ``workspace.file`` read, as its
    source-read receipt digests them. ``web_fetch_body``: the body a ``web.fetch``
    v3 origin sent, verified by core. ``commitment``: no observed-content
    identity is recorded, so the Capture's own commitment digest stands in.
    """

    digest: str
    basis: ObservedContentBasis


def observed_content(envelope: CaptureEnvelopeV1 | CaptureEnvelope) -> ObservedContent:
    """The content identity of what a Capture observed, with its basis."""

    if not isinstance(envelope, CaptureEnvelope) or envelope.observed_content_digest is None:
        return ObservedContent(digest=envelope.commitment.digest, basis="commitment")
    recorded = envelope.observed_content_digest
    evidence = envelope.production_evidence
    workspace = isinstance(evidence, ProviderInvocationCaptureEvidence) and (
        evidence.interface_digest in WORKSPACE_FILE_INTERFACE_DIGESTS
    )
    return ObservedContent(
        digest=recorded, basis="workspace_file_bytes" if workspace else "web_fetch_body"
    )


def _web_fetch_body_digest(content: bytes) -> str:
    """The verified ``retrieved.body_sha256`` of one ``web.fetch`` v3 material.

    Core recomputes ``sha256(b64decode(retrieved.body_base64))`` and refuses a
    material whose stated digest differs, or that carries no body to digest.
    """

    try:
        material = json.loads(content)
        retrieved = material["retrieved"]
        stated = retrieved["body_sha256"]
        body = base64.b64decode(retrieved["body_base64"], validate=True)
    except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
        raise CaptureObservedContentMismatch(
            f"{CaptureObservedContentMismatch.error_code}: the web.fetch result carries no "
            "retrieved body and body_sha256 to verify"
        ) from exc
    actual = "sha256:" + hashlib.sha256(body).hexdigest()
    if stated != actual:
        raise CaptureObservedContentMismatch(
            f"{CaptureObservedContentMismatch.error_code}: the web.fetch result states "
            f"body_sha256 {stated!r}, but its retrieved body digests to {actual}"
        )
    return actual


def build_provider_external_capture_v2(
    *,
    store: CaptureObjectStoreProtocol,
    contract: CaptureContractAny,
    result: ProviderResultToExternalCapture,
    receipt: ProviderInvocationReceipt,
    occurrence: ProviderExternalOccurrencePlan,
    producer: ArtifactIdentity,
    bound_generation: str,
    source_read_receipt: SourceReadReceipt | None = None,
) -> CaptureBuildResult:
    """Validate one common-driver Source result and commit its v2 Capture."""

    if occurrence.occurrence_kind != "source":
        raise CaptureFormatError("Provider Capture conversion requires a Source occurrence")
    if "external" not in contract_source_kinds(contract):
        raise CaptureFormatError("CaptureContract does not permit external observations")
    if "cas" not in contract_materialization_modes(contract):
        raise CaptureFormatError("CaptureContract does not permit CAS materialization")
    contract_digest_value = capture_contract_digest(contract).tagged
    if (
        occurrence.capture_contract_digest != contract_digest_value
        or receipt.capture_contract_digest != contract_digest_value
        or receipt.outcome.status != "ok"
        or receipt.output is None
        or producer.kind != "Provider"
        or not provider_capture_receipt_matches_occurrence(receipt, occurrence)
    ):
        raise CaptureFormatError("Provider Capture conversion differs from its admitted contract")
    try:
        output_commitment = ProviderInvocationOutputDigest.model_validate(receipt.output)
    except ValidationError as exc:
        raise CaptureFormatError("Provider Source receipt lacks its output digest") from exc
    if (
        isinstance(contract, CaptureContractV1)
        and result.source_identity not in contract.logical_source_identities
    ):
        raise CaptureFormatError("Provider Capture logical source is not admitted by its contract")
    if result.coordinate_type not in {
        pin.target.name for pin in capture_components(contract).coordinate_schema_pins
    } or result.selector_type not in {
        pin.target.name for pin in capture_components(contract).selector_schema_pins
    }:
        raise CaptureFormatError("Provider Capture source schemas are not admitted")
    if result.byte_length > contract.selection_budget.max_bytes:
        raise CaptureFormatError("Provider Capture result exceeds its contract byte budget")
    workspace_source = occurrence.interface_digest in WORKSPACE_FILE_INTERFACE_DIGESTS
    if workspace_source != (source_read_receipt is not None):
        raise CaptureFormatError("workspace.file Capture requires exactly one source-read receipt")
    if source_read_receipt is not None and (
        source_read_receipt.run_id != receipt.run_id
        or source_read_receipt.admission_binding_digest != receipt.admission_binding_digest
        or source_read_receipt.occurrence_path != receipt.occurrence_path
        or source_read_receipt.logical_source != result.source_identity
        or source_read_receipt.provider_input_digest != receipt.input_digest
        or source_read_receipt.policy_coordinate.generation_root != bound_generation
        or source_read_receipt.resolved_max_bytes != contract.selection_budget.max_bytes
    ):
        raise CaptureFormatError("workspace.file source-read receipt does not correspond")
    if isinstance(contract, CaptureContract | NamedCaptureContract) and result.replayability != (
        "exact" if contract.replay == "exact" else "attested_only"
    ):
        raise CaptureFormatError(
            "Provider replayability differs from the CaptureContract replay promise"
        )
    content = base64.b64decode(result.content_base64, validate=True)
    try:
        provider_output = (
            normalize_canonical(json.loads(content))
            if workspace_source
            else result.model_dump(mode="json")
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise CaptureFormatError("workspace.file result is not canonical JSON") from exc
    if workspace_source:
        # Verified canonical, the material's own bytes are the output's encoding.
        reproduces = canonical_bytes(
            provider_output
        ) == content and output_commitment.output_digest == (
            provider_invocation_output_digest_of_bytes(content)
        )
    else:
        reproduces = output_commitment.output_digest == provider_invocation_output_digest(
            provider_output
        )
    if not reproduces:
        raise CaptureFormatError("Provider Capture result differs from its invocation receipt")
    if workspace_source:
        assert source_read_receipt is not None
        source_echo = provider_output.get("source") if isinstance(provider_output, dict) else None
        if not isinstance(source_echo, dict) or source_echo != {
            "logical_source": source_read_receipt.logical_source,
            "commitment_digest": source_read_receipt.derived_request_digest,
            "bytes_digest": source_read_receipt.bytes_digest,
            "byte_length": source_read_receipt.byte_length,
        }:
            raise CaptureFormatError("workspace.file result mixes source attempts")
    observed_content_digest: str | None = None
    if source_read_receipt is not None:
        # Cross-checked above against this result's own source echo.
        observed_content_digest = source_read_receipt.bytes_digest
    elif occurrence.interface_digest == WEB_FETCH_V3_INTERFACE_DIGEST:
        observed_content_digest = _web_fetch_body_digest(content)
    stored = store.store(content)
    if stored.digest != result.bytes_digest:
        raise CasError("Provider Capture material CAS digest did not reproduce")
    occurrence_digest = provider_external_occurrence_plan_digest(occurrence)
    receipt_digest = provider_invocation_receipt_digest(receipt)
    evidence = ProviderInvocationCaptureEvidence(
        provider_artifact_digest=receipt.provider_artifact_digest,
        external_occurrence_plan_digest=occurrence_digest,
        interface_artifact_digest=occurrence.interface_artifact_digest,
        interface_id=receipt.interface_id,
        interface_digest=receipt.interface_digest,
        implementation_digest=receipt.implementation_digest,
        materialization_digest=receipt.materialization_digest,
        input_bucket=receipt.input_bucket,
        invocation_receipt_digest=receipt_digest,
        source_read_receipt_digest=(
            None if source_read_receipt is None else source_read_receipt_digest(source_read_receipt)
        ),
        secret_references=occurrence.secret_plan.references,
        egress=receipt.egress,
    )
    envelope = CaptureEnvelope(
        capture_contract_digest=contract_digest_value,
        source=ExternalSourceReference(
            source_identity=result.source_identity,
            producer_binding_digest=occurrence_digest,
            coordinate_type=result.coordinate_type,
            coordinate=result.coordinate,
            selector_type=result.selector_type,
            selector=result.selector,
            replayability=result.replayability,
        ),
        commitment=EvidenceCommitment(
            digest_kind="exact_bytes",
            digest=result.bytes_digest,
            byte_length=result.byte_length,
            materialization="cas",
        ),
        run_coordinate=CaptureRunCoordinate(
            run_kind="provider",
            run_id=receipt.run_id,
            bound_generation=bound_generation,
            executable_identity=producer,
            executable_digest=receipt.provider_artifact_digest,
        ),
        producer_receipt_digest=receipt_digest,
        producer=producer,
        producer_binding_digest=occurrence_digest,
        observed_at=result.observed_at,
        source_effective_time=result.source_effective_time,
        production_evidence=evidence,
        observed_content_digest=observed_content_digest,
    )
    return _store_general_capture(
        store=store,
        contract=contract,
        envelope=envelope,
        source_body_materialized=True,
    )


def build_procedure_capture_v2(
    *,
    store: CaptureObjectStoreProtocol,
    contract: CaptureContractAny,
    source_body: bytes,
    run_coordinate: CaptureRunCoordinate,
    producer_receipt_digest: str,
    producer: ArtifactIdentity,
    producer_binding_digest: str,
    observed_at: datetime,
) -> CaptureBuildResult:
    """Commit procedure egress below its already-authored producer receipt."""

    if producer.kind != "Procedure" or run_coordinate.executable_identity != producer:
        raise CaptureFormatError("procedure Capture requires its producing Procedure identity")
    if run_coordinate.executable_digest != producer_binding_digest:
        raise CaptureFormatError("procedure Capture binding differs from its executable digest")
    if "cas" not in contract_source_kinds(contract) or "cas" not in (
        contract_materialization_modes(contract)
    ):
        raise CaptureFormatError("CaptureContract does not permit CAS observations")
    if len(source_body) > contract.selection_budget.max_bytes:
        raise CaptureFormatError("procedure Capture exceeds its contract byte budget")
    body = store.store(source_body)
    envelope = CaptureEnvelope(
        capture_contract_digest=capture_contract_digest(contract).tagged,
        source=CasSourceReference(content_digest=body.digest),
        commitment=EvidenceCommitment(
            digest_kind="exact_bytes",
            digest=body.digest,
            byte_length=len(source_body),
            materialization="cas",
        ),
        run_coordinate=run_coordinate,
        producer_receipt_digest=producer_receipt_digest,
        producer=producer,
        producer_binding_digest=producer_binding_digest,
        observed_at=observed_at,
        production_evidence=ProcedureEgressCaptureEvidence(
            procedure_producer_receipt_digest=producer_receipt_digest,
        ),
    )
    return _store_general_capture(
        store=store,
        contract=contract,
        envelope=envelope,
        source_body_materialized=True,
    )


def build_cas_capture(
    *,
    store: CaptureObjectStoreProtocol,
    contract: CaptureContractAny,
    source_body: bytes,
    run_coordinate: CaptureRunCoordinateV1,
    run_receipt_digest: str,
    producer: ArtifactIdentity,
    producer_binding_digest: str,
    observed_at: datetime,
    source_effective_time: SourceEffectiveTime | None = None,
) -> CaptureBuildResult:
    """Create an exact bounded CAS observation under one accepted contract."""

    if "cas" not in contract_source_kinds(contract) or "cas" not in (
        contract_materialization_modes(contract)
    ):
        raise CaptureFormatError("CaptureContract does not permit CAS observations")
    if len(source_body) > contract.selection_budget.max_bytes:
        raise CaptureFormatError("CAS observation exceeds its CaptureContract byte budget")
    body = store.store(source_body)
    envelope = CaptureEnvelopeV1(
        capture_contract_digest=capture_contract_digest(contract).tagged,
        source=CasSourceReference(content_digest=body.digest),
        commitment=EvidenceCommitment(
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
    accepted_contract: CaptureContractAny | None = None,
) -> CaptureBuildResult:
    """Retain exact self-source bytes before minting their coordinator envelope."""

    governance_identifier(actor_id, label="coordinator self-source actor")
    if not re.fullmatch(r"CLM-[0-9a-f]{32}", claim_id):
        raise CaptureFormatError("coordinator self-source requires a Claim identity")
    contract = accepted_contract or COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT
    if not is_coordinator_self_source_contract(contract):
        raise CaptureFormatError("coordinator self-source requires its fixed contract profile")
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
    contract: CaptureContractAny,
    source: LedgerSourceReference,
    source_body: bytes,
    run_coordinate: CaptureRunCoordinateV1,
    run_receipt_digest: str,
    producer: ArtifactIdentity,
    producer_binding_digest: str,
    observed_at: datetime,
    source_effective_time: SourceEffectiveTime | None = None,
) -> CaptureBuildResult:
    """Bind exact accepted ledger bytes without copying them into the body CAS."""

    if "ledger" not in contract_source_kinds(contract) or "ledger" not in (
        contract_materialization_modes(contract)
    ):
        raise CaptureFormatError("CaptureContract does not permit ledger observations")
    if len(source_body) > contract.selection_budget.max_bytes:
        raise CaptureFormatError("ledger observation exceeds its CaptureContract byte budget")
    body_digest = CasDigest(hashlib.sha256(source_body).hexdigest()).tagged
    envelope = CaptureEnvelopeV1(
        capture_contract_digest=capture_contract_digest(contract).tagged,
        source=source,
        commitment=EvidenceCommitment(
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
    contract: CaptureContractAny,
    output_body: bytes,
    manifest: InputReceiptSetManifest,
    reducer_digest: str,
    run_coordinate: CaptureRunCoordinateV1,
    run_receipt_digest: str,
    producer: ArtifactIdentity,
    producer_binding_digest: str,
    observed_at: datetime,
    source_effective_time: SourceEffectiveTime | None = None,
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
        raise CasError("input receipt-set manifest CAS digest did not reproduce")
    output = store.store(output_body)
    envelope = CaptureEnvelopeV1(
        capture_contract_digest=capture_contract_digest(contract).tagged,
        source=CasSourceReference(content_digest=output.digest),
        commitment=EvidenceCommitment(
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


@runtime_checkable
class CaptureErasureLookupProtocol(Protocol):
    """A verification context that can also say which retained bodies were erased.

    ``body_erasures`` answers, for each given body digest an erasure record
    names, the latest such record: one keyed lookup per body. A body is
    content-addressed, so one Capture's erasure names the same bytes for every
    Capture that holds them.
    """

    def body_erasures(
        self, body_digests: tuple[str, ...]
    ) -> Mapping[str, RecordedCaptureErasure]: ...


def capture_retained_body_digests(envelope: CaptureEnvelopeAny) -> tuple[str, ...]:
    """The body objects a Capture keeps in the store: its CAS commitment and CAS source."""

    found: list[str] = []
    if envelope.commitment.materialization == "cas":
        found.append(envelope.commitment.digest)
    if isinstance(envelope.source, CasSourceReference):
        found.append(envelope.source.content_digest)
    return tuple(sorted(set(found)))


def capture_erasure_authorized(
    erasure: CaptureErasureRecord, contract: CaptureContractAny, *, contract_digest: str
) -> bool:
    """Whether ``erasure`` was made under ``contract``'s own erasure authority.

    ``contract`` is the exact version the record names (``contract_digest``):
    it authorizes erasure by rule, and the record names that rule.
    """

    policy = capture_retention_policy(contract)
    return (
        erasure.capture_contract_digest == contract_digest
        and policy.erasure == "authorized_by_rule"
        and policy.erasure_rule_digest == erasure.erasure_rule_digest
    )


def verify_capture(
    digest: str,
    *,
    store: CaptureObjectStoreProtocol,
    contract: CaptureContractAny,
    ledger_resolver: LedgerMaterialResolverProtocol | None = None,
    producer_artifact_digests: Mapping[str, str] | None = None,
    producer_receipt_resolver: ProducerReceiptResolverProtocol | None = None,
    require_production_proof: bool = True,
    erased_bodies: frozenset[str] = frozenset(),
) -> CaptureEnvelopeV1 | CaptureEnvelope:
    """Replay one envelope and every proof available for its source kind.

    ``require_production_proof`` is the check for a Capture cited as new
    evidence: a v2 envelope must be named, digest for digest, by a daemon
    journal record of the run that produced it, and a v1 envelope must be
    under a self-asserted contract, since it carries no producer receipt. Only
    a citation the accepted state already carries (an inherited citation, or a
    replay of an accepted acceptance) is verified without it.

    The resolver is the request's verification context: a verification it
    already finished for this digest answers again when the contract version,
    the producer artifact and the strictness agree. Ledger-sourced Captures are
    always verified afresh, against the caller's own ledger resolver.

    ``erased_bodies`` verifies a Capture whose retained bodies were destroyed by
    a governed erasure (the caller established it from the erasure records):
    everything is checked as usual except those bodies, which must be retained
    bodies of this Capture. Such a verification is never remembered.
    """

    CasDigest.from_tagged(digest)
    expected_contract = capture_contract_digest(contract).tagged
    if erased_bodies:
        return _verify_capture_once(
            digest,
            store=store,
            contract=contract,
            ledger_resolver=ledger_resolver,
            producer_artifact_digests=producer_artifact_digests,
            producer_receipt_resolver=producer_receipt_resolver,
            require_production_proof=require_production_proof,
            erased_bodies=erased_bodies,
        )
    if producer_receipt_resolver is not None:
        memo = producer_receipt_resolver.recall_verified(digest)
        if (
            memo is not None
            and _memo_answers(
                memo,
                store=store,
                contract=contract,
                contract_digest=expected_contract,
                producer_artifact_digests=producer_artifact_digests,
                require_production_proof=require_production_proof,
            )
            and _request_journal_proof_still_answers(
                memo,
                producer_receipt_resolver,
                require_production_proof=require_production_proof,
            )
        ):
            return memo.envelope
    envelope = _verify_capture_once(
        digest,
        store=store,
        contract=contract,
        ledger_resolver=ledger_resolver,
        producer_artifact_digests=producer_artifact_digests,
        producer_receipt_resolver=producer_receipt_resolver,
        require_production_proof=require_production_proof,
    )
    if producer_receipt_resolver is not None and not isinstance(
        envelope.source, LedgerSourceReference
    ):
        executable = envelope.run_coordinate.executable_identity
        verified = VerifiedCaptureMemo(
            capture_digest=digest,
            envelope=envelope,
            contract_digest=expected_contract,
            executable_digest=(
                None
                if executable == contract.identity
                else (producer_artifact_digests or {}).get(executable.qualified)
            ),
            production_proven=require_production_proof,
        )
        if isinstance(producer_receipt_resolver, _RequestCaptureProofEpoch):
            epoch = producer_receipt_resolver._capture_proof_epoch()
            if epoch is not None:
                # The cold verification above just checked this exact proof.
                verified.derived["journal_proof_epoch"] = epoch
        producer_receipt_resolver.remember_verified(verified)
    return envelope


def required_capture_bodies(
    envelope: CaptureEnvelopeV1 | CaptureEnvelope, capture_digest: str
) -> tuple[str, ...]:
    """Every retained Capture-side body verification requires, in one list.

    The envelope itself, its CAS-retained commitment and CAS source, and a
    derived Capture's input receipt-set manifest. A finished verification is
    reused only while the store still holds each of them, and, for a v2
    Capture, while the producer-journal proof payloads the resolver derived
    its receipt and production records from remain held and intact.
    """

    required = [capture_digest]
    if envelope.commitment.materialization == "cas":
        required.append(envelope.commitment.digest)
    if isinstance(envelope.source, CasSourceReference):
        required.append(envelope.source.content_digest)
    if envelope.input_receipt_set_manifest_digest is not None:
        required.append(envelope.input_receipt_set_manifest_digest)
    return tuple(dict.fromkeys(required))


def _journal_proof_still_answers(
    memo: VerifiedCaptureMemo,
    resolver: ProducerReceiptResolverProtocol,
    *,
    require_production_proof: bool,
) -> bool:
    """Whether the journal proof a v2 verification rested on still stands.

    The resolver answers its receipt and production lookups only while the
    journal payloads behind them are held and intact, so asking again is the
    same re-check a cold verification would make.
    """

    envelope = memo.envelope
    if not isinstance(envelope, CaptureEnvelope):
        return True
    run_id = envelope.run_coordinate.run_id
    try:
        if require_production_proof and not any(
            record.capture_digest == memo.capture_digest
            and record.capture_contract_digest == envelope.capture_contract_digest
            and record.receipt_digest in (None, envelope.producer_receipt_digest)
            for record in resolver.produced_capture(memo.capture_digest, run_id=run_id)
        ):
            return False
        return (
            resolver.producer_receipt(envelope.producer_receipt_digest, run_id=run_id) is not None
        )
    except (FormatError, ValueError):
        return False


def _memo_answers(
    memo: VerifiedCaptureMemo,
    *,
    store: CaptureObjectStoreProtocol,
    contract: CaptureContractAny,
    contract_digest: str,
    producer_artifact_digests: Mapping[str, str] | None,
    require_production_proof: bool,
) -> bool:
    """Whether a finished verification holds for this caller's contract, producers and store.

    The bytes behind a digest never change, but they can go missing: the
    caller's store must still hold the envelope and its retained material.
    """

    if memo.contract_digest != contract_digest:
        return False
    if not all(
        store.verify(item) for item in required_capture_bodies(memo.envelope, memo.capture_digest)
    ):
        return False
    if require_production_proof and not memo.production_proven:
        return False
    executable = memo.envelope.run_coordinate.executable_identity
    if memo.executable_digest is None:
        return executable == contract.identity
    return (
        producer_artifact_digests is not None
        and producer_artifact_digests.get(executable.qualified) == memo.executable_digest
    )


def _verify_observed_content(
    envelope: CaptureEnvelopeV1 | CaptureEnvelope,
    material: bytes,
    resolved_receipt: object,
) -> None:
    """An envelope's ``observed_content_digest`` is the one its producer evidence yields.

    Absent is always admissible (every envelope written before the field). A
    ``workspace.file`` Capture states its source-read receipt's ``bytes_digest``;
    a ``web.fetch`` v3 Capture states the body digest core recomputes from the
    retained material; no other Capture states one.
    """

    if not isinstance(envelope, CaptureEnvelope) or envelope.observed_content_digest is None:
        return
    evidence = envelope.production_evidence
    stated = envelope.observed_content_digest
    if isinstance(evidence, ProviderInvocationCaptureEvidence):
        if evidence.interface_digest in WORKSPACE_FILE_INTERFACE_DIGESTS:
            source_read = (
                resolved_receipt.source_read_receipt
                if isinstance(resolved_receipt, ProviderProducerReceiptResolution)
                else None
            )
            if source_read is not None and stated == source_read.bytes_digest:
                return
        elif evidence.interface_digest == WEB_FETCH_V3_INTERFACE_DIGEST:
            if stated == _web_fetch_body_digest(material):
                return
    raise CaptureFormatError("Capture observed_content_digest does not correspond")


def _verify_capture_once(
    digest: str,
    *,
    store: CaptureObjectStoreProtocol,
    contract: CaptureContractAny,
    ledger_resolver: LedgerMaterialResolverProtocol | None,
    producer_artifact_digests: Mapping[str, str] | None,
    producer_receipt_resolver: ProducerReceiptResolverProtocol | None,
    require_production_proof: bool,
    erased_bodies: frozenset[str] = frozenset(),
) -> CaptureEnvelopeV1 | CaptureEnvelope:
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
    erased = erased_bodies
    if not erased <= set(capture_retained_body_digests(envelope)):
        raise CaptureFormatError("erased bodies are not retained bodies of this Capture")
    executable = envelope.run_coordinate.executable_identity
    if isinstance(envelope, CaptureEnvelopeV1):
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
        if require_production_proof and not capture_contract_is_self_asserted(contract):
            raise CaptureProductionUnproven(
                "v1_envelope",
                "a version-1 Capture under a contract that is not self-asserted carries no "
                "producer receipt, so it cannot be cited as new evidence",
            )
    elif (
        executable != envelope.producer
        or producer_artifact_digests is None
        or producer_artifact_digests.get(executable.qualified)
        != envelope.run_coordinate.executable_digest
    ):
        raise CaptureFormatError("Capture v2 producer does not resolve at its exact digest")
    if isinstance(envelope, CaptureEnvelope):
        if producer_receipt_resolver is None:
            raise CaptureFormatError(
                "Capture producer receipt resolver is unavailable for "
                f"{envelope.producer_receipt_digest}"
            )
        run_id = envelope.run_coordinate.run_id
        if require_production_proof and not any(
            record.capture_digest == digest
            and record.capture_contract_digest == envelope.capture_contract_digest
            and record.receipt_digest in (None, envelope.producer_receipt_digest)
            for record in producer_receipt_resolver.produced_capture(digest, run_id=run_id)
        ):
            raise CaptureProductionUnproven(
                "no_production_record",
                f"no daemon run recorded producing Capture {digest}",
            )
        resolved_receipt = producer_receipt_resolver.producer_receipt(
            envelope.producer_receipt_digest, run_id=run_id
        )
        if resolved_receipt is None:
            raise CaptureFormatError(
                f"Capture producer receipt is unavailable: {envelope.producer_receipt_digest}"
            )
        evidence = envelope.production_evidence
        if isinstance(evidence, ProviderInvocationCaptureEvidence):
            if not isinstance(resolved_receipt, ProviderProducerReceiptResolution):
                raise CaptureFormatError("provider Capture resolved a non-Provider receipt")
            receipt = resolved_receipt.receipt
            occurrence = resolved_receipt.occurrence
            provider_evidence_bindings = (
                (
                    "external_occurrence_plan_digest",
                    evidence.external_occurrence_plan_digest,
                    provider_external_occurrence_plan_digest(occurrence),
                ),
                (
                    "interface_artifact_digest",
                    evidence.interface_artifact_digest,
                    occurrence.interface_artifact_digest,
                ),
                (
                    "secret_references",
                    evidence.secret_references,
                    occurrence.secret_plan.references,
                ),
                (
                    "invocation_receipt_digest",
                    evidence.invocation_receipt_digest,
                    provider_invocation_receipt_digest(receipt),
                ),
                (
                    "provider_artifact_digest",
                    evidence.provider_artifact_digest,
                    receipt.provider_artifact_digest,
                ),
                ("interface_id", evidence.interface_id, receipt.interface_id),
                ("interface_digest", evidence.interface_digest, receipt.interface_digest),
                (
                    "implementation_digest",
                    evidence.implementation_digest,
                    receipt.implementation_digest,
                ),
                (
                    "materialization_digest",
                    evidence.materialization_digest,
                    receipt.materialization_digest,
                ),
                ("input_bucket", evidence.input_bucket, receipt.input_bucket),
                ("egress", evidence.egress, receipt.egress),
            )
            for field_name, evidence_value, resolved_value in provider_evidence_bindings:
                if evidence_value != resolved_value:
                    raise CaptureFormatError(f"provider Capture {field_name} does not correspond")
            if receipt.outcome.status != "ok" or not provider_capture_receipt_matches_occurrence(
                receipt, occurrence
            ):
                raise CaptureFormatError("provider Capture resolved receipt is not admissible")
            source_read = resolved_receipt.source_read_receipt
            if (evidence.interface_digest in WORKSPACE_FILE_INTERFACE_DIGESTS) != (
                source_read is not None
            ):
                raise CaptureFormatError("provider Capture source-read receipt is unavailable")
            if source_read is not None and (
                not isinstance(envelope.source, ExternalSourceReference)
                or evidence.source_read_receipt_digest != source_read_receipt_digest(source_read)
                or source_read.run_id != receipt.run_id
                or source_read.admission_binding_digest != receipt.admission_binding_digest
                or source_read.occurrence_path != receipt.occurrence_path
                or source_read.logical_source != envelope.source.source_identity
                or source_read.provider_input_digest != receipt.input_digest
                or source_read.policy_coordinate.generation_root
                != envelope.run_coordinate.bound_generation
                or source_read.resolved_max_bytes != contract.selection_budget.max_bytes
            ):
                raise CaptureFormatError("provider Capture source-read receipt does not correspond")
        elif (
            not isinstance(resolved_receipt, ProcedureProducerReceiptProtocol)
            or resolved_receipt.tag != "playbill-procedure-producer-receipt-v1"
            or _procedure_producer_receipt_digest(resolved_receipt)
            != evidence.procedure_producer_receipt_digest
            or resolved_receipt.procedure_identity != envelope.producer
            or resolved_receipt.procedure_artifact_digest != envelope.producer_binding_digest
            or resolved_receipt.run_id != envelope.run_coordinate.run_id
            or resolved_receipt.accepted_coordinate.generation_root
            != envelope.run_coordinate.bound_generation
            or resolved_receipt.capture_contract_digest != envelope.capture_contract_digest
        ):
            raise CaptureFormatError("procedure Capture producer receipt does not correspond")
    if envelope.source.kind not in contract_source_kinds(contract):
        raise CaptureFormatError("Capture source kind is not permitted by its contract")
    if envelope.commitment.materialization not in contract_materialization_modes(contract):
        raise CaptureFormatError("Capture materialization is not permitted by its contract")
    if isinstance(envelope.source, ExternalSourceReference):
        if isinstance(
            contract, CaptureContract | NamedCaptureContract
        ) and envelope.source.replayability != (
            "exact" if contract.replay == "exact" else "attested_only"
        ):
            raise CaptureFormatError("Capture replayability differs from its CaptureContract")
        if (
            isinstance(contract, CaptureContractV1)
            and envelope.source.source_identity not in contract.logical_source_identities
        ):
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
            pin.target.name for pin in capture_components(contract).coordinate_schema_pins
        } or envelope.source.selector_type not in {
            pin.target.name for pin in capture_components(contract).selector_schema_pins
        }:
            raise CaptureFormatError("external Capture source schemas are not exactly pinned")
        if (
            envelope.commitment.materialization == "cas"
            and envelope.commitment.digest not in erased
        ):
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
            material_evidence = (
                envelope.production_evidence if isinstance(envelope, CaptureEnvelope) else None
            )
            if isinstance(envelope, CaptureEnvelope):
                _verify_observed_content(envelope, material, resolved_receipt)
            if (
                isinstance(envelope, CaptureEnvelope)
                and isinstance(material_evidence, ProviderInvocationCaptureEvidence)
                and material_evidence.interface_digest in WORKSPACE_FILE_INTERFACE_DIGESTS
            ):
                if not isinstance(resolved_receipt, ProviderProducerReceiptResolution) or (
                    resolved_receipt.source_read_receipt is None
                ):
                    raise CaptureFormatError("workspace.file source-read receipt is unavailable")
                try:
                    workspace_body = json.loads(material)
                except (UnicodeDecodeError, ValueError) as exc:
                    raise CaptureFormatError(
                        "workspace.file material is not canonical JSON"
                    ) from exc
                if canonical_bytes(workspace_body) != material or not isinstance(
                    workspace_body, dict
                ):
                    raise CaptureFormatError("workspace.file material is not canonical JSON")
                source_echo = workspace_body.get("source")
                source_read = resolved_receipt.source_read_receipt
                try:
                    output_commitment = ProviderInvocationOutputDigest.model_validate(
                        resolved_receipt.receipt.output
                    )
                except ValidationError as exc:
                    raise CaptureFormatError(
                        "workspace.file invocation output digest is unavailable"
                    ) from exc
                # The material was just verified canonical: its bytes are the encoding.
                if output_commitment.output_digest != provider_invocation_output_digest_of_bytes(
                    material
                ):
                    raise CaptureFormatError(
                        "workspace.file material differs from its invocation receipt"
                    )
                if not isinstance(source_echo, dict) or source_echo != {
                    "logical_source": source_read.logical_source,
                    "commitment_digest": source_read.derived_request_digest,
                    "bytes_digest": source_read.bytes_digest,
                    "byte_length": source_read.byte_length,
                }:
                    raise CaptureFormatError("workspace.file material mixes source attempts")
    if isinstance(envelope.source, CasSourceReference):
        if envelope.source.content_digest != envelope.commitment.digest:
            raise CaptureFormatError("Capture CAS source differs from its commitment")
    if (
        isinstance(envelope.source, CasSourceReference)
        and envelope.source.content_digest not in erased
    ):
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
    if isinstance(envelope.source, LedgerSourceReference):
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
            manifest = InputReceiptSetManifest.model_validate_json(manifest_bytes)
        except ValidationError as exc:
            raise CaptureFormatError("derived Capture receipt-set manifest is invalid") from exc
        if render_input_receipt_set_manifest(manifest) != manifest_bytes or (
            input_receipt_set_manifest_digest(manifest).tagged != manifest_digest
        ):
            raise CaptureFormatError("derived Capture receipt-set manifest does not reproduce")
    return envelope


__all__ = [
    "CaptureObservedContentMismatch",
    "ObservedContent",
    "ObservedContentBasis",
    "WEB_FETCH_V3_INTERFACE_DIGEST",
    "observed_content",
    "AcceptedCaptureContract",
    "CanonicalDuration",
    "CaptureContractLawResult",
    "CaptureContract",
    "CaptureContractV1",
    "CaptureContractAny",
    "is_coordinator_self_source_contract",
    "builtin_contract_for_write",
    "CaptureContractComponents",
    "CaptureShape",
    "CaptureReplay",
    "capture_shape_components",
    "contract_source_kinds",
    "contract_materialization_modes",
    "COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT_V1",
    "COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACTS",
    "foreign_source_capture_contract_v1",
    "CaptureBuildResult",
    "CaptureComponentRegistry",
    "CaptureEnvelopeAny",
    "CaptureEnvelopeV1",
    "CaptureEnvelope",
    "CaptureFormatError",
    "CaptureObjectStoreProtocol",
    "CaptureRetentionErasurePolicy",
    "CaptureRunCoordinateV1",
    "CaptureRunCoordinate",
    "CaptureSelectionBudget",
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
    "CAPTURE_COMPONENTS",
    "CaptureProductionEvidence",
    "CaptureProductionGap",
    "CaptureProductionRecord",
    "CaptureProductionUnproven",
    "VerifiedCaptureMemo",
    "required_capture_bodies",
    "DirectCaptureBuildResult",
    "DirectByteSpanSelection",
    "DirectClaimSelection",
    "DirectClaimSource",
    "DirectExternalSelection",
    "DirectForeignSourceSelection",
    "InputReceiptSetManifest",
    "ProcedureEgressCaptureEvidence",
    "ProcedureProducerReceiptProtocol",
    "ProducerReceiptResolverProtocol",
    "provider_capture_receipt_matches_occurrence",
    "ProviderResultToExternalCapture",
    "ProviderInvocationCaptureEvidence",
    "LedgerMaterialResolverProtocol",
    "SourceEffectiveTime",
    "build_coordinator_self_source_capture",
    "build_cas_capture",
    "build_procedure_capture_v2",
    "build_provider_external_capture_v2",
    "build_derived_cas_capture",
    "build_foreign_source_capture",
    "build_working_selection_capture",
    "build_ledger_capture",
    "capture_contract_digest",
    "capture_contract_is_self_asserted",
    "capture_is_coordinator_self_source",
    "capture_is_direct_selection_bound",
    "capture_is_direct_self_source",
    "classify_capture_reuse",
    "capture_contract_path",
    "authorize_erasure",
    "CaptureErasureLookupProtocol",
    "capture_erasure_authorized",
    "capture_retained_body_digests",
    "capture_component_pin",
    "capture_digest",
    "foreign_source_capture_contract",
    "foreign_source_contract_id",
    "capture_contract_successor_break",
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
