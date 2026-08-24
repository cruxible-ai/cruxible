"""Exact-semantic-root principal registry snapshots and replay helpers."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_core.playbill.canonical import SemanticRoot
from cruxible_core.playbill.errors import PrincipalIntegrityError
from cruxible_core.playbill.principal_rendering import render_principal
from cruxible_core.playbill.types import PrincipalRecord

_PRINCIPAL_PATH_RE = re.compile(r"^principals/([a-z][a-z0-9_.-]{0,127})\.yaml$")


class PrincipalRegistrySnapshot(BaseModel):
    """The complete public-key state used to verify one accepted semantic root."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    semantic_root: str
    principals: tuple[PrincipalRecord, ...]

    @field_validator("semantic_root")
    @classmethod
    def _semantic_root(cls, value: str) -> str:
        SemanticRoot.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _ordered_registry(self) -> "PrincipalRegistrySnapshot":
        identifiers = [principal.principal_id for principal in self.principals]
        if identifiers != sorted(set(identifiers), key=lambda item: item.encode("utf-8")):
            raise ValueError("principal registry must be sorted and unique")
        daemon = [
            principal for principal in self.principals if "daemon" in principal.authority_roles
        ]
        if len(daemon) != 1 or daemon[0].principal_id != "daemon":
            raise ValueError("principal registry requires exactly the daemon principal")
        public_keys = [principal.public_key for principal in self.principals]
        if len(public_keys) != len(set(public_keys)):
            raise ValueError("principal registry public keys must be unique")
        return self

    def require_active(self, principal_id: str) -> PrincipalRecord:
        for principal in self.principals:
            if principal.principal_id == principal_id:
                if principal.status != "active":
                    raise PrincipalIntegrityError(
                        f"principal is not active at {self.semantic_root}: {principal_id}"
                    )
                return principal
        raise PrincipalIntegrityError(
            f"principal is absent at {self.semantic_root}: {principal_id}"
        )

    def key_history_reference(self, principal_id: str) -> str:
        self.require_active(principal_id)
        return f"principals/{principal_id}.yaml@{self.semantic_root}"


def principal_registry_from_tree(
    tree: Mapping[str, bytes],
    *,
    semantic_root: str,
) -> PrincipalRegistrySnapshot:
    """Parse only canonical principal files from an already verified generation tree."""

    principals: list[PrincipalRecord] = []
    seen_paths = False
    for path in sorted(tree, key=lambda item: item.encode("utf-8")):
        match = _PRINCIPAL_PATH_RE.fullmatch(path)
        if match is None:
            continue
        seen_paths = True
        content = tree[path]
        try:
            payload = json.loads(content)
            principal = PrincipalRecord.model_validate(payload)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise PrincipalIntegrityError(f"invalid principal registry artifact: {path}") from exc
        if principal.principal_id != match.group(1):
            raise PrincipalIntegrityError(f"principal path and identity differ: {path}")
        if render_principal(principal) != content:
            raise PrincipalIntegrityError(f"principal registry artifact is not canonical: {path}")
        principals.append(principal)
    if not seen_paths:
        raise PrincipalIntegrityError("generation tree contains no principal registry")
    try:
        return PrincipalRegistrySnapshot(
            semantic_root=semantic_root,
            principals=tuple(principals),
        )
    except ValueError as exc:
        raise PrincipalIntegrityError("principal registry snapshot is invalid") from exc


def parse_principal_record(content: bytes, *, path: str) -> PrincipalRecord:
    """Parse one exact canonical principal artifact and verify path identity."""

    match = _PRINCIPAL_PATH_RE.fullmatch(path)
    if match is None:
        raise PrincipalIntegrityError(f"principal path is not canonical: {path}")
    try:
        payload = json.loads(content)
        principal = PrincipalRecord.model_validate(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise PrincipalIntegrityError(f"invalid principal registry artifact: {path}") from exc
    if principal.principal_id != match.group(1):
        raise PrincipalIntegrityError(f"principal path and identity differ: {path}")
    if render_principal(principal) != content:
        raise PrincipalIntegrityError(f"principal registry artifact is not canonical: {path}")
    return principal


__all__ = [
    "PrincipalRegistrySnapshot",
    "parse_principal_record",
    "principal_registry_from_tree",
]
