"""Inert content-addressed body storage with explicit read authorization."""

from __future__ import annotations

import os
import stat
import threading
import weakref
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

# Objects already hashed against their address, with the file identity observed
# when they were: (device, inode, size, mtime, ctime). Bytes are reused without
# re-hashing only when one open descriptor shows that same identity before and
# after the read, so the file read is the file hashed and was not written since.
_VERIFIED_CAPACITY = 65536
_VERIFIED: OrderedDict[tuple[str, str], tuple[int, int, int, int, int]] = OrderedDict()
_VERIFIED_LOCK = threading.Lock()


def _after_fork_in_child() -> None:
    # A lock held by another thread at fork time is never released in the child.
    global _VERIFIED_LOCK
    _VERIFIED_LOCK = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _read_descriptor(
    name: str, *, dir_fd: int
) -> tuple[os.stat_result, bytes, os.stat_result] | None:
    """Read one regular file through a single descriptor, with its identity around the read."""

    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except OSError:
        return None
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            return None
        chunks = []
        while chunk := os.read(descriptor, 1 << 20):
            chunks.append(chunk)
        after = os.fstat(descriptor)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    return before, b"".join(chunks), after


class ContentAddressedBodyStore:
    """Managed SHA-256 body store; storing bytes grants no canonical authority.

    The algorithm directory is validated once and then held open. Every shard
    and object is reached relative to that descriptor, never refusing to follow
    a symlink, so no later change to the directory's ancestors -- a directory
    moved away and replaced by a symlink -- can redirect a read or a write: the
    store keeps addressing exactly the directory it validated.
    """

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
        descriptor = os.open(self._algorithm_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        opened, named = os.fstat(descriptor), self._algorithm_root.lstat()
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            os.close(descriptor)
            raise PlaybillCasError("CAS algorithm directory changed while it was opened")
        self._root_fd = descriptor
        weakref.finalize(self, os.close, descriptor)

    @staticmethod
    def digest_bytes(content: bytes) -> CasDigest:
        return digest_bytes(content)

    @staticmethod
    def _names(digest: str) -> tuple[str, str]:
        value = CasDigest.from_tagged(digest).value
        return value[:2], value

    def _path(self, digest: str) -> Path:
        """Where an object is named on disk, for diagnostics; access never uses it."""

        shard, name = self._names(digest)
        return self._algorithm_root / shard / name

    def _shard(self, shard: str, *, create: bool = False) -> int | None:
        """Open one shard directory relative to the held algorithm directory."""

        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            return os.open(shard, flags, dir_fd=self._root_fd)
        except FileNotFoundError:
            if not create:
                return None
        except OSError as exc:
            raise PlaybillCasError("CAS shard directory is not trustworthy") from exc
        try:
            os.mkdir(shard, 0o700, dir_fd=self._root_fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise PlaybillCasError("CAS shard directory could not be created") from exc
        try:
            descriptor = os.open(shard, flags, dir_fd=self._root_fd)
        except OSError as exc:
            raise PlaybillCasError("CAS shard directory is not trustworthy") from exc
        os.fsync(self._root_fd)
        return descriptor

    def _object_status(self, digest: str) -> os.stat_result | None:
        shard, name = self._names(digest)
        descriptor = self._shard(shard)
        if descriptor is None:
            return None
        try:
            return os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return None
        finally:
            os.close(descriptor)

    def file_identity(self, digest: str) -> tuple[int, int, int, int, int] | None:
        """The stored object's file identity, or None when it is absent."""

        status = self._object_status(digest)
        return None if status is None else _file_identity(status)

    def store(self, content: bytes) -> CasObjectMetadata:
        """Durably store inert bytes, idempotently, under their exact digest."""

        digest = self.digest_bytes(content)
        shard, name = self._names(digest.tagged)
        directory = self._shard(shard, create=True)
        assert directory is not None
        try:
            os.fchmod(directory, 0o700)
            try:
                existing = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                self._verified_bytes(digest.tagged)
            else:
                descriptor: int | None = None
                try:
                    descriptor = os.open(
                        name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=directory,
                    )
                    view = memoryview(content)
                    while view:
                        written = os.write(descriptor, view)
                        if written <= 0:  # pragma: no cover - defensive OS contract
                            raise PlaybillCasError("CAS write made no progress")
                        view = view[written:]
                    os.fchmod(descriptor, 0o600)
                    os.fsync(descriptor)
                except FileExistsError:
                    self._verified_bytes(digest.tagged)
                except OSError as exc:
                    raise PlaybillCasError("CAS body could not be stored durably") from exc
                finally:
                    if descriptor is not None:
                        os.close(descriptor)
                os.fsync(directory)
        finally:
            os.close(directory)
        return CasObjectMetadata(
            digest=digest.tagged,
            present=True,
            byte_length=len(content),
            redacted=False,
        )

    def _memo_key(self, digest: str) -> tuple[str, str]:
        return (str(self._path(digest)), digest)

    def _known(self, digest: str) -> bool:
        """Whether this exact file was already verified and has not changed since."""

        status = self._object_status(digest)
        if status is None or not stat.S_ISREG(status.st_mode):
            return False
        with _VERIFIED_LOCK:
            known = _VERIFIED.get(self._memo_key(digest))
        return known == _file_identity(status)

    def _verified_bytes(self, digest: str) -> bytes:
        # Bytes are only ever returned from one open descriptor whose identity
        # is checked before and after the read, so what is returned is exactly
        # the file that was hashed (or is hashed here), unwritten in between.
        shard, name = self._names(digest)
        directory = self._shard(shard)
        if directory is None:
            raise PlaybillCasError("CAS object is missing")
        try:
            key = self._memo_key(digest)
            with _VERIFIED_LOCK:
                known = _VERIFIED.get(key)
            read = _read_descriptor(name, dir_fd=directory)
            if read is None:
                try:
                    status = os.stat(name, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError as exc:
                    raise PlaybillCasError("CAS object is missing") from exc
                if not stat.S_ISREG(status.st_mode):
                    raise PlaybillCasError("CAS object must be a regular file")
                raise PlaybillCasError("CAS object cannot be read")
            before, content, after = read
        finally:
            os.close(directory)
        if known is not None and _file_identity(before) == known == _file_identity(after):
            return content
        if self.digest_bytes(content).tagged != digest:
            raise PlaybillCasError("CAS object bytes do not match their content address")
        if _file_identity(before) == _file_identity(after):
            with _VERIFIED_LOCK:
                _VERIFIED[key] = _file_identity(after)
                _VERIFIED.move_to_end(key)
                while len(_VERIFIED) > _VERIFIED_CAPACITY:
                    _VERIFIED.popitem(last=False)
        return content

    def verify(self, digest: str) -> bool:
        """Verify exact bytes without disclosing them or their length."""

        if self._known(digest):
            return True
        if self._object_status(digest) is None:
            return False
        self._verified_bytes(digest)
        return True

    def read(self, digest: str, *, access: BodyAccessContext) -> bytes:
        if not access.can_read_body:
            raise PlaybillCasError("body access is denied")
        if self._object_status(digest) is None:
            raise PlaybillCasError("CAS object is missing")
        return self._verified_bytes(digest)

    def metadata(self, digest: str, *, access: BodyAccessContext) -> CasObjectMetadata:
        if self._object_status(digest) is None:
            return CasObjectMetadata(
                digest=digest,
                present=False,
                byte_length=None,
                redacted=not access.can_read_body,
            )
        content = self._verified_bytes(digest)
        return CasObjectMetadata(
            digest=digest,
            present=True,
            byte_length=len(content) if access.can_read_body else None,
            redacted=not access.can_read_body,
        )

    def erase(self, digest: str) -> bool:
        """Delete one exact verified body; semantic envelopes must be preserved elsewhere."""

        if self._object_status(digest) is None:
            return False
        self._verified_bytes(digest)
        shard, name = self._names(digest)
        directory = self._shard(shard)
        if directory is None:
            return False
        try:
            os.unlink(name, dir_fd=directory)
            os.fsync(directory)
        except OSError as exc:
            raise PlaybillCasError("CAS body could not be erased") from exc
        finally:
            os.close(directory)
        return True


__all__ = [
    "BodyAccessContext",
    "BodyProjectionProtocol",
    "CasObjectMetadata",
    "ContentAddressedBodyStore",
]
