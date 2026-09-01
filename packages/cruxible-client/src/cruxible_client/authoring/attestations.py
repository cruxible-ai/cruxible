"""Client-only Claim-attestation signing with local key custody."""

from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cruxible_client.contracts.claim_attestations import (
    ClaimAttestationAppendRequestV1,
    ClaimAttestationAppendResultV1,
    ClaimAttestationCaptureReferenceV1,
    ClaimAttestationStatementV2,
    ClaimAttestationV2,
    PreparedClaimAttestationRequestV1,
    claim_attestation_v2_statement_bytes,
)
from cruxible_client.contracts.claims import (
    ClaimArtifactAny,
    ClaimArtifactV2,
    ClaimArtifactV3,
    ClaimUnsupportedFormatError,
    SubjectClaimObject,
    claim_artifact_digest,
    claim_citation_references,
    claim_statement_digest,
)
from cruxible_client.contracts.errors import PlaybillKeyError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.types import PrincipalRecord
from cruxible_client.errors import InstanceScopeError

PRINCIPAL_KEY_PATH_ENV = "CRUXIBLE_PRINCIPAL_KEY_PATH"


class LocalClaimAttestationKeyUnavailable(PlaybillKeyError):
    error_code = "playbill.claim_attestation.local_signing_key_unavailable"


class ClaimAttestationV2Signer(Protocol):
    @property
    def signer(self) -> str: ...

    @property
    def signing_key_id(self) -> str: ...

    def sign_claim_attestation_v2(
        self, statement: ClaimAttestationStatementV2
    ) -> ClaimAttestationV2: ...


def _outside_roots(path: Path, roots: Sequence[Path]) -> None:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise PlaybillKeyError("client ClaimAttestation key is missing or unreadable") from exc
    for root in roots:
        try:
            boundary = root.resolve(strict=True)
        except OSError:
            boundary = root.resolve()
        if resolved == boundary or boundary in resolved.parents:
            raise PlaybillKeyError("client ClaimAttestation key is inside a forbidden root")


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
        raise PlaybillKeyError("client ClaimAttestation key must be a regular nonsymlink file")
    if stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
        raise PlaybillKeyError(
            "client ClaimAttestation key directory permissions must exclude group/world access"
        )
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PlaybillKeyError(
            "client ClaimAttestation key permissions must exclude group/world access"
        )
    descriptor: int | None = None
    content = bytearray()
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if opened.st_dev != metadata.st_dev or opened.st_ino != metadata.st_ino:
            raise PlaybillKeyError("client ClaimAttestation key changed while it was opened")
        while chunk := os.read(descriptor, 64 * 1024):
            content.extend(chunk)
    except OSError as exc:
        raise PlaybillKeyError("client ClaimAttestation key is missing or unreadable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        private_key = serialization.load_ssh_private_key(bytes(content), password=None)
    except (TypeError, ValueError) as exc:
        raise PlaybillKeyError(
            "client ClaimAttestation key is not an unencrypted OpenSSH key"
        ) from exc
    finally:
        for index in range(len(content)):
            content[index] = 0
    if not isinstance(private_key, Ed25519PrivateKey):
        raise PlaybillKeyError("ClaimAttestation signing requires an Ed25519 client key")
    return private_key


@dataclass(frozen=True)
class LocalEd25519ClaimAttestationSigner:
    signer: str
    signing_key_id: str
    private_key_path: Path
    public_key: str

    @classmethod
    def open(
        cls,
        *,
        signer: str,
        signing_key_id: str,
        private_key_path: Path,
        expected_public_key: str,
        forbidden_roots: Sequence[Path],
    ) -> "LocalEd25519ClaimAttestationSigner":
        _outside_roots(private_key_path, forbidden_roots)
        private_key = _load_private_key(private_key_path)
        public_key = private_key.public_key().public_bytes_raw().hex()
        if public_key != expected_public_key:
            raise PlaybillKeyError(
                "local ClaimAttestation key does not match accepted verification state"
            )
        return cls(
            signer=signer,
            signing_key_id=signing_key_id,
            private_key_path=private_key_path.resolve(strict=True),
            public_key=public_key,
        )

    def sign_claim_attestation_v2(
        self,
        statement: ClaimAttestationStatementV2,
    ) -> ClaimAttestationV2:
        if statement.attesting_principal_id != self.signer or (
            statement.signing_key_digest != self.signing_key_id
        ):
            raise PlaybillKeyError("V2 ClaimAttestation names a different signer or key")
        private_key = _load_private_key(self.private_key_path)
        if private_key.public_key().public_bytes_raw().hex() != self.public_key:
            raise PlaybillKeyError("ClaimAttestation key changed after signer initialization")
        signature = private_key.sign(claim_attestation_v2_statement_bytes(statement)).hex()
        return ClaimAttestationV2(statement=statement, signature=signature)


def local_attestation_signer_from_environment(
    client: Any,
    instance_id: str,
    *,
    workspace_root: Path | None = None,
) -> LocalEd25519ClaimAttestationSigner:
    """Resolve the authenticated actor and its local key without wire disclosure."""

    raw = os.environ.get(PRINCIPAL_KEY_PATH_ENV)
    if raw is None:
        raise LocalClaimAttestationKeyUnavailable(
            f"{LocalClaimAttestationKeyUnavailable.error_code}: "
            f"set {PRINCIPAL_KEY_PATH_ENV} to the caller's local Ed25519 key"
        )
    path = Path(raw)
    if not path.is_absolute():
        raise LocalClaimAttestationKeyUnavailable(
            f"{LocalClaimAttestationKeyUnavailable.error_code}: "
            f"{PRINCIPAL_KEY_PATH_ENV} must be an absolute path"
        )
    try:
        whoami = client.playbill_whoami(instance_id)
        listed = client.list_playbill_principals(instance_id)
        principal = next(
            (
                PrincipalRecord.model_validate(item)
                for item in listed.principals
                if item.get("principal_id") == whoami.actor_id
            ),
            None,
        )
        if principal is None or principal.status != "active" or principal.kind != "ordinary":
            raise PlaybillKeyError("authenticated actor is not an active ordinary principal")
        # Preserve the daemon-state custody boundary whenever the unscoped
        # endpoint is available. Instance-scoped credentials deliberately
        # cannot call it, but that transport limitation must not make the
        # attestation command unusable for an otherwise authorized actor.
        try:
            daemon_state_root = Path(client.server_info().state_root)
        except InstanceScopeError:
            daemon_state_root = None
        roots = tuple(root for root in (workspace_root, daemon_state_root) if root is not None)
        return LocalEd25519ClaimAttestationSigner.open(
            signer=whoami.actor_id,
            signing_key_id=principal.public_key_digest,
            private_key_path=path,
            expected_public_key=principal.public_key,
            forbidden_roots=roots,
        )
    except (AttributeError, OSError, StopIteration, ValueError, PlaybillKeyError) as exc:
        if isinstance(exc, LocalClaimAttestationKeyUnavailable):
            raise
        raise LocalClaimAttestationKeyUnavailable(
            f"{LocalClaimAttestationKeyUnavailable.error_code}: {exc}"
        ) from exc


def _claim_from_public_view(view: Any) -> ClaimArtifactAny:
    facts = {item.get("schema_id"): item.get("value") for item in view.facts}
    identity = view.envelope.get("identity")
    artifact_format = view.envelope.get("format_tag")
    statement = facts.get("playbill.claim.statement")
    backing = facts.get("playbill.claim.backing")
    lifecycle = facts.get("playbill.claim.lifecycle")
    if not (
        isinstance(identity, str)
        and isinstance(artifact_format, str)
        and isinstance(statement, dict)
        and isinstance(backing, dict)
        and isinstance(lifecycle, dict)
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
    return model.model_validate(
        {
            "artifact_format": artifact_format,
            "identity": {"kind": "Claim", "name": identity.removeprefix("Claim:")},
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


def append_prepared_claim_attestation(
    client: Any,
    instance_id: str,
    *,
    prepared: PreparedClaimAttestationRequestV1,
    signer: ClaimAttestationV2Signer,
) -> ClaimAttestationAppendResultV1:
    """Lower a digest-free client request to the exact signed append wire."""

    whoami = client.playbill_whoami(instance_id)
    principals = tuple(
        PrincipalRecord.model_validate(item)
        for item in client.list_playbill_principals(instance_id).principals
    )
    principal = next(
        (item for item in principals if item.principal_id == whoami.actor_id),
        None,
    )
    if principal is None or principal.status != "active" or principal.kind != "ordinary":
        raise ValueError("Claim attestations require the active ordinary caller principal")
    if signer.signer != whoami.actor_id or signer.signing_key_id != principal.public_key_digest:
        raise ValueError("Claim attestation signer differs from the authenticated caller")
    view = client.get_playbill_claim(
        instance_id,
        prepared.claim_id,
        at=prepared.referent_coordinate,
        evaluation_time=prepared.attested_at.isoformat(),
    )
    claim = _claim_from_public_view(view)

    def subject_shell_digest(address: Any) -> str:
        path = address.artifact_path
        prefix = "subjects/"
        if not path.startswith(prefix) or not path.endswith(".json"):
            raise ValueError("Claim subject address has no canonical Subject path")
        kind, subject_id = path[len(prefix) : -len(".json")].split("/", maxsplit=1)
        subject = client.get_playbill_subject(
            instance_id,
            kind,
            subject_id,
            at=view.coordinate,
        )
        digest = subject.envelope.get("artifact_digest")
        if not isinstance(digest, str):
            raise ValueError("Subject read lacks its exact artifact digest")
        return digest

    cited = tuple(item.capture_digest for item in prepared.capture_references)
    if prepared.attestation_basis == "examined_existing":
        cited = tuple(
            sorted(
                {
                    item.capture_digest
                    for item in claim_citation_references(claim)
                    if not hasattr(item, "role") or item.role == "evidence"
                },
                key=lambda item: item.encode("ascii"),
            )
        )
    statement = ClaimAttestationStatementV2(
        instance_id=instance_id,
        referent_coordinate=AcceptedCoordinate.model_validate(
            view.coordinate.model_dump(mode="json")
        ),
        claim_identity=claim.identity,
        claim_artifact_digest=claim_artifact_digest(claim).tagged,
        claim_statement_digest=claim_statement_digest(claim.statement).tagged,
        subject_shell_digest=subject_shell_digest(claim.statement.subject),
        object_shell_digest=(
            subject_shell_digest(claim.statement.object.address)
            if isinstance(claim.statement.object, SubjectClaimObject)
            else None
        ),
        attesting_principal_id=whoami.actor_id,
        signing_key_digest=principal.public_key_digest,
        attestation_basis=prepared.attestation_basis,
        stance=prepared.stance,
        cited_capture_digests=cited,
        attested_at=prepared.attested_at,
        valid_until=prepared.valid_until,
    )
    result = client.append_playbill_claim_attestation(
        instance_id,
        request=ClaimAttestationAppendRequestV1(
            attestation=signer.sign_claim_attestation_v2(statement),
            capture_references=(
                tuple(
                    ClaimAttestationCaptureReferenceV1(capture_digest=item.capture_digest)
                    for item in prepared.capture_references
                )
                if prepared.attestation_basis == "new_capture"
                else ()
            ),
            note=prepared.note,
        ),
    )
    return ClaimAttestationAppendResultV1.model_validate(result)


__all__ = [
    "ClaimAttestationV2Signer",
    "LocalClaimAttestationKeyUnavailable",
    "LocalEd25519ClaimAttestationSigner",
    "PRINCIPAL_KEY_PATH_ENV",
    "append_prepared_claim_attestation",
    "local_attestation_signer_from_environment",
]
