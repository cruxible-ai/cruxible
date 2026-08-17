"""Exact-subject signed Claim evidence, distinct from change-set approval."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_core.playbill.artifacts import ArtifactIdentity
from cruxible_core.playbill.canonical import CasDigest, Sha256Value, canonical_bytes
from cruxible_core.playbill.captures import CaptureObjectStoreProtocol
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.errors import PlaybillFormatError
from cruxible_core.playbill.principals import PrincipalRegistrySnapshot
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.providers import ProviderV1
from cruxible_core.playbill.semantic import SemanticAddress

if TYPE_CHECKING:
    from cruxible_core.playbill.claims import AcceptedClaim

ClaimStance = Literal["support", "contradict", "unsure"]
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

    from cruxible_core.playbill.claims import SubjectClaimObject, claim_statement_digest

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
    "ClaimAttestationCoverage",
    "ClaimAttestationError",
    "ClaimAttestationStatement",
    "ClaimStance",
    "VerifiedClaimAttestationV1",
    "accepted_referent_coordinates_from_tree",
    "claim_attestation_digest",
    "claim_attestation_statement_bytes",
    "read_claim_attestation",
    "render_claim_attestation",
    "store_claim_attestation",
    "verify_claim_attestation",
]
