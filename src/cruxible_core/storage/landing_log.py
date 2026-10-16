"""The sequenced log of body-store landings: the change record for CAS availability.

Every body that lands in the store (bar journal-record payloads, reachable only
through the record that names them) and every erase is one *operation* with
two rows under increasing sequences: an intent (``store``/``erase``) before the
object moves and a completion (``stored``/``erased``) once it has. A row says
"this digest may have changed"; its completions say what the object then is.
A read decides a body's presence from the log alone (``present``): the digest's
last completion at or before the read's recorded position, kept per process in
an index the log extends as it grows. Files are opened only for their bytes.

A body damaged out of band -- deleted, or rewritten in place -- moves no
operation. Whoever verifies it and finds the damage (the evidence sweep, or any
read that hashes it) records a *damage* completion (``missing``/``corrupt``,
``note``) once, while the log still has the digest ``stored``; a body later
found whole again, repaired in place or stored again, gets a ``stored``
completion of its own. So a read at a position after the damage record answers
absent, one before it present, and a follower sees the damage as one more row
naming the digest.

A log minted over a store that already holds objects (a store from before the
log, or bodies first stored as journal payloads) records each of them as
``stored`` first, so the log names every object the store held when it began.
The seed is written whole under a scratch name and renamed over the log only
once it is durable: a seed interrupted part way leaves no log, and the next
open or landing seeds again. A writer locks the file the log's name holds and
re-opens when the seed has replaced it, so no row lands in a superseded file.

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
import threading
from bisect import bisect_right
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from cruxible_client.contracts.errors import CasError
from cruxible_client.contracts.primitives import new_id

if TYPE_CHECKING:
    from cruxible_core.storage.cas import ContentAddressedBodyStore

LandingKind = Literal["store", "stored", "erase", "erased", "missing", "corrupt"]
#: What a verification found an object to be.
BodyState = Literal["present", "missing", "corrupt"]
LOG_NAME = "landings.log"
SEED_NAME = "landings.seed"
PENDING_NAME = "landings.pending"
RECORD = 96
_HEADER = b"cruxible-landings-v1 "
_CODES: dict[LandingKind, bytes] = {
    "store": b"s",
    "stored": b"S",
    "erase": b"e",
    "erased": b"E",
    "missing": b"M",
    "corrupt": b"C",
}
_KINDS: dict[int, LandingKind] = {code[0]: kind for kind, code in _CODES.items()}
_COMPLETION: dict[str, LandingKind] = {"store": "stored", "erase": "erased"}
_FILE = os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_HEX = frozenset("0123456789abcdef")


#: How many times one create answered ENOENT is retried: each retry follows a
#: concurrent writer's create (or unlink) of the very name, so it soon settles.
CREATE_RETRIES = 64


def _hex(name: str, length: int) -> bool:
    return len(name) == length and set(name) <= _HEX


def _open_creating(name: str, flags: int, *, dir_fd: int) -> int:
    """``openat(dir_fd, name, flags | O_CREAT)``, retried past a concurrent create.

    macOS answers ENOENT to the loser of two concurrent ``O_CREAT`` opens of one
    new name relative to a directory descriptor (a path open is not affected):
    the name exists by then, so the open is retried. ENOENT stands only once
    the directory itself is gone.
    """

    for _attempt in range(CREATE_RETRIES):
        try:
            return os.open(name, flags | os.O_CREAT, 0o600, dir_fd=dir_fd)
        except FileNotFoundError:
            if not parent_linked(dir_fd):
                raise
    return os.open(name, flags | os.O_CREAT, 0o600, dir_fd=dir_fd)


def parent_linked(dir_fd: int) -> bool:
    """Whether a held directory is still linked: an ENOENT under it is then transient.

    A create relative to it answered ENOENT while it is linked lost a race with
    a concurrent create (or removal) of the same name, and is retried; once the
    directory itself is gone, the ENOENT stands.
    """

    return os.fstat(dir_fd).st_nlink > 0


class _Presence:
    """One log's completions per digest, read once per process and extended as it grows."""

    def __init__(self) -> None:
        self.through = 0
        #: Each digest's completions in log order: (sequence, completion kind).
        self.events: dict[str, list[tuple[int, LandingKind]]] = {}


#: Per (held algorithm directory, log identity): a replaced log is another index.
_INDEXES: dict[tuple[str, str], _Presence] = {}
_INDEXES_LOCK = threading.Lock()
#: Per held algorithm directory: the digests whose last completion this process
#: has seen (read into an index, or written) is a damage record. A verification
#: that finds such a body whole records its repair; one this process has not seen
#: damaged costs that check nothing beyond this lookup (``damage_seen``).
_DAMAGE_SEEN: dict[str, set[str]] = {}
_DAMAGE_KINDS = frozenset({"missing", "corrupt"})


def _saw(root: str, digest: str, kind: LandingKind) -> None:
    """Note one completion for ``damage_seen`` (the caller holds ``_INDEXES_LOCK``)."""

    if kind in _DAMAGE_KINDS:
        _DAMAGE_SEEN.setdefault(root, set()).add(digest)
    else:
        _DAMAGE_SEEN.get(root, set()).discard(digest)


def damage_seen(root: str, digest: str) -> bool:
    """Whether this process last saw ``digest`` recorded damaged in the store at ``root``.

    No I/O: a damage record another process wrote is seen once a read here has
    extended its index past it (``LandingLog.present``); until then the evidence
    sweep records the repair.
    """

    seen = _DAMAGE_SEEN.get(root)
    return seen is not None and digest in seen


def _reset_after_fork() -> None:
    global _INDEXES_LOCK
    _INDEXES_LOCK = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)


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
        if write:
            with self._writing() as descriptor:
                yield descriptor
            return
        try:
            descriptor = os.open(LOG_NAME, os.O_RDONLY | _FILE, dir_fd=self._directory)
        except FileNotFoundError:
            yield None
            return
        except OSError as exc:
            raise CasError("the CAS landing log could not be opened") from exc
        try:
            _regular(descriptor, what="log")
            fcntl.flock(descriptor, fcntl.LOCK_SH)
            yield descriptor
        finally:
            os.close(descriptor)

    def _locked(self) -> int:
        """Open and exclusively lock the file the log's name holds once the lock is taken.

        A seed renamed over the log while this writer waited leaves it holding
        the superseded file: it re-opens the one the name now holds.
        """

        flags = os.O_RDWR | os.O_APPEND | _FILE
        while True:
            try:
                descriptor = _open_creating(LOG_NAME, flags, dir_fd=self._directory)
            except OSError as exc:
                raise CasError("the CAS landing log could not be opened") from exc
            try:
                held = _regular(descriptor, what="log")
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                try:
                    named = os.stat(LOG_NAME, dir_fd=self._directory, follow_symlinks=False)
                except FileNotFoundError:
                    named = None
                if named is not None and (named.st_dev, named.st_ino) == (
                    held.st_dev,
                    held.st_ino,
                ):
                    return descriptor
            except BaseException:
                os.close(descriptor)
                raise
            os.close(descriptor)

    @contextmanager
    def _writing(self) -> Iterator[int]:
        """The log, exclusively locked, its seed complete: seeded first when it has none."""

        descriptor = self._locked()
        try:
            if os.fstat(descriptor).st_size < RECORD:
                self._seed()
                os.close(descriptor)
                descriptor = -1
                descriptor = self._locked()
            yield descriptor
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def _append(self, kind: LandingKind, digest: str, *, sync: bool) -> int:
        with self._writing() as descriptor:
            size = os.fstat(descriptor).st_size
            if size % RECORD:
                # A torn record a writer died inside: drop it before appending.
                os.ftruncate(descriptor, size - size % RECORD)
            written = os.write(descriptor, _record(kind, digest))
            if written != RECORD:  # pragma: no cover - a short local append
                raise CasError("a CAS landing record was not written whole")
            if sync:
                os.fsync(descriptor)
            return os.fstat(descriptor).st_size // RECORD - 1

    def _seed(self) -> None:
        """Publish a new log under the caller's exclusive lock of the one it replaces: its
        identity, then a ``stored`` row for every object the store already holds.

        Written whole and made durable under a scratch name, then renamed over the
        log: an interrupted seed publishes nothing, and the next writer seeds again.
        """

        header = _HEADER + new_id("landings", length=32).encode("ascii")
        rows = [header.ljust(RECORD - 1, b" ") + b"\n"]
        for shard in sorted(os.listdir(self._directory)):
            if not _hex(shard, 2):
                continue
            try:
                directory = os.open(
                    shard, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self._directory
                )
            except OSError:
                continue
            try:
                names = os.listdir(directory)
            finally:
                os.close(directory)
            rows.extend(
                _record("stored", "sha256:" + name) for name in sorted(names) if _hex(name, 64)
            )
        seed = os.open(
            SEED_NAME, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _FILE, 0o600, dir_fd=self._directory
        )
        try:
            _regular(seed, what="seed")
            view = memoryview(b"".join(rows))
            while view:
                written = os.write(seed, view)
                if written <= 0:  # pragma: no cover - a short local write
                    raise CasError("the CAS landing log could not be minted")
                view = view[written:]
            os.fsync(seed)
        finally:
            os.close(seed)
        os.rename(SEED_NAME, LOG_NAME, src_dir_fd=self._directory, dst_dir_fd=self._directory)
        os.fsync(self._directory)

    def ensure(self) -> None:
        """Seed the log over a store that holds objects but has none: one open when it exists.

        An empty store keeps no log until its first landing. A log whose seed was
        interrupted was never published, so this seeds it again.
        """

        with self._opened(write=False) as descriptor:
            if descriptor is not None and os.fstat(descriptor).st_size >= RECORD:
                return
        if not any(_hex(name, 2) for name in os.listdir(self._directory)):
            return
        with self._writing():
            pass

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

    def after(
        self, sequence: int, *, through: int | None = None, identity: str | None = None
    ) -> tuple[Landing, ...]:
        """Every complete row after ``sequence`` (through ``through``): one ranged read.

        ``identity`` is the log the caller's positions name: another log refuses.
        """

        with self._opened(write=False) as descriptor:
            if descriptor is None:
                if identity:
                    raise CasError("the CAS landing log was replaced")
                return ()
            if identity is not None and (
                os.pread(descriptor, RECORD, 0)[len(_HEADER) :].strip().decode("ascii") != identity
            ):
                raise CasError("the CAS landing log was replaced")
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

    # -- presence -------------------------------------------------------------

    def present(self, digest: str, *, identity: str, sequence: int) -> bool:
        """Whether ``digest`` is in the store as of ``sequence`` of the log ``identity``.

        Its last completion at or before that position decides: ``stored``
        present; ``erased``, a damage record (``missing``/``corrupt``) or none
        absent. An operation whose completion comes later has not happened yet.
        Read from the process's index of the log, extended by the rows it has not
        yet read: never from the object itself.
        """

        return self.last_completion(digest, identity=identity, sequence=sequence) == "stored"

    def last_completion(self, digest: str, *, identity: str, sequence: int) -> LandingKind | None:
        """The digest's last completion at or before ``sequence`` of the log ``identity``."""

        if not identity or sequence <= 0:
            return None
        key = (str(self._store._algorithm_root), identity)
        with _INDEXES_LOCK:
            index = _INDEXES.setdefault(key, _Presence())
        with _INDEXES_LOCK:
            if index.through < sequence:
                for row in self.after(index.through, through=sequence, identity=identity):
                    if not row.intent:
                        index.events.setdefault(row.digest, []).append((row.sequence, row.kind))
                        _saw(key[0], row.digest, row.kind)
                    index.through = row.sequence
            events = index.events.get(digest)
            if not events:
                return None
            found = bisect_right(events, sequence, key=lambda event: event[0])
            return events[found - 1][1] if found > 0 else None

    def _disagrees(self, digest: str, state: BodyState) -> bool:
        """Whether what a verification found contradicts the log's current presence.

        Only a ``stored`` digest can be damaged, and only a damaged one repaired:
        an erased body, or a journal payload the log never named, records nothing.
        """

        head = self.head()
        last = self.last_completion(digest, identity=head.identity, sequence=head.sequence)
        if state == "present":
            return last in {"missing", "corrupt"}
        return last == "stored"

    def note(self, digest: str, state: BodyState, *, recheck: Callable[[], BodyState]) -> bool:
        """Record what a verification found when the log says otherwise: one completion.

        A ``stored`` digest found missing or corrupt gets that damage record; a
        damaged one found whole again gets ``stored``. The finding is checked
        again (``recheck``) under the digest's pending marker, so it never lands
        over a store or erase completing meanwhile; while another operation on
        the digest holds the marker nothing is recorded, as that operation's
        completion says what the object then is. Returns whether a row landed.
        """

        if not self._disagrees(digest, state):
            return False
        # A marker a dead writer left is settled first, never taken over.
        self.reconcile(digest)
        pending = self._pending(create=True)
        assert pending is not None
        try:
            marker = self._hold_marker(pending, digest, wait=False)
            if marker is None:
                return False
            try:
                state = recheck()
                if not self._disagrees(digest, state):
                    return False
                kind: LandingKind = "stored" if state == "present" else state
                self._append(kind, digest, sync=True)
                with _INDEXES_LOCK:
                    _saw(str(self._store._algorithm_root), digest, kind)
                return True
            finally:
                try:
                    os.unlink(self._marker(digest), dir_fd=pending)
                except FileNotFoundError:
                    pass
                os.close(marker)
        finally:
            os.close(pending)

    def ensure_landed(self, digest: str, *, sync: bool = True) -> None:
        """Log an object the store holds but the log does not name as present.

        A body first stored as a journal payload lands unlogged; stored again as
        evidence, it is logged now, so a read deciding presence from the log sees it.
        """

        head = self.head()
        if self.present(digest, identity=head.identity, sequence=head.sequence):
            return
        with self.operation(digest, "store", sync=sync):
            pass

    # -- operations -----------------------------------------------------------

    def _pending(self, *, create: bool) -> int | None:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            return os.open(PENDING_NAME, flags, dir_fd=self._directory)
        except FileNotFoundError:
            if not create:
                return None
        attempt = 0
        while True:
            attempt += 1
            # ENOENT while the held directory is linked: a concurrent create of
            # the name (or its removal) won the race; take the name again.
            try:
                os.mkdir(PENDING_NAME, 0o700, dir_fd=self._directory)
            except FileExistsError:
                pass
            except FileNotFoundError:
                if attempt > CREATE_RETRIES or not parent_linked(self._directory):
                    raise
                continue
            try:
                return os.open(PENDING_NAME, flags, dir_fd=self._directory)
            except FileNotFoundError:
                if attempt > CREATE_RETRIES or not parent_linked(self._directory):
                    raise

    @staticmethod
    def _marker(digest: str) -> str:
        return digest.removeprefix("sha256:")

    @contextmanager
    def operation(
        self, digest: str, kind: Literal["store", "erase"], *, sync: bool = True
    ) -> Iterator[None]:
        """Publish an intent, run the object change, then publish its completion.

        Holds a locked pending marker throughout. When the change raises, the
        completion is published for whatever the object then is; when even that
        fails (or the process dies), the marker stays for reconciliation.
        """

        pending = self._pending(create=True)
        assert pending is not None
        try:
            marker = self._hold_marker(pending, digest, wait=True)
            assert marker is not None
            try:
                self._append(kind, digest, sync=False)
                try:
                    yield
                finally:
                    self._settle(digest, pending, completion=_COMPLETION[kind], sync=sync)
            finally:
                os.close(marker)
        finally:
            os.close(pending)

    def _hold_marker(self, pending: int, digest: str, *, wait: bool) -> int | None:
        """Create and exclusively lock the digest's pending marker (None: held elsewhere)."""

        while True:
            marker = _open_creating(self._marker(digest), os.O_RDWR | _FILE, dir_fd=pending)
            try:
                fcntl.flock(marker, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                os.close(marker)
                return None
            if os.fstat(marker).st_nlink:
                return marker
            # Reconciled away between create and lock: take a fresh marker.
            os.close(marker)

    def _settle(
        self, digest: str, pending: int, *, completion: LandingKind | None, sync: bool = True
    ) -> None:
        """Publish an operation's completion, then drop its marker (the caller holds its lock).

        The completion names what the object is now; ``None`` reads it first.
        """

        present = self._store._object_status(digest) is not None
        if completion is None or (completion == "stored") != present:
            completion = "stored" if present else "erased"
        self._append(completion, digest, sync=sync)
        with _INDEXES_LOCK:
            _saw(str(self._store._algorithm_root), digest, completion)
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


__all__ = [
    "CREATE_RETRIES",
    "LOG_NAME",
    "PENDING_NAME",
    "SEED_NAME",
    "BodyState",
    "damage_seen",
    "Landing",
    "LandingHead",
    "LandingKind",
    "LandingLog",
    "parent_linked",
]
