"""Exact-subject signed Claim evidence, distinct from change-set approval."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Literal, TypeAlias

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import (
    CasDigest,
    Sha256Value,
    canonical_bytes,
    typed_digest,
)
from cruxible_client.contracts.captures import CaptureObjectStoreProtocol
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.errors import PlaybillFormatError
from cruxible_client.contracts.principals import PrincipalRegistrySnapshot
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.providers import ProviderV1
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.types import PrincipalRecord

if TYPE_CHECKING:
    from cruxible_client.contracts.claims import AcceptedClaim

ClaimStance = Literal["support", "contradict", "unsure"]
ClaimAttestationBasis: TypeAlias = Literal["examined_existing", "new_capture"]
ClaimAttestationLineageStatus: TypeAlias = Literal["proven", "incomplete"]
AttestationGrade = Literal["verified_provider", "verified_principal"]
ClaimAttestationCoverage = Literal["exact_subject", "shell_stale"]

_SIGNATURE_RE = re.compile(r"^[0-9a-f]{128}$")
_KEY_ID_RE = re.compile(r"^(?:[a-z][a-z0-9_.:-]{0,127}|sha256:[0-9a-f]{64})$")


class ClaimAttestationError(PlaybillFormatError):
    """A ClaimAttestation is malformed, misbound, stale, or cryptographically invalid."""


class _StrictClaimAttestationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClaimAttestationStatement(_StrictClaimAttestationModel):
    tag: Literal["playbill-claim-attestation-v1"] = "playbill-claim-attestation-v1"
    instance_id: str
    referent_coordinate: AcceptedCoordinate
    subject: SemanticAddress
    subject_content_digest: str
    object_subject: SemanticAddress | None = None
    object_content_digest: str | None = None
    claim_statement_digest: str
    stance: ClaimStance
    provider_or_principal: ArtifactIdentity
    signing_key_id: str
    capture_digests: tuple[str, ...]
    observed_at: datetime
    valid_until: datetime | None = None

    @field_validator(
        "subject_content_digest",
        "object_content_digest",
        "claim_statement_digest",
    )
    @classmethod
    def _semantic_digest(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @field_validator("capture_digests")
    @classmethod
    def _captures(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("ascii"))):
            raise ValueError("ClaimAttestation Capture digests must be sorted and unique")
        for item in value:
            CasDigest.from_tagged(item)
        return value

    @field_validator("signing_key_id")
    @classmethod
    def _key_id(cls, value: str) -> str:
        if not _KEY_ID_RE.fullmatch(value):
            raise ValueError("ClaimAttestation signing_key_id is not canonical")
        return value

    @field_validator("observed_at", "valid_until")
    @classmethod
    def _time(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("ClaimAttestation times must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _shape(self) -> "ClaimAttestationStatement":
        if self.provider_or_principal.kind not in {"Principal", "Provider"}:
            raise ValueError("ClaimAttestation signer must be a Principal or Provider")
        if self.valid_until is not None and self.valid_until <= self.observed_at:
            raise ValueError("ClaimAttestation validity interval must be increasing")
        if (self.object_subject is None) != (self.object_content_digest is None):
            raise ValueError("ClaimAttestation object subject and digest must appear together")
        if self.stance in {"support", "contradict"} and not self.capture_digests:
            raise ValueError("support/contradict ClaimAttestations require exact evidence")
        return self


class ClaimAttestation(ClaimAttestationStatement):
    algorithm: Literal["ed25519"] = "ed25519"
    signature: str

    @field_validator("signature")
    @classmethod
    def _signature(cls, value: str) -> str:
        if not _SIGNATURE_RE.fullmatch(value):
            raise ValueError("ClaimAttestation signature must contain 64 bytes of lowercase hex")
        return value

    @property
    def statement(self) -> ClaimAttestationStatement:
        payload = self.model_dump(mode="json")
        payload.pop("algorithm")
        payload.pop("signature")
        return ClaimAttestationStatement.model_validate(payload)


def claim_attestation_statement_bytes(
    statement: ClaimAttestationStatement | ClaimAttestation,
) -> bytes:
    unsigned = statement.statement if isinstance(statement, ClaimAttestation) else statement
    return canonical_bytes(
        {
            "algorithm": "ed25519",
            "domain": "playbill-claim-attestation-signature-v1",
            "instance_id": unsigned.instance_id,
            "statement": unsigned.model_dump(mode="json"),
        }
    )


def render_claim_attestation(attestation: ClaimAttestation) -> bytes:
    return canonical_bytes(attestation.model_dump(mode="json"))


def claim_attestation_digest(attestation: ClaimAttestation) -> CasDigest:
    return CasDigest(hashlib.sha256(render_claim_attestation(attestation)).hexdigest())


def store_claim_attestation(
    attestation: ClaimAttestation,
    *,
    store: CaptureObjectStoreProtocol,
) -> str:
    metadata = store.store(render_claim_attestation(attestation))
    expected = claim_attestation_digest(attestation).tagged
    if metadata.digest != expected:
        raise ClaimAttestationError("ClaimAttestation CAS digest did not reproduce")
    return expected


def read_claim_attestation(
    digest: str,
    *,
    store: CaptureObjectStoreProtocol,
) -> ClaimAttestation:
    CasDigest.from_tagged(digest)
    content = store.read(
        digest,
        access=BodyAccessContext(principal_id="playbill-attestation", can_read_body=True),
    )
    try:
        attestation = ClaimAttestation.model_validate_json(content)
    except ValueError as exc:
        raise ClaimAttestationError("ClaimAttestation CAS object is invalid") from exc
    if render_claim_attestation(attestation) != content or (
        claim_attestation_digest(attestation).tagged != digest
    ):
        raise ClaimAttestationError("ClaimAttestation CAS object does not reproduce")
    return attestation


def accepted_referent_coordinates_from_tree(
    tree: Mapping[str, bytes],
    *,
    current: AcceptedCoordinate,
) -> frozenset[AcceptedCoordinate]:
    """Recover accepted bases recorded by immutable member-law evaluations."""

    coordinates = {current}
    for path in sorted(tree, key=lambda item: item.encode("utf-8")):
        if not re.fullmatch(r"changesets/cs-[0-9]{20}\.json", path):
            continue
        try:
            payload = json.loads(tree[path])
            law_evidence = payload.get("law_evidence", [])
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ClaimAttestationError(f"accepted change-set is malformed: {path}") from exc
        if not isinstance(law_evidence, list):
            continue
        for member in law_evidence:
            if not isinstance(member, dict):
                continue
            value = member.get("evaluation_coordinate")
            if not isinstance(value, dict):
                continue
            try:
                coordinates.add(
                    AcceptedCoordinate(
                        git_oid=value["git_oid"],
                        semantic_root=value["semantic_root"],
                        generation_root=value["generation_root"],
                        compiler_digest=value["compiler_digest"],
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ClaimAttestationError(
                    f"accepted law evaluation coordinate is malformed: {path}"
                ) from exc
    return frozenset(coordinates)


class VerifiedClaimAttestationV1(_StrictClaimAttestationModel):
    tag: Literal["playbill-verified-claim-attestation-v1"] = (
        "playbill-verified-claim-attestation-v1"
    )
    attestation_digest: str
    statement: ClaimAttestationStatement
    attestation_grade: AttestationGrade
    control_domain: str
    upstream_provenance: tuple[ArtifactIdentity, ...] = ()
    coverage: ClaimAttestationCoverage
    current: bool

    @field_validator("attestation_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        CasDigest.from_tagged(value)
        return value


# The evidence-door wire is parallel to the acceptance-time V1 carrier above.
# V1 bytes, parser, digest, signer, and verification remain historical law.
CLAIM_ATTESTATION_SIGNATURE_V2_DOMAIN = "playbill-claim-attestation-signature-v2"
CLAIM_ATTESTATION_STATEMENT_V2_DOMAIN = "playbill-claim-attestation-statement-v2"
CLAIM_ATTESTATION_ENVELOPE_V2_DOMAIN = "playbill-claim-attestation-envelope-v2"
CLAIM_ATTESTATION_VERIFICATION_ACCOUNT_V1_DOMAIN = (
    "playbill-claim-attestation-verification-account-v1"
)


class ClaimAttestationStatementV2(_StrictClaimAttestationModel):
    """Complete signed observation bound to one exact accepted Claim artifact."""

    tag: Literal["playbill-claim-attestation-v2"] = "playbill-claim-attestation-v2"
    instance_id: str
    referent_coordinate: AcceptedCoordinate
    claim_identity: ArtifactIdentity
    claim_artifact_digest: str
    claim_statement_digest: str
    subject_shell_digest: str
    object_shell_digest: str | None = None
    attesting_principal_id: str
    signing_key_digest: str
    attestation_basis: ClaimAttestationBasis
    stance: ClaimStance
    cited_capture_digests: tuple[str, ...]
    attested_at: datetime
    valid_until: datetime | None = None

    @field_validator(
        "claim_artifact_digest",
        "claim_statement_digest",
        "subject_shell_digest",
        "object_shell_digest",
        "signing_key_digest",
    )
    @classmethod
    def _sha256(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @field_validator("cited_capture_digests")
    @classmethod
    def _capture_set(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("ascii"))):
            raise ValueError("V2 ClaimAttestation captures must be ASCII-sorted and unique")
        for digest in value:
            CasDigest.from_tagged(digest)
        return value

    @field_validator("attested_at", "valid_until")
    @classmethod
    def _v2_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("V2 ClaimAttestation times must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _v2_shape(self) -> "ClaimAttestationStatementV2":
        if self.claim_identity.kind != "Claim":
            raise ValueError("V2 ClaimAttestation identity kind must be Claim")
        from cruxible_client.contracts.claims import claim_path

        claim_path(self.claim_identity.name)
        if self.valid_until is not None and self.valid_until <= self.attested_at:
            raise ValueError("V2 ClaimAttestation validity interval must be increasing")
        if self.attestation_basis == "new_capture" and not self.cited_capture_digests:
            raise ValueError("new_capture ClaimAttestations require at least one Capture")
        return self


class ClaimAttestationV2(_StrictClaimAttestationModel):
    tag: Literal["playbill-claim-attestation-envelope-v2"] = (
        "playbill-claim-attestation-envelope-v2"
    )
    statement: ClaimAttestationStatementV2
    algorithm: Literal["ed25519-v1"] = "ed25519-v1"
    signature: str

    @field_validator("signature")
    @classmethod
    def _v2_signature(cls, value: str) -> str:
        if not _SIGNATURE_RE.fullmatch(value):
            raise ValueError("V2 ClaimAttestation signature must be 64 lowercase-hex bytes")
        return value


class ClaimAttestationCaptureReferenceV1(_StrictClaimAttestationModel):
    tag: Literal["playbill-claim-attestation-capture-reference-v1"] = (
        "playbill-claim-attestation-capture-reference-v1"
    )
    capture_digest: str

    @field_validator("capture_digest")
    @classmethod
    def _capture_digest(cls, value: str) -> str:
        CasDigest.from_tagged(value)
        return value


class PreparedClaimAttestationRequestV1(_StrictClaimAttestationModel):
    tag: Literal["playbill-prepared-claim-attestation-request-v1"] = (
        "playbill-prepared-claim-attestation-request-v1"
    )
    claim_id: str
    attestation_basis: ClaimAttestationBasis
    stance: ClaimStance
    capture_references: tuple[ClaimAttestationCaptureReferenceV1, ...] = ()
    referent_coordinate: AcceptedCoordinate | None = None
    attested_at: datetime
    valid_until: datetime | None = None
    note: str | None = None

    @field_validator("capture_references")
    @classmethod
    def _references(
        cls, value: tuple[ClaimAttestationCaptureReferenceV1, ...]
    ) -> tuple[ClaimAttestationCaptureReferenceV1, ...]:
        digests = tuple(item.capture_digest for item in value)
        if digests != tuple(sorted(set(digests), key=lambda item: item.encode("ascii"))):
            raise ValueError("attestation Capture references must be ASCII-sorted and unique")
        return value

    @field_validator("attested_at", "valid_until")
    @classmethod
    def _prepared_time(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("prepared attestation times must be timezone-aware")
        return value

    @field_validator("note")
    @classmethod
    def _note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value or "\x00" in value or len(value.encode("utf-8")) > 4096:
            raise ValueError("attestation note must contain 1..4096 UTF-8 bytes and no NUL")
        return value

    @model_validator(mode="after")
    def _prepared_shape(self) -> "PreparedClaimAttestationRequestV1":
        if self.valid_until is not None and self.valid_until <= self.attested_at:
            raise ValueError("prepared attestation validity interval must be increasing")
        if self.attestation_basis == "new_capture" and not self.capture_references:
            raise ValueError("new_capture requests require Capture references")
        return self


class ClaimAttestationResolvedArtifactV1(_StrictClaimAttestationModel):
    tag: Literal["playbill-claim-attestation-resolved-artifact-v1"] = (
        "playbill-claim-attestation-resolved-artifact-v1"
    )
    identity: ArtifactIdentity
    artifact_digest: str
    live_at_append: bool

    @field_validator("artifact_digest")
    @classmethod
    def _artifact_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class VerifiedClaimAttestationV2(_StrictClaimAttestationModel):
    tag: Literal["playbill-verified-claim-attestation-v2"] = (
        "playbill-verified-claim-attestation-v2"
    )
    statement_digest: str
    envelope_digest: str
    statement: ClaimAttestationStatementV2
    referent_coordinate: AcceptedCoordinate
    append_coordinate: AcceptedCoordinate
    attesting_principal_id: str
    submitted_by: str
    current_at_append: bool
    resolved_artifacts: tuple[ClaimAttestationResolvedArtifactV1, ...] = ()
    admitted_capture_digests: tuple[str, ...] = ()
    recorded_at: datetime

    @field_validator("statement_digest", "envelope_digest")
    @classmethod
    def _account_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("admitted_capture_digests")
    @classmethod
    def _admitted(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.encode("ascii"))):
            raise ValueError("admitted Capture digests must be ASCII-sorted and unique")
        for digest in value:
            CasDigest.from_tagged(digest)
        return value

    @field_validator("recorded_at")
    @classmethod
    def _recorded_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("verification account recorded_at must be timezone-aware")
        return value


class ClaimAttestationAppendRequestV1(_StrictClaimAttestationModel):
    tag: Literal["playbill-claim-attestation-append-request-v1"] = (
        "playbill-claim-attestation-append-request-v1"
    )
    attestation: ClaimAttestationV2
    capture_references: tuple[ClaimAttestationCaptureReferenceV1, ...] = ()
    note: str | None = None

    @field_validator("capture_references")
    @classmethod
    def _append_references(
        cls, value: tuple[ClaimAttestationCaptureReferenceV1, ...]
    ) -> tuple[ClaimAttestationCaptureReferenceV1, ...]:
        digests = tuple(item.capture_digest for item in value)
        if digests != tuple(sorted(set(digests), key=lambda item: item.encode("ascii"))):
            raise ValueError("attestation Capture references must be ASCII-sorted and unique")
        return value

    @field_validator("note")
    @classmethod
    def _append_note(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value or "\x00" in value or len(value.encode("utf-8")) > 4096:
            raise ValueError("attestation note must contain 1..4096 UTF-8 bytes and no NUL")
        return value

    @model_validator(mode="after")
    def _request_shape(self) -> "ClaimAttestationAppendRequestV1":
        stated = self.attestation.statement.cited_capture_digests
        referenced = tuple(item.capture_digest for item in self.capture_references)
        if referenced and referenced != stated:
            raise ValueError("append Capture references differ from the signed statement")
        if self.attestation.statement.attestation_basis == "new_capture" and referenced != stated:
            raise ValueError("new_capture append requires every signed Capture reference")
        return self


class ClaimAttestationAppendResultV1(_StrictClaimAttestationModel):
    tag: Literal["playbill-claim-attestation-append-result-v1"] = (
        "playbill-claim-attestation-append-result-v1"
    )
    event_digest: str
    partition_digest: str
    statement_digest: str
    envelope_digest: str
    partition_sequence: int
    recorded_coordinate: AcceptedCoordinate
    recorded_head: str
    current_head: str
    submitted_by: str
    recorded_at: datetime

    @field_validator(
        "event_digest",
        "partition_digest",
        "statement_digest",
        "envelope_digest",
        "recorded_head",
        "current_head",
    )
    @classmethod
    def _result_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("recorded_at")
    @classmethod
    def _result_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("append result recorded_at must be timezone-aware")
        return value


def claim_attestation_v2_statement_bytes(
    statement: ClaimAttestationStatementV2 | ClaimAttestationV2,
) -> bytes:
    unsigned = statement.statement if isinstance(statement, ClaimAttestationV2) else statement
    return canonical_bytes(
        {
            "algorithm": "ed25519-v1",
            "domain": CLAIM_ATTESTATION_SIGNATURE_V2_DOMAIN,
            "instance_id": unsigned.instance_id,
            "statement": unsigned.model_dump(mode="json"),
        }
    )


def claim_attestation_v2_statement_digest(statement: ClaimAttestationStatementV2) -> str:
    payload = statement.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(
        Sha256Value,
        CLAIM_ATTESTATION_STATEMENT_V2_DOMAIN,
        payload,
    ).tagged


def claim_attestation_v2_envelope_digest(attestation: ClaimAttestationV2) -> str:
    payload = attestation.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(
        Sha256Value,
        CLAIM_ATTESTATION_ENVELOPE_V2_DOMAIN,
        payload,
    ).tagged


def claim_attestation_verification_account_digest(
    account: VerifiedClaimAttestationV2,
) -> str:
    payload = account.model_dump(mode="json")
    payload.pop("tag")
    return typed_digest(
        Sha256Value,
        CLAIM_ATTESTATION_VERIFICATION_ACCOUNT_V1_DOMAIN,
        payload,
    ).tagged


def verify_claim_attestation_v2_signature(
    attestation: ClaimAttestationV2,
    *,
    public_key: str,
) -> None:
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key)).verify(
            bytes.fromhex(attestation.signature),
            claim_attestation_v2_statement_bytes(attestation),
        )
    except (InvalidSignature, ValueError) as exc:
        raise ClaimAttestationError(
            "V2 ClaimAttestation Ed25519 signature does not verify"
        ) from exc


def verify_claim_attestation_v2_principal(
    attestation: ClaimAttestationV2,
    *,
    principal: PrincipalRecord,
) -> None:
    """Enforce the V2 ordinary-principal law at the crypto boundary."""

    statement = attestation.statement
    if principal.kind != "ordinary":
        raise ClaimAttestationError("V2 ClaimAttestation signer must be an ordinary principal")
    if principal.status != "active":
        raise ClaimAttestationError("V2 ClaimAttestation signer must be active")
    if principal.principal_id != statement.attesting_principal_id:
        raise ClaimAttestationError("V2 ClaimAttestation signer identity differs")
    if principal.public_key_digest != statement.signing_key_digest:
        raise ClaimAttestationError("V2 ClaimAttestation signing key digest differs")
    verify_claim_attestation_v2_signature(attestation, public_key=principal.public_key)


def verify_claim_attestation(
    attestation: ClaimAttestation,
    *,
    verification_time: datetime,
    expected_instance_id: str,
    expected_coordinate: AcceptedCoordinate,
    claim: AcceptedClaim,
    referent_subject_content_digest: str,
    referent_object_content_digest: str | None,
    principals: PrincipalRegistrySnapshot,
    providers: Mapping[str, ProviderV1],
    store: CaptureObjectStoreProtocol,
    current_subject_content_digest: str | None = None,
    current_object_content_digest: str | None = None,
) -> VerifiedClaimAttestationV1:
    """Verify exact statement/referent/key binding at one trusted explicit time.

    Provider key intervals are evaluated at the signed historical observation
    time so expired keys retain verifiable history. Revocation in accepted
    Provider state is the compromise boundary; callers must not treat ordinary
    expiry as retroactive revocation.
    """

    from cruxible_client.contracts.claims import SubjectClaimObject, claim_statement_digest

    if verification_time.tzinfo is None or verification_time.utcoffset() is None:
        raise ClaimAttestationError("ClaimAttestation verification time must be timezone-aware")
    statement = attestation.statement
    if statement.observed_at > verification_time:
        raise ClaimAttestationError("ClaimAttestation observed_at is in the future")
    if statement.instance_id != expected_instance_id:
        raise ClaimAttestationError("ClaimAttestation belongs to a different instance")
    if statement.referent_coordinate != expected_coordinate:
        raise ClaimAttestationError("ClaimAttestation refers to a different accepted coordinate")
    if statement.claim_statement_digest != claim.statement_digest or (
        claim_statement_digest(claim.claim.statement).tagged != claim.statement_digest
    ):
        raise ClaimAttestationError("ClaimAttestation names a different ClaimStatement")
    if statement.subject != claim.claim.statement.subject:
        raise ClaimAttestationError("ClaimAttestation subject differs from the ClaimStatement")
    expected_object = (
        claim.claim.statement.object.address
        if isinstance(claim.claim.statement.object, SubjectClaimObject)
        else None
    )
    if statement.object_subject != expected_object:
        raise ClaimAttestationError("ClaimAttestation object subject differs from the statement")
    if statement.subject_content_digest != referent_subject_content_digest or (
        statement.object_content_digest != referent_object_content_digest
    ):
        raise ClaimAttestationError("ClaimAttestation referent shell digests do not reproduce")
    if principals.semantic_root != expected_coordinate.semantic_root:
        raise ClaimAttestationError("principal registry differs from the referent coordinate")
    for digest in statement.capture_digests:
        if not store.verify(digest):
            raise ClaimAttestationError("ClaimAttestation evidence Capture is unavailable")

    if statement.provider_or_principal.kind == "Principal":
        try:
            principal = principals.require_active(statement.provider_or_principal.name)
        except Exception as exc:
            raise ClaimAttestationError("ClaimAttestation Principal is absent or revoked") from exc
        if statement.signing_key_id != principal.public_key_digest:
            raise ClaimAttestationError("ClaimAttestation Principal key identity differs")
        public_key = principal.public_key
        grade: AttestationGrade = "verified_principal"
        control_domain = f"principal.{principal.principal_id}"
        upstream: tuple[ArtifactIdentity, ...] = ()
    else:
        provider = providers.get(statement.provider_or_principal.qualified)
        if provider is None or provider.lifecycle.state != "live":
            raise ClaimAttestationError("ClaimAttestation Provider is absent or retired")
        try:
            key = provider.require_key(statement.signing_key_id, at=statement.observed_at)
        except PlaybillFormatError as exc:
            raise ClaimAttestationError(str(exc)) from exc
        public_key = key.public_key
        grade = "verified_provider"
        control_domain = provider.control_domain
        upstream = provider.upstream_provenance
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key)).verify(
            bytes.fromhex(attestation.signature),
            claim_attestation_statement_bytes(attestation),
        )
    except (InvalidSignature, ValueError) as exc:
        raise ClaimAttestationError("ClaimAttestation Ed25519 signature does not verify") from exc

    subject_current = current_subject_content_digest or referent_subject_content_digest
    object_current = (
        current_object_content_digest
        if current_object_content_digest is not None
        else referent_object_content_digest
    )
    coverage: ClaimAttestationCoverage = (
        "exact_subject"
        if subject_current == statement.subject_content_digest
        and object_current == statement.object_content_digest
        else "shell_stale"
    )
    return VerifiedClaimAttestationV1(
        attestation_digest=claim_attestation_digest(attestation).tagged,
        statement=statement,
        attestation_grade=grade,
        control_domain=control_domain,
        upstream_provenance=upstream,
        coverage=coverage,
        current=coverage == "exact_subject",
    )


__all__ = [
    "AttestationGrade",
    "ClaimAttestation",
    "ClaimAttestationAppendRequestV1",
    "ClaimAttestationAppendResultV1",
    "ClaimAttestationBasis",
    "ClaimAttestationCaptureReferenceV1",
    "ClaimAttestationCoverage",
    "ClaimAttestationError",
    "ClaimAttestationLineageStatus",
    "ClaimAttestationResolvedArtifactV1",
    "ClaimAttestationStatement",
    "ClaimAttestationStatementV2",
    "ClaimAttestationV2",
    "ClaimStance",
    "PreparedClaimAttestationRequestV1",
    "VerifiedClaimAttestationV1",
    "VerifiedClaimAttestationV2",
    "accepted_referent_coordinates_from_tree",
    "claim_attestation_digest",
    "claim_attestation_statement_bytes",
    "claim_attestation_v2_envelope_digest",
    "claim_attestation_v2_statement_bytes",
    "claim_attestation_v2_statement_digest",
    "claim_attestation_verification_account_digest",
    "read_claim_attestation",
    "render_claim_attestation",
    "store_claim_attestation",
    "verify_claim_attestation",
    "verify_claim_attestation_v2_principal",
    "verify_claim_attestation_v2_signature",
]
