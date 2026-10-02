"""A principal's signed consent to one bearer credential minted in its name.

A credential acts as exactly one principal. Minting one in a principal's name
needs that principal's authority: either the request already acts as it, or
the principal signs this statement with its registered key. An admin token
alone never suffices, so no operator can mint a credential that authors as
someone else.
"""

from __future__ import annotations

from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.principals import is_canonical_principal_id
from cruxible_client.contracts.temporal import format_datetime, parse_datetime

RUNTIME_CREDENTIAL_MINT_TAG = "cruxible-runtime-credential-mint-v1"

#: How far a signed mint statement's ``issued_at`` may sit from the daemon's
#: clock. A statement is also single-use, so this bounds only how long a
#: captured, never-submitted statement stays usable.
RUNTIME_CREDENTIAL_PROOF_MAX_SKEW_SECONDS = 300

RuntimeCredentialPermissionModeV1 = Literal["read_only", "governed_write", "graph_write", "admin"]


class RuntimeCredentialMintStatementV1(BaseModel):
    """Exactly the bytes a principal signs to consent to one credential."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["cruxible-runtime-credential-mint-v1"] = "cruxible-runtime-credential-mint-v1"
    instance_id: str = Field(min_length=1, max_length=256)
    principal_id: str
    permission_mode: RuntimeCredentialPermissionModeV1
    label: str = Field(min_length=1, max_length=256)
    # The exact string signed, never re-rendered: `format_datetime` UTC form.
    issued_at: str = Field(min_length=1, max_length=64)
    nonce: str = Field(min_length=16, max_length=128)

    @field_validator("principal_id")
    @classmethod
    def _principal_id(cls, value: str) -> str:
        if not is_canonical_principal_id(value):
            raise ValueError("principal_id must be a canonical lowercase identifier")
        return value

    @field_validator("issued_at")
    @classmethod
    def _issued_at(cls, value: str) -> str:
        parsed = parse_datetime(value)
        if parsed is None or format_datetime(parsed) != value:
            raise ValueError("issued_at must be a canonical UTC timestamp")
        return value


class RuntimeCredentialPrincipalProofV1(BaseModel):
    """A mint statement and the principal's Ed25519 signature over it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    statement: RuntimeCredentialMintStatementV1
    signature: str = Field(pattern=r"^[0-9a-f]{128}$")


def runtime_credential_mint_statement_bytes(statement: RuntimeCredentialMintStatementV1) -> bytes:
    """The canonical preimage; the tag inside it separates this signing domain."""

    return canonical_bytes(statement.model_dump(mode="json"))


def verify_runtime_credential_proof(
    proof: RuntimeCredentialPrincipalProofV1, *, public_key: str
) -> bool:
    """Whether ``public_key`` (raw Ed25519 hex) signed exactly this statement."""

    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key)).verify(
            bytes.fromhex(proof.signature),
            runtime_credential_mint_statement_bytes(proof.statement),
        )
    except (InvalidSignature, ValueError):
        return False
    return True


__all__ = [
    "RUNTIME_CREDENTIAL_MINT_TAG",
    "RUNTIME_CREDENTIAL_PROOF_MAX_SKEW_SECONDS",
    "RuntimeCredentialMintStatementV1",
    "RuntimeCredentialPermissionModeV1",
    "RuntimeCredentialPrincipalProofV1",
    "runtime_credential_mint_statement_bytes",
    "verify_runtime_credential_proof",
]
