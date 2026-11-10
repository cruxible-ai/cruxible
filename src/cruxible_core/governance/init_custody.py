"""Client key custody for `init`, shared by the CLI and the MCP adapter.

Each init principal's key is generated in its key directory beside a resume
marker naming the init target (transport, instance, principal, public key). An
init interrupted after the keys exist resumes with the same keys; key material
without this init's marker is never reused.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.errors import SigningKeyError
from cruxible_client.contracts.types import PrincipalKind
from cruxible_core.governance.keys import (
    ClientPrincipalKeyTarget,
    GeneratedKeyMaterial,
    adopt_client_principal_key,
    generate_client_principal_key,
    validate_client_principal_key_target,
)


def init_resume_marker(target: ClientPrincipalKeyTarget) -> Path:
    return target.directory / f".cruxible-init-resume-{target.principal.principal_id}.json"


def _init_resume_payload(
    target: ClientPrincipalKeyTarget,
    *,
    transport: str,
    instance_id: str,
    public_key: str,
) -> dict[str, str]:
    return {
        "tag": "playbill-init-key-resume-v1",
        "transport": transport,
        "instance_id": instance_id,
        "principal_id": target.principal.principal_id,
        "kind": target.principal.kind,
        "public_key": public_key,
    }


def _readinit_resume_marker(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SigningKeyError(
            f"Cruxible init retry marker is missing or malformed: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise SigningKeyError(f"Cruxible init retry marker is missing or malformed: {path}")
    return payload


def _writeinit_resume_marker(path: Path, payload: Mapping[str, str]) -> None:
    try:
        with path.open("xb") as handle:
            handle.write(canonical_bytes(dict(payload)) + b"\n")
        path.chmod(0o600)
    except FileExistsError as exc:
        raise SigningKeyError(f"refusing to replace Cruxible init retry marker: {path}") from exc


def _adopt_init_retry_key(
    target: ClientPrincipalKeyTarget,
    *,
    forbidden_roots: tuple[Path, ...],
    transport: str,
    instance_id: str,
) -> GeneratedKeyMaterial:
    marker = init_resume_marker(target)
    if not marker.is_file() or marker.is_symlink():
        raise SigningKeyError(
            "Cruxible is already initialized for this custody, or the existing key material "
            "was provisioned independently; refusing to reuse it without this init's retry "
            f"marker: {marker}"
        )
    material = adopt_client_principal_key(
        target.directory,
        principal_id=target.principal.principal_id,
        kind=target.principal.kind,
        forbidden_roots=forbidden_roots,
    )
    expected = _init_resume_payload(
        target,
        transport=transport,
        instance_id=instance_id,
        public_key=material.principal.public_key,
    )
    if _readinit_resume_marker(marker) != expected:
        raise SigningKeyError(
            f"existing key material belongs to a different Cruxible init target: {marker}"
        )
    return material


def prepare_init_custody(
    *,
    transport: str,
    instance_id: str,
    specifications: tuple[tuple[Path, str, PrincipalKind], ...],
    forbidden_roots: tuple[Path, ...],
) -> tuple[tuple[GeneratedKeyMaterial, ...], tuple[Path, ...]]:
    targets = tuple(
        validate_client_principal_key_target(
            directory,
            principal_id=principal_id,
            kind=kind,
            forbidden_roots=forbidden_roots,
        )
        for directory, principal_id, kind in specifications
    )
    if len({target.principal.principal_id for target in targets}) != len(targets):
        raise SigningKeyError("Cruxible init principal IDs must be distinct")
    key_paths = tuple(
        path for target in targets for path in (target.private_key_path, target.public_key_path)
    )
    if len(set(key_paths)) != len(key_paths):
        raise SigningKeyError("Cruxible init custody key paths must be distinct")

    prepared: list[GeneratedKeyMaterial | None] = []
    for target in targets:
        private_exists = target.private_key_path.exists()
        public_exists = target.public_key_path.exists()
        marker = init_resume_marker(target)
        if private_exists != public_exists:
            raise SigningKeyError(
                f"Cruxible init retry requires a complete key pair for "
                f"{target.principal.principal_id}"
            )
        if private_exists:
            prepared.append(
                _adopt_init_retry_key(
                    target,
                    forbidden_roots=forbidden_roots,
                    transport=transport,
                    instance_id=instance_id,
                )
            )
        else:
            if marker.exists():
                raise SigningKeyError(
                    f"Cruxible init retry marker has no complete key pair: {marker}"
                )
            prepared.append(None)

    materials: list[GeneratedKeyMaterial] = []
    markers: list[Path] = []
    for target, existing in zip(targets, prepared, strict=True):
        material = existing
        marker = init_resume_marker(target)
        if material is None:
            material = generate_client_principal_key(
                target.directory,
                principal_id=target.principal.principal_id,
                kind=target.principal.kind,
                forbidden_roots=forbidden_roots,
            )
            _writeinit_resume_marker(
                marker,
                _init_resume_payload(
                    target,
                    transport=transport,
                    instance_id=instance_id,
                    public_key=material.principal.public_key,
                ),
            )
        materials.append(material)
        markers.append(marker)
    return tuple(materials), tuple(markers)


__all__ = ["init_resume_marker", "prepare_init_custody"]
