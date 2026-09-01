"""Ed25519 generation and custody helpers for Playbill bootstrap."""

from __future__ import annotations

import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from cruxible_client.contracts.errors import PlaybillKeyError
from cruxible_client.contracts.types import PrincipalKind, PrincipalRecord

DAEMON_PRIVATE_KEY_FILE = "daemon_ed25519"
DAEMON_PUBLIC_KEY_FILE = "daemon_ed25519.pub"
ALLOWED_SIGNERS_FILE = "allowed_signers"


@dataclass(frozen=True)
class GeneratedKeyMaterial:
    """A newly generated key; private path is intentionally not serializable."""

    principal: PrincipalRecord
    private_key_path: Path
    public_key_path: Path


@dataclass(frozen=True)
class ClientPrincipalKeyTarget:
    """Validated client-custody paths without creating or adopting key bytes."""

    directory: Path
    principal: PrincipalRecord
    private_key_path: Path
    public_key_path: Path


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _is_within(candidate: Path, root: Path) -> bool:
    return candidate == root or root in candidate.parents


def assert_outside_roots(path: Path, forbidden_roots: Sequence[Path]) -> None:
    """Refuse client key custody inside a workspace or managed instance."""

    resolved = _resolved(path)
    for raw_root in forbidden_roots:
        root = _resolved(raw_root)
        if _is_within(resolved, root):
            raise PlaybillKeyError(
                "client approval/recovery keys must remain outside workspaces and "
                "managed Playbill instance storage"
            )


def _secure_directory(path: Path) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise PlaybillKeyError(f"key directory is not a real directory: {path}")
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise PlaybillKeyError(
                f"key directory permissions must exclude group/world access: {path}"
            )
        return
    path.mkdir(parents=True, mode=0o700)
    os.chmod(path, 0o700)


def _exclusive_write(path: Path, content: bytes, mode: int) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.chmod(path, mode)


def _serialize_private(private_key: Ed25519PrivateKey) -> bytes:
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _serialize_public(private_key: Ed25519PrivateKey, principal_id: str) -> bytes:
    key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    )
    return key + f" playbill:{principal_id}\n".encode("utf-8")


def _public_key_hex(private_key: Ed25519PrivateKey) -> str:
    return private_key.public_key().public_bytes_raw().hex()


def _generate_key_material(
    directory: Path,
    *,
    principal_id: str,
    kind: PrincipalKind,
    private_filename: str,
    public_filename: str,
) -> GeneratedKeyMaterial:
    _secure_directory(directory)
    private_path = directory / private_filename
    public_path = directory / public_filename
    if private_path.exists() or public_path.exists():
        raise PlaybillKeyError(f"refusing to overwrite existing key material for {principal_id}")
    private_key = Ed25519PrivateKey.generate()
    try:
        _exclusive_write(private_path, _serialize_private(private_key), 0o600)
        _exclusive_write(public_path, _serialize_public(private_key, principal_id), 0o644)
    except BaseException:
        if public_path.exists():
            public_path.unlink()
        if private_path.exists():
            private_path.unlink()
        raise
    return GeneratedKeyMaterial(
        principal=PrincipalRecord(
            principal_id=principal_id,
            public_key=_public_key_hex(private_key),
            kind=kind,
        ),
        private_key_path=private_path,
        public_key_path=public_path,
    )


def generate_daemon_key(credentials_directory: Path) -> GeneratedKeyMaterial:
    """Generate the one instance-specific Git signing key in managed custody."""

    material = _generate_key_material(
        credentials_directory,
        principal_id="daemon",
        kind="daemon",
        private_filename=DAEMON_PRIVATE_KEY_FILE,
        public_filename=DAEMON_PUBLIC_KEY_FILE,
    )
    public_fields = material.public_key_path.read_bytes().split()
    if len(public_fields) < 2:
        raise PlaybillKeyError("generated daemon public key is malformed")
    _exclusive_write(
        credentials_directory / ALLOWED_SIGNERS_FILE,
        b"daemon " + b" ".join(public_fields[:2]) + b"\n",
        0o600,
    )
    return material


def generate_client_principal_key(
    key_directory: Path,
    *,
    principal_id: str,
    kind: PrincipalKind,
    forbidden_roots: Sequence[Path],
) -> GeneratedKeyMaterial:
    """Generate a client-held approval/recovery key outside daemon storage."""

    if principal_id == "daemon" or kind == "daemon":
        raise PlaybillKeyError("client keys cannot claim daemon identity or kind")
    assert_outside_roots(key_directory, forbidden_roots)
    # Validate identifier and sorted roles before using either in a filename.
    placeholder = PrincipalRecord(
        principal_id=principal_id,
        public_key="0" * 64,
        kind=kind,
    )
    return _generate_key_material(
        key_directory,
        principal_id=placeholder.principal_id,
        kind=placeholder.kind,
        private_filename=f"{placeholder.principal_id}.ed25519",
        public_filename=f"{placeholder.principal_id}.ed25519.pub",
    )


def validate_client_principal_key_target(
    key_directory: Path,
    *,
    principal_id: str,
    kind: PrincipalKind,
    forbidden_roots: Sequence[Path],
) -> ClientPrincipalKeyTarget:
    """Validate one custody target without creating directories or key material."""

    if principal_id == "daemon" or kind == "daemon":
        raise PlaybillKeyError("client keys cannot claim daemon identity or kind")
    directory = _resolved(key_directory)
    assert_outside_roots(directory, forbidden_roots)
    principal = PrincipalRecord(
        principal_id=principal_id,
        public_key="0" * 64,
        kind=kind,
    )
    if directory.exists():
        if directory.is_symlink() or not directory.is_dir():
            raise PlaybillKeyError(f"key directory is not a real directory: {directory}")
        if stat.S_IMODE(directory.stat().st_mode) & 0o077:
            raise PlaybillKeyError(
                f"key directory permissions must exclude group/world access: {directory}"
            )
    return ClientPrincipalKeyTarget(
        directory=directory,
        principal=principal,
        private_key_path=directory / f"{principal.principal_id}.ed25519",
        public_key_path=directory / f"{principal.principal_id}.ed25519.pub",
    )


def adopt_client_principal_key(
    key_directory: Path,
    *,
    principal_id: str,
    kind: PrincipalKind,
    forbidden_roots: Sequence[Path],
) -> GeneratedKeyMaterial:
    """Load one exact complete client key pair after an external retry receipt check."""

    target = validate_client_principal_key_target(
        key_directory,
        principal_id=principal_id,
        kind=kind,
        forbidden_roots=forbidden_roots,
    )
    for path in (target.private_key_path, target.public_key_path):
        if path.is_symlink() or not path.is_file():
            raise PlaybillKeyError(
                f"retry adoption requires a complete regular key pair for {principal_id}"
            )
    if stat.S_IMODE(target.private_key_path.stat().st_mode) & 0o077:
        raise PlaybillKeyError(
            f"private key permissions must exclude group/world access: {target.private_key_path}"
        )
    private_public = public_key_hex_from_private_file(target.private_key_path)
    try:
        public_content = target.public_key_path.read_bytes()
    except OSError as exc:
        raise PlaybillKeyError("client public key is missing or unreadable") from exc
    public_hex = raw_public_key_hex_from_openssh(public_content)
    if private_public != public_hex:
        raise PlaybillKeyError("client public/private key pair does not correspond")
    return GeneratedKeyMaterial(
        principal=target.principal.model_copy(update={"public_key": public_hex}),
        private_key_path=target.private_key_path,
        public_key_path=target.public_key_path,
    )


def public_key_hex_from_private_file(path: Path) -> str:
    """Load an unencrypted OpenSSH Ed25519 key and return its raw public bytes."""

    try:
        private_key = serialization.load_ssh_private_key(path.read_bytes(), password=None)
    except (OSError, ValueError) as exc:
        raise PlaybillKeyError("daemon private key is missing or unreadable") from exc
    if not isinstance(private_key, Ed25519PrivateKey):
        raise PlaybillKeyError("Playbill requires an Ed25519 daemon private key")
    return _public_key_hex(private_key)


def raw_public_key_hex_from_openssh(content: bytes) -> str:
    """Parse one OpenSSH Ed25519 public key without accepting another algorithm."""

    fields = content.split()
    if len(fields) < 2:
        raise PlaybillKeyError("OpenSSH public key is malformed")
    try:
        public_key = serialization.load_ssh_public_key(b" ".join(fields[:2]))
    except ValueError as exc:
        raise PlaybillKeyError("OpenSSH public key is malformed") from exc
    if not isinstance(public_key, Ed25519PublicKey):
        raise PlaybillKeyError("Playbill requires an Ed25519 public key")
    return public_key.public_bytes_raw().hex()


__all__ = [
    "ALLOWED_SIGNERS_FILE",
    "DAEMON_PRIVATE_KEY_FILE",
    "DAEMON_PUBLIC_KEY_FILE",
    "GeneratedKeyMaterial",
    "ClientPrincipalKeyTarget",
    "adopt_client_principal_key",
    "assert_outside_roots",
    "generate_client_principal_key",
    "generate_daemon_key",
    "public_key_hex_from_private_file",
    "raw_public_key_hex_from_openssh",
    "validate_client_principal_key_target",
]
