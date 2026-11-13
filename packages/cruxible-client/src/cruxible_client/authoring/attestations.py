"""Client-only Claim-attestation signing with local key custody."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cruxible_client.authoring.signing import _load_private_key as _load_key_file
from cruxible_client.contracts.attestation_evidence import ClaimAttestationEvidenceRequest
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestation,
    ClaimAttestationAppendRequest,
    ClaimAttestationAppendResult,
    ClaimAttestationCaptureReference,
    ClaimAttestationStatement,
    ClaimStance,
    PreparedClaimAttestationRequest,
    claim_attestation_v2_statement_bytes,
)
from cruxible_client.contracts.claims import (
    ClaimArtifact,
    ClaimUnsupportedFormatError,
    SubjectClaimObject,
    claim_artifact_digest,
    claim_citation_references,
    claim_statement_digest,
)
from cruxible_client.contracts.errors import FormatError, SigningKeyError
from cruxible_client.contracts.principals import PRINCIPAL_KEY_ENV
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.types import PrincipalRecord
from cruxible_client.contracts.write import Evidence
from cruxible_client.errors import InstanceScopeError


class LocalClaimAttestationKeyUnavailable(SigningKeyError):
    error_code = "cruxible.claim_attestation.local_signing_key_unavailable"


class ClaimAttestationReferentStaleError(FormatError):
    """The caller pinned a Claim revision that is no longer current at the
    head where evidence was prepared."""

    error_code = "cruxible.claim_attestation.referent_not_current"


class ClaimAttestationSigner(Protocol):
    @property
    def signer(self) -> str: ...

    @property
    def signing_key_id(self) -> str: ...

    def sign_claim_attestation_v2(
        self, statement: ClaimAttestationStatement
    ) -> ClaimAttestation: ...


def _outside_roots(path: Path, roots: Sequence[Path]) -> None:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise SigningKeyError("client ClaimAttestationV1 key is missing or unreadable") from exc
    for root in roots:
        try:
            boundary = root.resolve(strict=True)
        except OSError:
            boundary = root.resolve()
        if resolved == boundary or boundary in resolved.parents:
            raise SigningKeyError("client ClaimAttestationV1 key is inside a forbidden root")


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    return _load_key_file(
        path,
        label="client ClaimAttestationV1 key",
        ed25519_message="ClaimAttestationV1 signing requires an Ed25519 client key",
    )


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
            raise SigningKeyError(
                "local ClaimAttestationV1 key does not match accepted verification state"
            )
        return cls(
            signer=signer,
            signing_key_id=signing_key_id,
            private_key_path=private_key_path.resolve(strict=True),
            public_key=public_key,
        )

    def sign_claim_attestation_v2(
        self,
        statement: ClaimAttestationStatement,
    ) -> ClaimAttestation:
        if statement.attesting_principal_id != self.signer or (
            statement.signing_key_digest != self.signing_key_id
        ):
            raise SigningKeyError("V2 ClaimAttestationV1 names a different signer or key")
        private_key = _load_private_key(self.private_key_path)
        if private_key.public_key().public_bytes_raw().hex() != self.public_key:
            raise SigningKeyError("ClaimAttestationV1 key changed after signer initialization")
        signature = private_key.sign(claim_attestation_v2_statement_bytes(statement)).hex()
        return ClaimAttestation(statement=statement, signature=signature)


def local_attestation_signer_from_environment(
    client: Any,
    instance_id: str,
    *,
    workspace_root: Path | None = None,
    private_key_path: Path | None = None,
) -> LocalEd25519ClaimAttestationSigner:
    """Resolve the authenticated actor and its local key without wire disclosure.

    The key is ``private_key_path`` when the caller resolved one (the MCP
    server's folder identity), else ``CRUXIBLE_PRINCIPAL_KEY``.
    """

    raw = (
        str(private_key_path) if private_key_path is not None else os.environ.get(PRINCIPAL_KEY_ENV)
    )
    if raw is None:
        raise LocalClaimAttestationKeyUnavailable(
            f"{LocalClaimAttestationKeyUnavailable.error_code}: "
            f"set {PRINCIPAL_KEY_ENV} to the caller's local Ed25519 key, or run "
            "`cruxible init` in this folder: the MCP server and CLI started there act as "
            "its owner"
        )
    path = Path(raw)
    if not path.is_absolute():
        raise LocalClaimAttestationKeyUnavailable(
            f"{LocalClaimAttestationKeyUnavailable.error_code}: "
            f"{PRINCIPAL_KEY_ENV} must be an absolute path"
        )
    try:
        whoami = client.whoami(instance_id)
        principal = next(
            (
                item
                for item in principal_records(client, instance_id)
                if item.principal_id == whoami.actor_id
            ),
            None,
        )
        if principal is None or principal.status != "active" or principal.kind != "ordinary":
            raise SigningKeyError("authenticated actor is not an active ordinary principal")
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
    except (AttributeError, OSError, StopIteration, ValueError, SigningKeyError) as exc:
        if isinstance(exc, LocalClaimAttestationKeyUnavailable):
            raise
        raise LocalClaimAttestationKeyUnavailable(
            f"{LocalClaimAttestationKeyUnavailable.error_code}: {exc}"
        ) from exc


def principal_records(client: Any, instance_id: str) -> tuple[PrincipalRecord, ...]:
    """The accepted principal registry at the head, through orient's principals section."""

    from cruxible_client.contracts.orient import ORIENT_MAX_LIMIT

    records: list[PrincipalRecord] = []
    cursor: str | None = None
    while True:
        page = client.orient(
            instance_id, section="principals", limit=ORIENT_MAX_LIMIT, cursor=cursor
        )
        records.extend(
            PrincipalRecord.model_validate(item.model_dump(mode="json"))
            for item in page.principals or ()
        )
        if not page.truncated or page.next_cursor is None:
            return tuple(records)
        cursor = page.next_cursor


def accepted_proof(
    client: Any,
    instance_id: str,
    ref: str,
    *,
    at: Any = None,
    evaluation_time: datetime | None = None,
) -> dict[str, Any]:
    """One accepted artifact's full envelope and facts through ``get(detail="proof")``."""

    from cruxible_client.contracts import AcceptedCoordinate
    from cruxible_client.contracts.get_reads import GetRequest

    result = client.get(
        instance_id,
        request=GetRequest(
            ref=ref,
            detail="proof",
            at=(
                None
                if at is None
                else AcceptedCoordinate.model_validate(
                    at.model_dump(mode="json") if hasattr(at, "model_dump") else dict(at)
                )
            ),
            evaluation_time=evaluation_time,
            surface="sdk",
        ),
    )
    if result.proof is None:  # pragma: no cover - proof always answers an artifact
        raise ValueError(f"{ref} answered no proof")
    return dict(result.proof)


def _claim_from_public_view(view: Any) -> ClaimArtifact:
    facts = {item.get("schema_id"): item.get("value") for item in view.facts}
    identity = view.envelope.get("identity")
    artifact_format = view.envelope.get("format_tag")
    statement = facts.get("cruxible.claim.statement")
    backing = facts.get("cruxible.claim.backing")
    lifecycle = facts.get("cruxible.claim.lifecycle")
    if not (
        isinstance(identity, str)
        and isinstance(artifact_format, str)
        and isinstance(statement, dict)
        and isinstance(backing, dict)
        and isinstance(lifecycle, dict)
    ):
        raise ValueError("Claim read lacks its complete canonical artifact")
    if artifact_format != ClaimArtifact.model_fields["artifact_format"].default:
        raise ClaimUnsupportedFormatError(
            f"{ClaimUnsupportedFormatError.error_code}: {artifact_format!r}"
        )
    return ClaimArtifact.model_validate(
        {
            "artifact_format": artifact_format,
            "identity": {"kind": "Claim", "name": identity.removeprefix("Claim:")},
            "statement": statement,
            "backing": backing,
            "pins": lifecycle.get("pins"),
            "lifecycle": lifecycle.get("lifecycle"),
            **(
                {"retirement": lifecycle["retirement"]}
                if lifecycle.get("retirement") is not None
                else {}
            ),
        }
    )


def _claim_artifact_digest_at(
    client: Any, instance_id: str, claim_id: str, coordinate: AcceptedCoordinate
) -> str:
    """The Claim's exact accepted artifact digest at ``coordinate``."""

    from cruxible_client.contracts import ClaimViewRecord

    view = ClaimViewRecord.model_validate(
        accepted_proof(client, instance_id, claim_id, at=coordinate)
    )
    return claim_artifact_digest(_claim_from_public_view(view)).tagged


def prepare_claim_attestation(
    client: Any,
    instance_id: str,
    *,
    prepared: PreparedClaimAttestationRequest,
    signer: ClaimAttestationSigner,
) -> ClaimAttestation:
    """Bind and sign an exact accepted Claim without appending or accepting it."""

    whoami = client.whoami(instance_id)
    principals = principal_records(client, instance_id)
    principal = next(
        (item for item in principals if item.principal_id == whoami.actor_id),
        None,
    )
    if principal is None or principal.status != "active" or principal.kind != "ordinary":
        raise ValueError("Claim attestations require the active ordinary caller principal")
    if signer.signer != whoami.actor_id or signer.signing_key_id != principal.public_key_digest:
        raise ValueError("Claim attestation signer differs from the authenticated caller")
    from cruxible_client.contracts import ClaimViewRecord

    view = ClaimViewRecord.model_validate(
        accepted_proof(
            client,
            instance_id,
            prepared.claim_id,
            at=prepared.referent_coordinate,
            evaluation_time=prepared.attested_at,
        )
    )
    claim = _claim_from_public_view(view)

    def subject_shell_digest(address: Any) -> str:
        path = address.artifact_path
        prefix = "subjects/"
        if not path.startswith(prefix) or not path.endswith(".json"):
            raise ValueError("Claim subject address has no canonical Subject path")
        subject = accepted_proof(
            client, instance_id, path[len(prefix) : -len(".json")], at=view.coordinate
        )
        envelope = subject.get("envelope")
        digest = envelope.get("artifact_digest") if isinstance(envelope, Mapping) else None
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
    statement = ClaimAttestationStatement(
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
    return signer.sign_claim_attestation_v2(statement)


def append_prepared_claim_attestation(
    client: Any,
    instance_id: str,
    *,
    prepared: PreparedClaimAttestationRequest,
    signer: ClaimAttestationSigner,
) -> ClaimAttestationAppendResult:
    attestation = prepare_claim_attestation(client, instance_id, prepared=prepared, signer=signer)
    result = client.append_claim_attestation(
        instance_id,
        request=ClaimAttestationAppendRequest(
            attestation=attestation,
            capture_references=tuple(
                ClaimAttestationCaptureReference(capture_digest=item.capture_digest)
                for item in prepared.capture_references
            ),
        ),
    )
    return ClaimAttestationAppendResult.model_validate(result)


def attest_claim(
    client: Any,
    instance_id: str,
    *,
    claim_id: str,
    stance: ClaimStance,
    signer: ClaimAttestationSigner,
    attested_at: datetime,
    because: str | None = None,
    evidence: Evidence | None = None,
    workspace: Path | None = None,
    valid_until: datetime | None = None,
    referent_coordinate: AcceptedCoordinate | None = None,
) -> ClaimAttestationAppendResult:
    """Attest one Claim on write's evidence options, then sign and append it once (D-R42).

    ``because`` is the caller's own words, cited as a self-source Capture;
    ``evidence`` is one write evidence option (``self``, ``section`` with
    ``own_words``, ``file``, ``capture``, ``contract``), file and section
    evidence observed here from ``workspace``. The daemon makes or resolves
    those Captures at its head. With no ``referent_coordinate``, that head
    becomes the signed referent. A caller who names one (a pinned
    ``ClaimRef``) keeps it, whether or not evidence is supplied; if the Claim
    was revised since, so that pinned revision is no longer the one standing
    at the evidence head, this refuses (``ClaimAttestationReferentStaleError``)
    rather than sign the old revision over evidence prepared for the new one.
    With neither ``because`` nor ``evidence``, the attestation cites the
    Claim's own evidence.
    """

    claim = claim_id.removeprefix("Claim:")
    pinned_referent = referent_coordinate
    captures: tuple[ClaimAttestationCaptureReference, ...] = ()
    if because is not None or evidence is not None:
        from cruxible_client.authoring.write_evidence import observe_evidence

        if evidence is not None and workspace is not None:
            evidence = observe_evidence(evidence, workspace=workspace)
        made = client.prepare_claim_attestation_evidence(
            instance_id,
            request=ClaimAttestationEvidenceRequest(
                claim_id=claim, because=because, evidence=evidence
            ),
        )
        captures = tuple(
            ClaimAttestationCaptureReference(capture_digest=digest)
            for digest in made.capture_digests
        )
        if pinned_referent is None:
            # No explicit selection: the evidence head becomes the referent,
            # as before.
            referent_coordinate = made.referent_coordinate
        elif pinned_referent != made.referent_coordinate:
            # The caller named an exact revision. Evidence can only be made
            # at the live head, so confirm that revision is still the one
            # standing there before signing it; otherwise the statement
            # would cite evidence prepared against a different revision
            # than the one it claims to attest.
            if _claim_artifact_digest_at(
                client, instance_id, claim, pinned_referent
            ) != _claim_artifact_digest_at(client, instance_id, claim, made.referent_coordinate):
                raise ClaimAttestationReferentStaleError(
                    f"{ClaimAttestationReferentStaleError.error_code}: Claim {claim_id} was "
                    "revised between the pinned referent and the evidence head; re-select the "
                    "Claim's current revision before attesting"
                )
            referent_coordinate = pinned_referent
    return append_prepared_claim_attestation(
        client,
        instance_id,
        prepared=PreparedClaimAttestationRequest(
            claim_id=claim,
            stance=stance,
            capture_references=captures,
            referent_coordinate=referent_coordinate,
            attested_at=attested_at,
            valid_until=valid_until,
        ),
        signer=signer,
    )


__all__ = [
    "ClaimAttestationReferentStaleError",
    "ClaimAttestationSigner",
    "LocalClaimAttestationKeyUnavailable",
    "LocalEd25519ClaimAttestationSigner",
    "PRINCIPAL_KEY_ENV",
    "prepare_claim_attestation",
    "append_prepared_claim_attestation",
    "attest_claim",
    "local_attestation_signer_from_environment",
]
