"""Inert content-addressed body storage with explicit read authorization."""

from __future__ import annotations

import hashlib
import heapq
import os
import re
import stat
import threading
import weakref
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

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


# -- body observation -------------------------------------------------------------

FileIdentity = tuple[int, int, int, int, int]


@dataclass
class BodyObservation:
    """The file identity of every CAS object read or checked inside one ``observe_bodies``.

    A derived answer kept beyond the fold that computed it (the stored ``next``
    queue) is valid only while every object the fold consulted keeps the identity
    it had, so the observation is taken here, at the one layer every body read,
    existence check and identity reuse passes through, rather than by each
    caller. ``None`` records an absent object. An object seen with two identities
    moved during the fold (``consistent`` is False); an answer that rests on
    something no identity stands for, such as an external reader, clears
    ``complete``.
    """

    identities: dict[str, FileIdentity | None]
    consistent: bool = True
    complete: bool = True

    def note(self, digest: str, identity: FileIdentity | None) -> None:
        if self.identities.setdefault(digest, identity) != identity:
            self.consistent = False

    def fingerprints(self) -> tuple[tuple[str, FileIdentity | None], ...]:
        return tuple(sorted(self.identities.items()))


_BODY_OBSERVATION: ContextVar[BodyObservation | None] = ContextVar(
    "cruxible_cas_body_observation", default=None
)


@contextmanager
def observe_bodies() -> Iterator[BodyObservation]:
    """Record every CAS object identity this context consults; outside one, nothing is kept."""

    observation = BodyObservation(identities={})
    token = _BODY_OBSERVATION.set(observation)
    try:
        yield observation
    finally:
        _BODY_OBSERVATION.reset(token)


# Every hook starts with one context lookup and does nothing else outside an
# observation: no identity is built and no extra stat is taken for a caller that
# is not observing.


def _observe(digest: str, status: os.stat_result | None) -> None:
    """Record one object as stat showed it; ``None`` is a confirmed absence."""

    observation = _BODY_OBSERVATION.get()
    if observation is not None:
        observation.note(digest, None if status is None else _file_identity(status))


def _observe_read(digest: str, before: os.stat_result, after: os.stat_result) -> None:
    """Record one read bracketed by two stats of its descriptor."""

    observation = _BODY_OBSERVATION.get()
    if observation is None:
        return
    if _file_identity(before) == _file_identity(after):
        observation.note(digest, _file_identity(after))
    else:
        # Written while it was read: no one identity stands for these bytes.
        observation.consistent = False


def _observe_descriptor_read(digest: str, before: os.stat_result, descriptor: int) -> None:
    """Record a read whose closing stat is taken only when someone is observing."""

    if _BODY_OBSERVATION.get() is not None:
        _observe_read(digest, before, os.fstat(descriptor))


def note_unobservable_body() -> None:
    """Mark the current observation incomplete: an answer rests on a read no identity covers."""

    observation = _BODY_OBSERVATION.get()
    if observation is not None:
        observation.complete = False


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


_HEX_PREFIX = re.compile(r"[0-9a-f]{0,64}")
_SHARD_NAME = re.compile(r"[0-9a-f]{2}")
_OBJECT_NAME = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class CasScan:
    """One bounded scan of stored digests: what matched, and whether it finished."""

    digests: tuple[str, ...]
    complete: bool
    examined: int
    nearest: tuple[str, ...] = ()


def _shared_length(name: str, prefix: str) -> int:
    length = 0
    while length < min(len(name), len(prefix)) and name[length] == prefix[length]:
        length += 1
    return length


def _ranked(closest: list[tuple[int, str]]) -> tuple[str, ...]:
    return tuple(
        digest for _shared, digest in sorted(closest, key=lambda item: (-item[0], item[1]))
    )


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
            _observe(digest, None)
            return None
        try:
            status = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            status = None
        finally:
            os.close(descriptor)
        _observe(digest, status)
        return status

    def peek(self, digest: str, length: int) -> bytes:
        """The first ``length`` bytes of one object, UNVERIFIED: for classifying only.

        Nothing read here is trusted; a caller that keeps an object reads it
        again through ``read``, which verifies every byte against the address.
        """

        shard, name = self._names(digest)
        directory = self._shard(shard)
        if directory is None:
            _observe(digest, None)
            return b""
        try:
            try:
                descriptor = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                )
            except FileNotFoundError:
                _observe(digest, None)
                return b""
            except OSError:
                note_unobservable_body()
                return b""
        finally:
            os.close(directory)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                _observe(digest, before)
                return b""
            head = os.read(descriptor, length)
            _observe_descriptor_read(digest, before, descriptor)
            return head
        except OSError:
            note_unobservable_body()
            return b""
        finally:
            os.close(descriptor)

    def scan(self, hex_prefix: str = "", *, budget: int, nearest: int = 0) -> CasScan:
        """Stored digests starting with ``hex_prefix``, examining at most ``budget`` names.

        Objects are sharded by their first two hex digits, so a prefix of two or
        more names one shard and a shorter one the shards it opens. Entries are
        streamed, never listed whole: the scan stops after ``budget`` entries and
        says so (``complete`` is False), so a caller never mistakes a partial
        scan for the whole store. ``nearest`` keeps that many of the examined
        digests sharing the longest prefix with ``hex_prefix``, in the same pass.
        """

        if not _HEX_PREFIX.fullmatch(hex_prefix):
            raise PlaybillCasError("a digest prefix is lowercase hex")
        found: list[str] = []
        closest: list[tuple[int, str]] = []
        examined = 0
        shards = (
            [hex_prefix[:2]]
            if len(hex_prefix) >= 2
            else sorted(
                name
                for name in os.listdir(self._root_fd)
                if _SHARD_NAME.fullmatch(name) and name.startswith(hex_prefix)
            )
        )
        for shard in shards:
            descriptor = self._shard(shard)
            if descriptor is None:
                continue
            try:
                with os.scandir(descriptor) as entries:
                    for entry in entries:
                        if examined >= budget:
                            return CasScan(
                                digests=tuple(sorted(found)),
                                complete=False,
                                examined=examined,
                                nearest=_ranked(closest),
                            )
                        examined += 1
                        name = entry.name
                        if not _OBJECT_NAME.fullmatch(name) or not name.startswith(shard):
                            continue
                        if name.startswith(hex_prefix):
                            found.append("sha256:" + name)
                        elif nearest:
                            shared = _shared_length(name, hex_prefix)
                            item = (shared, "sha256:" + name)
                            if len(closest) < nearest:
                                heapq.heappush(closest, item)
                            elif item > closest[0]:
                                heapq.heapreplace(closest, item)
            finally:
                os.close(descriptor)
        return CasScan(
            digests=tuple(sorted(found)),
            complete=True,
            examined=examined,
            nearest=_ranked(closest),
        )

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
            _observe(digest, None)
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
                    _observe(digest, None)
                    raise PlaybillCasError("CAS object is missing") from exc
                _observe(digest, status)
                if not stat.S_ISREG(status.st_mode):
                    raise PlaybillCasError("CAS object must be a regular file")
                raise PlaybillCasError("CAS object cannot be read")
            before, content, after = read
        finally:
            os.close(directory)
        # Observed before the address is checked: an answer that a corrupt
        # object refused rests on that object just as much as one it served.
        _observe_read(digest, before, after)
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

    def availability(self, digest: str) -> Literal["present", "missing", "corrupt"]:
        """Re-hash one stored object from disk, streaming, trusting no earlier check.

        `verify` reuses an earlier hash while the file's identity is unchanged;
        bytes that rot in place keep that identity, so a sweep looking for rot
        must hash again. Nothing is held beyond one read buffer.
        """

        shard, name = self._names(digest)
        directory = self._shard(shard)
        if directory is None:
            _observe(digest, None)
            return "missing"
        try:
            try:
                # Non-blocking, so a FIFO planted in the store is refused below
                # instead of waiting forever for a writer.
                descriptor = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                )
            except FileNotFoundError:
                _observe(digest, None)
                return "missing"
            except OSError:
                note_unobservable_body()
                return "corrupt"
        finally:
            os.close(directory)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                _observe(digest, before)
                return "corrupt"
            hasher = hashlib.sha256()
            while chunk := os.read(descriptor, 1 << 20):
                hasher.update(chunk)
            _observe_descriptor_read(digest, before, descriptor)
        except OSError:
            note_unobservable_body()
            return "corrupt"
        finally:
            os.close(descriptor)
        return "present" if CasDigest(hasher.hexdigest()).tagged == digest else "corrupt"

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


# -- dry runs ---------------------------------------------------------------------

_DRY_RUN_BODIES: ContextVar[dict[str, bytes] | None] = ContextVar(
    "cruxible_cas_dry_run_bodies", default=None
)


@contextmanager
def dry_run_bodies() -> Iterator[dict[str, bytes]]:
    """Hold every body stored in this context in memory, so a dry run writes nothing.

    A dry run takes the same path as the write it previews up to the commit, and
    that path stores bodies as it lowers (a self-source capture, exact content).
    Inside this context the instance's body store is a ``DryRunBodyStore``: the
    bodies it stores are held here and read back from here, and nothing reaches
    the store on disk. The context is per call (a context variable), so a
    concurrent write in another request is unaffected.
    """

    held: dict[str, bytes] = {}
    token = _DRY_RUN_BODIES.set(held)
    try:
        yield held
    finally:
        _DRY_RUN_BODIES.reset(token)


def dry_run_held_bodies() -> dict[str, bytes] | None:
    """The bodies the current dry run holds, or None outside a dry run."""

    return _DRY_RUN_BODIES.get()


class DryRunBodyStore:
    """A body store that reads through to ``base`` and holds new bodies in memory."""

    def __init__(self, base: ContentAddressedBodyStore, held: dict[str, bytes]) -> None:
        self._base = base
        self._held = held

    digest_bytes = staticmethod(ContentAddressedBodyStore.digest_bytes)

    def store(self, content: bytes) -> CasObjectMetadata:
        digest = self.digest_bytes(content).tagged
        if not self._base.verify(digest):
            self._held[digest] = bytes(content)
        return CasObjectMetadata(
            digest=digest, present=True, byte_length=len(content), redacted=False
        )

    def verify(self, digest: str) -> bool:
        return digest in self._held or self._base.verify(digest)

    def availability(self, digest: str) -> Literal["present", "missing", "corrupt"]:
        return "present" if digest in self._held else self._base.availability(digest)

    def read(self, digest: str, *, access: BodyAccessContext) -> bytes:
        held = self._held.get(digest)
        if held is None:
            return self._base.read(digest, access=access)
        if not access.can_read_body:
            raise PlaybillCasError("body access is denied")
        return held

    def metadata(self, digest: str, *, access: BodyAccessContext) -> CasObjectMetadata:
        held = self._held.get(digest)
        if held is None:
            return self._base.metadata(digest, access=access)
        return CasObjectMetadata(
            digest=digest,
            present=True,
            byte_length=len(held) if access.can_read_body else None,
            redacted=not access.can_read_body,
        )

    def peek(self, digest: str, length: int) -> bytes:
        held = self._held.get(digest)
        return self._base.peek(digest, length) if held is None else held[:length]

    def scan(self, hex_prefix: str = "", *, budget: int, nearest: int = 0) -> CasScan:
        base = self._base.scan(hex_prefix, budget=budget, nearest=nearest)
        held = [
            digest for digest in self._held if digest.removeprefix("sha256:").startswith(hex_prefix)
        ]
        return CasScan(
            digests=tuple(sorted({*base.digests, *held})),
            complete=base.complete,
            examined=base.examined,
            nearest=base.nearest,
        )

    def erase(self, digest: str) -> bool:
        raise PlaybillCasError("a dry run erases nothing")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)


__all__ = [
    "BodyAccessContext",
    "BodyObservation",
    "CasScan",
    "BodyProjectionProtocol",
    "CasObjectMetadata",
    "ContentAddressedBodyStore",
    "DryRunBodyStore",
    "dry_run_bodies",
    "dry_run_held_bodies",
    "note_unobservable_body",
    "observe_bodies",
]
