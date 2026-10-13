"""The sequenced log of body-store landings: the change record for CAS availability.

Every body that lands in the store (bar journal-record payloads, reachable only
through the record that names them) and every erase is one *operation* with
two rows under increasing sequences: an intent (``store``/``erase``) before the
object moves and a completion (``stored``/``erased``) once it has. A row says
"this digest may have changed", never what it now holds: readers consult the
store.

An operation is recoverable. Before its intent row the writer creates and
locks a pending marker named by the digest; it publishes the completion and
removes the marker after the object is final. A writer that fails or dies in
between leaves the marker unlocked, and reconciliation -- on a retry of the
same digest, on opening the store, and before every follower's pass --
publishes a completion for whatever the object then is (present: ``stored``;
absent: ``erased``) and removes the marker. A follower that has read an intent
without its completion keeps its cursor before that intent, so a current cursor
never implies an unfinished digest was reconciled.

The log, its marker directory and every read and write go through the store's
held algorithm-directory descriptor, never a pathname: moving an ancestor away
can redirect neither. The log is a flat file of fixed-width records under a
header naming a random identity minted with it, so a cursor kept against one
log is never read against a replacement; a torn tail record is dropped by the
next writer and ignored by readers.
"""

from __future__ import annotations

import fcntl
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from cruxible_client.contracts.errors import CasError
from cruxible_client.contracts.primitives import new_id

if TYPE_CHECKING:
    from cruxible_core.storage.cas import ContentAddressedBodyStore

LandingKind = Literal["store", "stored", "erase", "erased"]
LOG_NAME = "landings.log"
PENDING_NAME = "landings.pending"
RECORD = 96
_HEADER = b"cruxible-landings-v1 "
_CODES: dict[LandingKind, bytes] = {"store": b"s", "stored": b"S", "erase": b"e", "erased": b"E"}
_KINDS: dict[int, LandingKind] = {code[0]: kind for kind, code in _CODES.items()}
_COMPLETION: dict[str, LandingKind] = {"store": "stored", "erase": "erased"}
_FILE = os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


@dataclass(frozen=True)
class LandingHead:
    """Where the log stands: its identity and last sequence (``""`` and 0 before any row)."""

    identity: str
    sequence: int


@dataclass(frozen=True)
class Landing:
    sequence: int
    digest: str
    kind: LandingKind

    @property
    def intent(self) -> bool:
        return self.kind in {"store", "erase"}


def _record(kind: LandingKind, digest: str) -> bytes:
    body = _CODES[kind] + b" " + digest.encode("ascii")
    if len(body) > RECORD - 1:
        raise CasError("a landing digest does not fit its record")
    return body.ljust(RECORD - 1, b" ") + b"\n"


def _regular(descriptor: int, *, what: str) -> os.stat_result:
    status = os.fstat(descriptor)
    if not stat.S_ISREG(status.st_mode):
        raise CasError(f"the CAS landing {what} must be a regular file")
    return status


class LandingLog:
    """The landing log in one body store's held algorithm directory."""

    def __init__(self, store: ContentAddressedBodyStore) -> None:
        self._store = store

    @property
    def _directory(self) -> int:
        return self._store._root_fd

    # -- the file -----------------------------------------------------------

    @contextmanager
    def _opened(self, *, write: bool) -> Iterator[int | None]:
        flags = (os.O_RDWR | os.O_CREAT | os.O_APPEND) if write else os.O_RDONLY
        try:
            descriptor = os.open(LOG_NAME, flags | _FILE, 0o600, dir_fd=self._directory)
        except FileNotFoundError:
            if write:
                raise
            yield None
            return
        except OSError as exc:
            raise CasError("the CAS landing log could not be opened") from exc
        try:
            _regular(descriptor, what="log")
            fcntl.flock(descriptor, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
            yield descriptor
        finally:
            os.close(descriptor)

    def _append(self, kind: LandingKind, digest: str, *, sync: bool) -> int:
        with self._opened(write=True) as descriptor:
            assert descriptor is not None
            size = os.fstat(descriptor).st_size
            if size < RECORD:
                # No complete header: mint the log's identity.
                os.ftruncate(descriptor, 0)
                header = _HEADER + new_id("landings", length=32).encode("ascii")
                os.write(descriptor, header.ljust(RECORD - 1, b" ") + b"\n")
            elif size % RECORD:
                # A torn record a writer died inside: drop it before appending.
                os.ftruncate(descriptor, size - size % RECORD)
            written = os.write(descriptor, _record(kind, digest))
            if written != RECORD:  # pragma: no cover - a short local append
                raise CasError("a CAS landing record was not written whole")
            if sync:
                os.fsync(descriptor)
            return os.fstat(descriptor).st_size // RECORD - 1

    def head(self) -> LandingHead:
        """The log's identity and newest complete sequence: one header read and a stat."""

        with self._opened(write=False) as descriptor:
            if descriptor is None:
                return LandingHead("", 0)
            size = os.fstat(descriptor).st_size
            if size < RECORD:
                return LandingHead("", 0)
            header = os.pread(descriptor, RECORD, 0)
        if not header.startswith(_HEADER):
            raise CasError("the CAS landing log has no header")
        return LandingHead(header[len(_HEADER) :].strip().decode("ascii"), size // RECORD - 1)

    def after(self, sequence: int, *, through: int | None = None) -> tuple[Landing, ...]:
        """Every complete row after ``sequence`` (through ``through``): one ranged read."""

        with self._opened(write=False) as descriptor:
            if descriptor is None:
                return ()
            last = os.fstat(descriptor).st_size // RECORD - 1
            if through is not None:
                last = min(last, through)
            if last <= sequence:
                return ()
            start = (sequence + 1) * RECORD
            raw = os.pread(descriptor, (last - sequence) * RECORD, start)
        rows: list[Landing] = []
        for index in range(len(raw) // RECORD):
            record = raw[index * RECORD : (index + 1) * RECORD]
            kind = _KINDS.get(record[0])
            if kind is None or record[1:2] != b" " or record[-1:] != b"\n":
                raise CasError("a CAS landing record is malformed")
            rows.append(
                Landing(sequence + 1 + index, record[2 : RECORD - 1].strip().decode("ascii"), kind)
            )
        return tuple(rows)

    # -- operations -----------------------------------------------------------

    def _pending(self, *, create: bool) -> int | None:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            return os.open(PENDING_NAME, flags, dir_fd=self._directory)
        except FileNotFoundError:
            if not create:
                return None
        try:
            os.mkdir(PENDING_NAME, 0o700, dir_fd=self._directory)
        except FileExistsError:
            pass
        return os.open(PENDING_NAME, flags, dir_fd=self._directory)

    @staticmethod
    def _marker(digest: str) -> str:
        return digest.removeprefix("sha256:")

    @contextmanager
    def operation(self, digest: str, kind: Literal["store", "erase"]) -> Iterator[None]:
        """Publish an intent, run the object change, then publish its completion.

        Holds a locked pending marker throughout. When the change raises, the
        completion is published for whatever the object then is; when even that
        fails (or the process dies), the marker stays for reconciliation.
        """

        pending = self._pending(create=True)
        assert pending is not None
        try:
            while True:
                marker = os.open(
                    self._marker(digest), os.O_RDWR | os.O_CREAT | _FILE, 0o600, dir_fd=pending
                )
                fcntl.flock(marker, fcntl.LOCK_EX)
                if os.fstat(marker).st_nlink:
                    break
                # Reconciled away between create and lock: take a fresh marker.
                os.close(marker)
            try:
                self._append(kind, digest, sync=False)
                try:
                    yield
                finally:
                    self._settle(digest, pending, completion=_COMPLETION[kind])
            finally:
                os.close(marker)
        finally:
            os.close(pending)

    def _settle(self, digest: str, pending: int, *, completion: LandingKind | None) -> None:
        """Publish an operation's completion, then drop its marker (the caller holds its lock).

        The completion names what the object is now; ``None`` reads it first.
        """

        present = self._store._object_status(digest) is not None
        if completion is None or (completion == "stored") != present:
            completion = "stored" if present else "erased"
        self._append(completion, digest, sync=True)
        try:
            os.unlink(self._marker(digest), dir_fd=pending)
        except FileNotFoundError:
            pass

    def reconcile(self, digest: str | None = None) -> int:
        """Publish the completion of every operation its writer left unfinished.

        A marker another live process still holds is skipped: its writer will
        publish. Returns how many operations this call settled.
        """

        pending = self._pending(create=False)
        if pending is None:
            return 0
        settled = 0
        try:
            names = (
                [self._marker(digest)]
                if digest is not None
                else [entry.name for entry in os.scandir(pending)]
            )
            for name in names:
                try:
                    marker = os.open(name, os.O_RDWR | _FILE, dir_fd=pending)
                except FileNotFoundError:
                    continue
                try:
                    try:
                        fcntl.flock(marker, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    if not os.fstat(marker).st_nlink:
                        continue
                    self._settle("sha256:" + name, pending, completion=None)
                    settled += 1
                finally:
                    os.close(marker)
        finally:
            os.close(pending)
        return settled

    def unfinished(self) -> bool:
        """Whether any operation's marker is still pending: one directory read."""

        pending = self._pending(create=False)
        if pending is None:
            return False
        try:
            with os.scandir(pending) as entries:
                return any(True for _ in entries)
        finally:
            os.close(pending)


__all__ = ["LOG_NAME", "PENDING_NAME", "Landing", "LandingHead", "LandingKind", "LandingLog"]
