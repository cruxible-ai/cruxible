"""Client-held approval signer seam; this module never belongs on a server request."""

from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cruxible_client.contracts.attestations import (
    ApprovalAttestation,
    ApprovalStatement,
    approval_statement_bytes,
)
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestation,
    ClaimAttestationStatement,
    claim_attestation_statement_bytes,
)
from cruxible_client.contracts.errors import PlaybillKeyError
from cruxible_core.playbill.keys import assert_outside_roots


class ApprovalSigner(Protocol):
    """Algorithm seam for a client that can sign one frozen approval statement."""

    @property
    def signer_id(self) -> str: ...

    @property
    def public_key(self) -> str: ...

    def sign(self, statement: ApprovalStatement) -> ApprovalAttestation: ...


@dataclass(frozen=True)
class LocalEd25519ApprovalSigner:
    """A local-file signer whose private path cannot enter a Pydantic wire model."""

    signer_id: str
    private_key_path: Path
    public_key: str

    @classmethod
    def open(
        cls,
        *,
        signer_id: str,
        private_key_path: Path,
        expected_public_key: str,
        forbidden_roots: Sequence[Path],
    ) -> LocalEd25519ApprovalSigner:
        """Validate custody and key identity without retaining private bytes."""

        assert_outside_roots(private_key_path, forbidden_roots)
        private_key = _load_private_key(private_key_path)
        public_key = private_key.public_key().public_bytes_raw().hex()
        if public_key != expected_public_key:
            raise PlaybillKeyError(
                "local approval key does not match the principal at the signing semantic root"
            )
        return cls(
            signer_id=signer_id,
            private_key_path=private_key_path.resolve(strict=True),
            public_key=public_key,
        )

    def sign(self, statement: ApprovalStatement) -> ApprovalAttestation:
        if statement.signer_id != self.signer_id:
            raise PlaybillKeyError("approval statement names a different signer")
        private_key = _load_private_key(self.private_key_path)
        if private_key.public_key().public_bytes_raw().hex() != self.public_key:
            raise PlaybillKeyError("approval key changed after signer initialization")
        signature = private_key.sign(approval_statement_bytes(statement)).hex()
        return ApprovalAttestation(**statement.model_dump(mode="json"), sig=signature)


class ClaimAttestationSigner(Protocol):
    """Client-held signer for an exact evidence disposition."""

    @property
    def signer(self) -> str: ...

    @property
    def signing_key_id(self) -> str: ...

    def sign_claim_attestation(self, statement: ClaimAttestationStatement) -> ClaimAttestation: ...


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


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    """Read a nonsymlink 0600 OpenSSH key through a no-follow descriptor."""

    if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
        raise PlaybillKeyError("client approval key must be a regular nonsymlink file")
    parent_mode = stat.S_IMODE(path.parent.stat().st_mode)
    if parent_mode & 0o077:
        raise PlaybillKeyError(
            "client approval key directory permissions must exclude group/world access"
        )
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PlaybillKeyError("client approval key permissions must exclude group/world access")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if opened.st_dev != metadata.st_dev or opened.st_ino != metadata.st_ino:
            raise PlaybillKeyError("client approval key changed while it was opened")
        content = bytearray()
        while chunk := os.read(descriptor, 64 * 1024):
            content.extend(chunk)
    except OSError as exc:
        raise PlaybillKeyError("client approval key is missing or unreadable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        private_key = serialization.load_ssh_private_key(bytes(content), password=None)
    except (TypeError, ValueError) as exc:
        raise PlaybillKeyError("client approval key is not an unencrypted OpenSSH key") from exc
    finally:
        for index in range(len(content)):
            content[index] = 0
    if not isinstance(private_key, Ed25519PrivateKey):
        raise PlaybillKeyError("Playbill approval requires an Ed25519 client key")
    return private_key


__all__ = [
    "ApprovalSigner",
    "ClaimAttestationSigner",
    "LocalEd25519ApprovalSigner",
    "LocalEd25519ClaimAttestationSigner",
]
