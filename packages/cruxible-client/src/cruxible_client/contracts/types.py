"""Strict protocol and inspection types for Playbill bootstrap."""

from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.canonical import Sha256Value

GitObjectFormat = Literal["sha1", "sha256"]
AuthorityMode = Literal["legacy", "ledger", "inactive"]
PrincipalRole = Literal["daemon", "owner", "reviewer", "recovery"]
OperatingProfile = Literal["local", "cloud"]
RecoveryPosture = Literal["narrowed-no-recovery", "recovery-configured"]

_PRINCIPAL_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_LOWER_HEX_32_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PrincipalRecord(StrictModel):
    """One public principal record stored in the signed genesis tree."""

    tag: Literal["playbill-principal-v1"] = "playbill-principal-v1"
    principal_id: str
    algorithm: Literal["ed25519-v1"] = "ed25519-v1"
    public_key: str
    authority_roles: tuple[PrincipalRole, ...]
    status: Literal["active", "revoked"] = "active"

    @field_validator("principal_id")
    @classmethod
    def _principal_id(cls, value: str) -> str:
        if not _PRINCIPAL_ID_RE.fullmatch(value):
            raise ValueError("principal_id must be a canonical lowercase identifier")
        return value

    @field_validator("public_key")
    @classmethod
    def _public_key(cls, value: str) -> str:
        if not _LOWER_HEX_32_RE.fullmatch(value):
            raise ValueError("public_key must contain 32 bytes of lowercase Ed25519 hex")
        return value

    @field_validator("authority_roles")
    @classmethod
    def _roles(cls, value: tuple[PrincipalRole, ...]) -> tuple[PrincipalRole, ...]:
        if not value:
            raise ValueError("authority_roles must not be empty")
        if tuple(sorted(set(value))) != value:
            raise ValueError("authority_roles must be sorted and unique")
        if "daemon" in value and value != ("daemon",):
            raise ValueError("daemon authority cannot be combined with client roles")
        return value

    @property
    def public_key_digest(self) -> str:
        digest = hashlib.sha256(bytes.fromhex(self.public_key)).hexdigest()
        return f"sha256:{digest}"


class PlaybillTrustRoot(StrictModel):
    """Out-of-band inputs required to reopen and verify generation zero."""

    tag: Literal["playbill-trust-root-v1"] = "playbill-trust-root-v1"
    instance_id: str = Field(min_length=1, max_length=256)
    daemon_public_key: str
    principals: tuple[PrincipalRecord, ...]

    @field_validator("instance_id")
    @classmethod
    def _instance_id(cls, value: str) -> str:
        normalized = value.strip()
        if normalized != value or not normalized:
            raise ValueError("instance_id must be nonblank and already normalized")
        return value

    @field_validator("daemon_public_key")
    @classmethod
    def _daemon_public_key(cls, value: str) -> str:
        if not _LOWER_HEX_32_RE.fullmatch(value):
            raise ValueError("daemon_public_key must contain 32 bytes of lowercase hex")
        return value

    @model_validator(mode="after")
    def _principal_registry(self) -> "PlaybillTrustRoot":
        ids = [record.principal_id for record in self.principals]
        if ids != sorted(set(ids)):
            raise ValueError("principals must be sorted and unique by principal_id")
        daemon = [record for record in self.principals if record.principal_id == "daemon"]
        if len(daemon) != 1:
            raise ValueError("trust root must contain exactly one daemon principal")
        if daemon[0].public_key != self.daemon_public_key:
            raise ValueError("daemon principal differs from daemon_public_key")
        if daemon[0].authority_roles != ("daemon",):
            raise ValueError("daemon principal must carry only daemon authority")
        if not any("owner" in record.authority_roles for record in self.principals):
            raise ValueError("trust root requires at least one owner principal")
        return self


class StorageLayout(StrictModel):
    """Relative managed paths; no private filename is exposed by inspection."""

    ledger: str = "ledger.git"
    projections: str = "projections"
    cas: str = "cas"
    exhaust: str = "exhaust"
    credentials: str = "credentials"
    leases: str = "leases"

    @field_validator("ledger", "projections", "cas", "exhaust", "credentials", "leases")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            not value
            or str(path) != value
            or path.is_absolute()
            or "\\" in value
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError("storage paths must be canonical relative POSIX paths")
        return value

    @model_validator(mode="after")
    def _unique_paths(self) -> "StorageLayout":
        values = list(self.model_dump().values())
        if len(values) != len(set(values)):
            raise ValueError("storage paths must be unique")
        parts = [PurePosixPath(value).parts for value in values]
        for index, left in enumerate(parts):
            for right in parts[index + 1 :]:
                shorter, longer = sorted((left, right), key=len)
                if longer[: len(shorter)] == shorter:
                    raise ValueError("storage paths must not contain one another")
        return self


class CompilerCoordinate(StrictModel):
    """Validated semantic compiler/schema coordinate outside the bootstrap preimage.

    ``implementation`` names the reference semantics whose rule digest is
    accepted. The physical assembler engine that realizes those semantics is
    recorded separately as nonlogical projection build metadata.
    """

    tag: Literal["playbill-compiler-v1"] = "playbill-compiler-v1"
    implementation: Literal["python-reference"] = "python-reference"
    schema_version: Literal[1] = 1
    rule_digest: str

    @field_validator("rule_digest")
    @classmethod
    def _rule_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class GenesisCoordinate(StrictModel):
    """Persisted, recomputable generation-zero coordinate."""

    git_oid: str
    bootstrap_root: str
    semantic_root: str
    generation_root: str

    @field_validator("git_oid")
    @classmethod
    def _git_oid(cls, value: str) -> str:
        if not _GIT_OID_RE.fullmatch(value):
            raise ValueError("git_oid must be lowercase SHA-1 or SHA-256 hex")
        return value

    @field_validator("bootstrap_root", "semantic_root", "generation_root")
    @classmethod
    def _sha256_coordinate(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class GenerationDescriptor(StrictModel):
    """Exact `playbill-gen-v1` preimage fields, using raw digest hex."""

    tag: Literal["playbill-gen-v1"] = "playbill-gen-v1"
    semantic_root: str
    git_oid: str
    parent_generation_root: str

    @field_validator("semantic_root", "parent_generation_root")
    @classmethod
    def _root(cls, value: str) -> str:
        if not _LOWER_HEX_32_RE.fullmatch(value):
            raise ValueError("generation descriptor roots must be lowercase SHA-256 hex")
        return value

    @field_validator("git_oid")
    @classmethod
    def _oid(cls, value: str) -> str:
        if not _GIT_OID_RE.fullmatch(value):
            raise ValueError("generation descriptor Git OID is malformed")
        return value


class AuthorityMatrix(StrictModel):
    """One scalar write authority per migration family."""

    families: dict[str, AuthorityMode]

    @field_validator("families")
    @classmethod
    def _families(cls, value: dict[str, AuthorityMode]) -> dict[str, AuthorityMode]:
        required = {"config", "documents", "graph", "procedures", "workflows"}
        if set(value) != required:
            raise ValueError(
                "authority matrix must name exactly config, documents, graph, "
                "procedures, and workflows"
            )
        return dict(sorted(value.items()))


def initial_authority_matrix() -> AuthorityMatrix:
    return AuthorityMatrix(
        families={
            "config": "legacy",
            "documents": "inactive",
            "graph": "legacy",
            "procedures": "legacy",
            "workflows": "legacy",
        }
    )


class PlaybillDescriptor(StrictModel):
    """The strict operational descriptor for one opt-in managed instance."""

    tag: Literal["playbill-instance-v1"] = "playbill-instance-v1"
    format_version: Literal[1] = 1
    instance_id: str
    git_object_format: GitObjectFormat
    daemon_algorithm: Literal["ed25519-v1"] = "ed25519-v1"
    daemon_public_key: str
    compiler: CompilerCoordinate
    authority: AuthorityMatrix
    operating_profile: OperatingProfile
    recovery_posture: RecoveryPosture
    storage: StorageLayout
    genesis: GenesisCoordinate

    @field_validator("instance_id")
    @classmethod
    def _instance_id(cls, value: str) -> str:
        normalized = value.strip()
        if normalized != value or not normalized:
            raise ValueError("instance_id must be nonblank and already normalized")
        return value

    @field_validator("daemon_public_key")
    @classmethod
    def _daemon_public_key(cls, value: str) -> str:
        if not _LOWER_HEX_32_RE.fullmatch(value):
            raise ValueError("daemon_public_key must contain 32 bytes of lowercase hex")
        return value


class PrincipalInspection(StrictModel):
    principal_id: str
    algorithm: str
    authority_roles: tuple[PrincipalRole, ...]
    status: str
    public_key_digest: str


class PlaybillInspection(StrictModel):
    """Credential-safe read model for internal/service-level inspection."""

    descriptor_tag: str
    format_version: int
    instance_id: str
    git_object_format: GitObjectFormat
    head_oid: str
    bootstrap_root: str
    semantic_root: str
    generation_root: str
    compiler: CompilerCoordinate
    authority: AuthorityMatrix
    operating_profile: OperatingProfile
    recovery_posture: RecoveryPosture
    principals: tuple[PrincipalInspection, ...]
    managed_root: str
    storage_directories: dict[str, str]
    daemon_private_key_present: bool


__all__ = [
    "AuthorityMatrix",
    "AuthorityMode",
    "CompilerCoordinate",
    "GenesisCoordinate",
    "GenerationDescriptor",
    "GitObjectFormat",
    "OperatingProfile",
    "PlaybillDescriptor",
    "PlaybillInspection",
    "PlaybillTrustRoot",
    "PrincipalInspection",
    "PrincipalRecord",
    "PrincipalRole",
    "RecoveryPosture",
    "StorageLayout",
    "initial_authority_matrix",
]
