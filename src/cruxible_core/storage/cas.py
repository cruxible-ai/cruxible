"""Inert content-addressed body storage with explicit read authorization."""

from __future__ import annotations

import os
import stat
import threading
from collections import OrderedDict
from pathlib import Path

from cruxible_client.contracts.canonical import CasDigest
from cruxible_client.contracts.cas_contracts import (
    BodyAccessContext,
    BodyProjectionProtocol,
    CasObjectMetadata,
    digest_bytes,
)
from cruxible_client.contracts.errors import PlaybillCasError


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


# Objects already hashed against their address, with the file identity observed
# when they were: (device, inode, size, mtime, ctime). A file whose identity is
# unchanged has not been rewritten, so its bytes need not be re-hashed.
_VERIFIED_CAPACITY = 65536
_VERIFIED: OrderedDict[tuple[str, str], tuple[int, int, int, int, int]] = OrderedDict()
_VERIFIED_LOCK = threading.Lock()
_VALID_SHARDS: dict[tuple[str, str], tuple[int, int, int]] = {}


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


class ContentAddressedBodyStore:
    """Managed SHA-256 body store; storing bytes grants no canonical authority."""

    def __init__(self, root: Path, *, reservation_root: Path | None = None) -> None:
        if root.is_symlink() or not root.is_dir():
            raise PlaybillCasError("CAS root must be an existing regular directory")
        self.root = root.resolve(strict=True)
        self.reservation_root = (
            root.parent / "leases" / "procedure-material"
            if reservation_root is None
            else reservation_root
        )
        algorithm = self.root / "sha256"
        algorithm.mkdir(mode=0o700, exist_ok=True)
        if algorithm.is_symlink() or not algorithm.is_dir():
            raise PlaybillCasError("CAS algorithm directory is not trustworthy")
        os.chmod(algorithm, 0o700)
        self._algorithm_root = algorithm.resolve(strict=True)

    @staticmethod
    def digest_bytes(content: bytes) -> CasDigest:
        return digest_bytes(content)

    def _path(self, digest: str) -> Path:
        value = CasDigest.from_tagged(digest)
        directory = self._algorithm_root / value.value[:2]
        if directory.exists() or directory.is_symlink():
            self._validate_shard(directory)
        return directory / value.value

    def _validate_shard(self, directory: Path) -> None:
        # A shard validated before is re-resolved only if its lstat identity
        # (device, inode, mode) changed: a swapped-in symlink or directory differs.
        try:
            identity = directory.lstat()
        except OSError:
            identity = None
        key = (str(self._algorithm_root), directory.name)
        signature = (
            None if identity is None else (identity.st_dev, identity.st_ino, identity.st_mode)
        )
        with _VERIFIED_LOCK:
            known = _VALID_SHARDS.get(key)
        if identity is not None and known == signature and stat.S_ISDIR(identity.st_mode):
            return
        if directory.is_symlink() or not directory.is_dir():
            raise PlaybillCasError("CAS shard directory is not trustworthy")
        try:
            resolved = directory.resolve(strict=True)
        except OSError as exc:
            raise PlaybillCasError("CAS shard directory cannot be resolved") from exc
        if resolved.parent != self._algorithm_root or resolved.name != directory.name:
            raise PlaybillCasError("CAS shard directory escapes the managed CAS root")
        if signature is not None:
            with _VERIFIED_LOCK:
                _VALID_SHARDS[key] = signature

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

    def _known(self, path: Path, digest: str) -> bool:
        """Whether this exact file was already verified and has not changed since."""

        try:
            metadata = path.lstat()
        except OSError:
            return False
        if not stat.S_ISREG(metadata.st_mode):
            return False
        with _VERIFIED_LOCK:
            known = _VERIFIED.get((str(path), digest))
        return known == _file_identity(metadata)

    def _verified_bytes(self, path: Path, digest: str) -> bytes:
        if self._known(path, digest):
            try:
                return path.read_bytes()
            except OSError as exc:
                raise PlaybillCasError("CAS object cannot be read") from exc
        self._validate_shard(path.parent)
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
        after = path.lstat()
        if _file_identity(after) == _file_identity(metadata):
            with _VERIFIED_LOCK:
                _VERIFIED[(str(path), digest)] = _file_identity(after)
                _VERIFIED.move_to_end((str(path), digest))
                while len(_VERIFIED) > _VERIFIED_CAPACITY:
                    _VERIFIED.popitem(last=False)
        return content

    def verify(self, digest: str) -> bool:
        """Verify exact bytes without disclosing them or their length."""

        path = self._path(digest)
        if self._known(path, digest):
            return True
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

    def erase(self, digest: str) -> bool:
        """Delete one exact verified body; semantic envelopes must be preserved elsewhere."""

        path = self._path(digest)
        if not path.exists() and not path.is_symlink():
            return False
        self._verified_bytes(path, digest)
        try:
            path.unlink()
        except OSError as exc:
            raise PlaybillCasError("CAS body could not be erased") from exc
        _fsync_directory(path.parent)
        return True


__all__ = [
    "BodyAccessContext",
    "BodyProjectionProtocol",
    "CasObjectMetadata",
    "ContentAddressedBodyStore",
]
