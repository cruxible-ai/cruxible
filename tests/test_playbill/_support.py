"""Small custody-safe test helpers for Playbill bootstrap."""

from __future__ import annotations

from pathlib import Path

from cruxible_client.contracts.types import GitObjectFormat
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.keys import GeneratedKeyMaterial, generate_client_principal_key

FIXED_TIMESTAMP = "2026-08-10T12:00:00+00:00"


def generate_client(
    tmp_path: Path,
    *,
    managed_root: Path,
    principal_id: str,
    roles: tuple[str, ...],
) -> GeneratedKeyMaterial:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return generate_client_principal_key(
        tmp_path / f"client-custody-{principal_id}",
        principal_id=principal_id,
        kind="recovery" if roles == ("recovery",) else "ordinary",
        forbidden_roots=(workspace, managed_root),
    )


def initialize_local(
    tmp_path: Path,
    *,
    object_format: GitObjectFormat = "sha256",
) -> tuple[PlaybillInstance, GeneratedKeyMaterial]:
    managed_root = tmp_path / f"managed-{object_format}"
    owner = generate_client(
        tmp_path,
        managed_root=managed_root,
        principal_id="owner",
        roles=("owner",),
    )
    reviewer = generate_client(
        tmp_path,
        managed_root=managed_root,
        principal_id="reviewer",
        roles=("reviewer",),
    )
    instance = PlaybillInstance.initialize(
        managed_root,
        instance_id="inst_playbill_test",
        client_principals=(owner.principal, reviewer.principal),
        workspace_roots=(tmp_path / "workspace",),
        git_object_format=object_format,
        timestamp=FIXED_TIMESTAMP,
    )
    return instance, owner


def client_material(
    tmp_path: Path,
    instance: PlaybillInstance,
    *,
    principal_id: str = "reviewer",
) -> GeneratedKeyMaterial:
    """Recover test-only custody metadata for a bootstrap client key."""

    key_directory = tmp_path / f"client-custody-{principal_id}"
    return GeneratedKeyMaterial(
        principal=instance._recovered.head.principals.require_active(principal_id),
        private_key_path=key_directory / f"{principal_id}.ed25519",
        public_key_path=key_directory / f"{principal_id}.ed25519.pub",
    )
