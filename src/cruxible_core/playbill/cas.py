"""Inert content-addressed body storage with explicit read authorization."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from cruxible_core.playbill.canonical import CasDigest
from cruxible_core.playbill.errors import PlaybillCasError


class _StrictCasModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BodyAccessContext(_StrictCasModel):
    """Policy seam for protected body and body-derived metadata access."""

    tag: Literal["playbill-body-access-v1"] = "playbill-body-access-v1"
    principal_id: str
    can_read_body: bool = False


class CasObjectMetadata(_StrictCasModel):
    digest: str
    present: bool
    byte_length: int | None
    redacted: bool


class BodyProjectionProtocol(Protocol):
    """Compiler-only metadata seam; public reads still require explicit access."""

    def verify(self, digest: str) -> bool: ...

    def metadata(
        self,
        digest: str,
        *,
        access: BodyAccessContext,
    ) -> CasObjectMetadata: ...


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class ContentAddressedBodyStore:
    """Managed SHA-256 body store; storing bytes grants no canonical authority."""

    def __init__(self, root: Path) -> None:
        if root.is_symlink() or not root.is_dir():
            raise PlaybillCasError("CAS root must be an existing regular directory")
        self.root = root.resolve(strict=True)
        algorithm = self.root / "sha256"
        algorithm.mkdir(mode=0o700, exist_ok=True)
        if algorithm.is_symlink() or not algorithm.is_dir():
            raise PlaybillCasError("CAS algorithm directory is not trustworthy")
        os.chmod(algorithm, 0o700)
        self._algorithm_root = algorithm.resolve(strict=True)

    @staticmethod
    def digest_bytes(content: bytes) -> CasDigest:
        return CasDigest(hashlib.sha256(content).hexdigest())

    def _path(self, digest: str) -> Path:
        value = CasDigest.from_tagged(digest)
        directory = self._algorithm_root / value.value[:2]
        return directory / value.value

    def store(self, content: bytes) -> CasObjectMetadata:
        """Durably store inert bytes, idempotently, under their exact digest."""

        digest = self.digest_bytes(content)
        path = self._path(digest.tagged)
        directory = path.parent
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise PlaybillCasError("CAS shard directory is not trustworthy")
        os.chmod(directory, 0o700)
        if path.exists() or path.is_symlink():
            self._verified_bytes(path, digest.tagged)
            return CasObjectMetadata(
                digest=digest.tagged,
                present=True,
                byte_length=len(content),
                redacted=False,
            )
        descriptor: int | None = None
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:  # pragma: no cover - defensive OS contract
                    raise PlaybillCasError("CAS write made no progress")
                view = view[written:]
            os.fsync(descriptor)
        except FileExistsError:
            self._verified_bytes(path, digest.tagged)
        except OSError as exc:
            raise PlaybillCasError("CAS body could not be stored durably") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        os.chmod(path, 0o600)
        _fsync_directory(directory)
        return CasObjectMetadata(
            digest=digest.tagged,
            present=True,
            byte_length=len(content),
            redacted=False,
        )

    def _verified_bytes(self, path: Path, digest: str) -> bytes:
        if path.is_symlink() or not path.is_file():
            raise PlaybillCasError("CAS object must be a regular file")
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode):
            raise PlaybillCasError("CAS object must be a regular file")
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise PlaybillCasError("CAS object cannot be read") from exc
        if self.digest_bytes(content).tagged != digest:
            raise PlaybillCasError("CAS object bytes do not match their content address")
        return content

    def verify(self, digest: str) -> bool:
        """Verify exact bytes without disclosing them or their length."""

        path = self._path(digest)
        if not path.exists() and not path.is_symlink():
            return False
        self._verified_bytes(path, digest)
        return True

    def read(self, digest: str, *, access: BodyAccessContext) -> bytes:
        if not access.can_read_body:
            raise PlaybillCasError("body access is denied")
        path = self._path(digest)
        if not path.exists() and not path.is_symlink():
            raise PlaybillCasError("CAS object is missing")
        return self._verified_bytes(path, digest)

    def metadata(self, digest: str, *, access: BodyAccessContext) -> CasObjectMetadata:
        path = self._path(digest)
        present = path.exists() or path.is_symlink()
        if not present:
            return CasObjectMetadata(
                digest=digest,
                present=False,
                byte_length=None,
                redacted=not access.can_read_body,
            )
        content = self._verified_bytes(path, digest)
        return CasObjectMetadata(
            digest=digest,
            present=True,
            byte_length=len(content) if access.can_read_body else None,
            redacted=not access.can_read_body,
        )


__all__ = [
    "BodyAccessContext",
    "BodyProjectionProtocol",
    "CasObjectMetadata",
    "ContentAddressedBodyStore",
]
