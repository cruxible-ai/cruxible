"""Client-held approval signer seam; this module never belongs on a server request."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cruxible_client.authoring.signing import (
    ApprovalSigner as ApprovalSigner,
)
from cruxible_client.authoring.signing import (
    LocalEd25519ApprovalSigner as LocalEd25519ApprovalSigner,
)
from cruxible_client.authoring.signing import (
    _load_private_key,
    assert_outside_roots,
)
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestation,
    ClaimAttestationStatement,
    ClaimAttestationStatementV2,
    ClaimAttestationV2,
    claim_attestation_statement_bytes,
    claim_attestation_v2_statement_bytes,
)
from cruxible_client.contracts.errors import PlaybillKeyError


class ClaimAttestationSigner(Protocol):
    """Client-held signer for an exact evidence disposition."""

    @property
    def signer(self) -> str: ...

    @property
    def signing_key_id(self) -> str: ...

    def sign_claim_attestation(self, statement: ClaimAttestationStatement) -> ClaimAttestation: ...

    def sign_claim_attestation_v2(
        self, statement: ClaimAttestationStatementV2
    ) -> ClaimAttestationV2: ...


@dataclass(frozen=True)
class LocalEd25519ClaimAttestationSigner:
    """Use the same custody checks while preserving a separate signature domain."""

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
        assert_outside_roots(private_key_path, forbidden_roots)
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

    def sign_claim_attestation(
        self,
        statement: ClaimAttestationStatement,
    ) -> ClaimAttestation:
        if statement.provider_or_principal.qualified != self.signer or (
            statement.signing_key_id != self.signing_key_id
        ):
            raise PlaybillKeyError("ClaimAttestation statement names a different signer or key")
        private_key = _load_private_key(self.private_key_path)
        if private_key.public_key().public_bytes_raw().hex() != self.public_key:
            raise PlaybillKeyError("ClaimAttestation key changed after signer initialization")
        signature = private_key.sign(claim_attestation_statement_bytes(statement)).hex()
        return ClaimAttestation(
            **statement.model_dump(mode="json"),
            signature=signature,
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


__all__ = [
    "ApprovalSigner",
    "ClaimAttestationSigner",
    "LocalEd25519ApprovalSigner",
    "LocalEd25519ClaimAttestationSigner",
]
