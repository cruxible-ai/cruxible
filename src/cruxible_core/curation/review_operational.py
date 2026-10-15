"""Append-only operational observations outside Cruxible governed state.

This store records what one daemon served or mechanically observed.  Its files
are not ledger members and must never participate in semantic, generation, or
candidate identity.

Readers keep a per-process replay of every partition they verified, and
``partitions.log`` names each partition a writer touched, written under the
store lock before the partition changes. A reader reads only the log lines
added since it last looked and re-reads only those partitions, from the first
sequence it has not verified; ``head`` is rebuilt only when a partition moved.
A partition is verified once per process; a log that is replaced, truncated or
missing makes the next reader list the whole store again.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import stat
import threading
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_client.contracts.errors import CruxibleError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.temporal import ensure_utc
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.storage.preview_fence import is_previewing, refuse_write_while_previewing

REVIEW_OPERATIONAL_EVENT_DIGEST_DOMAIN = "playbill-review-operational-event-v1"
REVIEW_OPERATIONAL_PARTITION_GENESIS_DOMAIN = "playbill-review-operational-partition-genesis-v1"
REVIEW_OPERATIONAL_HEAD_DIGEST_DOMAIN = "playbill-review-operational-head-v1"
REVIEW_OPERATIONAL_PARTITION_PATH_DOMAIN = "playbill-review-operational-partition-path-v1"
REVIEW_OPERATIONAL_PAYLOAD_DIGEST_DOMAIN = "playbill-review-operational-payload-v1"
REVIEW_OPERATIONAL_STORE_DIRECTORY = "review-operational-v1"

ReviewOperationalFamily: TypeAlias = Literal[
    "curation", "audit", "consumption", "block_observation", "block_scan"
]
REVIEW_OPERATIONAL_APPEND_BATCH_LIMIT = 256
_UNCHECKED_PARTITION_HEAD = object()
_FAMILIES: tuple[ReviewOperationalFamily, ...] = (
    "audit",
    "block_observation",
    "block_scan",
    "consumption",
    "curation",
)
# Each line names one partition a writer touched: "<family>\t<directory name>".
_TOUCH_LOG = "partitions.log"
# Past this size, a reader that has read the whole log starts a new one.
_TOUCH_LOG_COMPACT_BYTES = 1 << 20
# In-memory touches kept for incremental readers before they rebuild once.
_TOUCH_FEED_LIMIT = 1 << 16


class ReviewOperationalStoreError(CruxibleError):
    """Operational state is corrupt, unsafe, or concurrently changed."""

    code = "cruxible.curation.operational_store_invalid"

    @property
    def error_code(self) -> str:
        return self.code

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


class ReviewOperationalConcurrentChangeError(CruxibleError):
    """The caller's expected operational partition head is no longer current."""

    code = "cruxible.curation.concurrent_change"

    @property
    def error_code(self) -> str:
        return self.code

    def __init__(self) -> None:
        super().__init__(f"{self.code}: operational partition changed concurrently")


class _StrictOperationalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReviewOperationalStoreManifestV1(_StrictOperationalModel):
    tag: Literal["playbill-review-operational-store-v1"] = "playbill-review-operational-store-v1"
    instance_id: str = Field(min_length=1, max_length=256)
    initialized_coordinate: AcceptedCoordinate
    initialized_generation: int = Field(ge=0)
    initialized_at: datetime

    @field_validator("initialized_at")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        return ensure_utc(value)


class PlaybillReviewOperationalEventV1(_StrictOperationalModel):
    tag: Literal["playbill-review-operational-event-v1"] = "playbill-review-operational-event-v1"
    instance_id: str = Field(min_length=1, max_length=256)
    family: ReviewOperationalFamily
    partition_id: str = Field(min_length=1, max_length=512)
    sequence: int = Field(ge=0)
    previous_event_digest: str
    accepted_coordinate: AcceptedCoordinate
    accepted_generation: int = Field(ge=0)
    actor_context: GovernedActorContext
    payload_digest: str
    recorded_at: datetime
    event_digest: str

    @field_validator("previous_event_digest", "payload_digest", "event_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("recorded_at")
    @classmethod
    def _time(cls, value: datetime) -> datetime:
        return ensure_utc(value)

    @model_validator(mode="after")
    def _reproduces(self) -> PlaybillReviewOperationalEventV1:
        if self.event_digest != review_operational_event_digest(self):
            raise ValueError("review operational event digest does not reproduce")
        return self


class ReviewOperationalPartitionHeadV1(_StrictOperationalModel):
    family: ReviewOperationalFamily
    partition_id: str
    sequence: int = Field(ge=0)
    event_digest: str

    @field_validator("event_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value


class ReviewOperationalHeadV1(_StrictOperationalModel):
    tag: Literal["playbill-review-operational-head-v1"] = "playbill-review-operational-head-v1"
    initialized: bool
    initialized_coordinate: AcceptedCoordinate | None
    initialized_generation: int | None = Field(default=None, ge=0)
    partitions: tuple[ReviewOperationalPartitionHeadV1, ...]
    head_digest: str

    @field_validator("head_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _shape(self) -> ReviewOperationalHeadV1:
        if self.initialized != (self.initialized_coordinate is not None):
            raise ValueError("operational head initialization fields disagree")
        if self.initialized != (self.initialized_generation is not None):
            raise ValueError("operational head generation fields disagree")
        expected = tuple(
            sorted(
                self.partitions,
                key=lambda item: (item.family.encode("ascii"), item.partition_id.encode("utf-8")),
            )
        )
        if self.partitions != expected:
            raise ValueError("operational partition heads are not canonical")
        if self.head_digest != review_operational_head_digest(self):
            raise ValueError("review operational head digest does not reproduce")
        return self


def review_operational_event_digest(event: PlaybillReviewOperationalEventV1) -> str:
    payload = event.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("event_digest")
    return typed_digest(Sha256Value, REVIEW_OPERATIONAL_EVENT_DIGEST_DOMAIN, payload).tagged


def review_operational_partition_genesis_digest(
    *, instance_id: str, family: ReviewOperationalFamily, partition_id: str
) -> str:
    return typed_digest(
        Sha256Value,
        REVIEW_OPERATIONAL_PARTITION_GENESIS_DOMAIN,
        {"instance_id": instance_id, "family": family, "partition_id": partition_id},
    ).tagged


def review_operational_head_digest(head: ReviewOperationalHeadV1) -> str:
    payload = head.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("head_digest")
    return typed_digest(Sha256Value, REVIEW_OPERATIONAL_HEAD_DIGEST_DOMAIN, payload).tagged


def build_review_operational_head(
    *,
    initialized_coordinate: AcceptedCoordinate | None,
    initialized_generation: int | None,
    partitions: tuple[ReviewOperationalPartitionHeadV1, ...],
) -> ReviewOperationalHeadV1:
    ordered = tuple(
        sorted(
            partitions,
            key=lambda item: (item.family.encode("ascii"), item.partition_id.encode("utf-8")),
        )
    )
    placeholder = "sha256:" + "0" * 64
    draft = ReviewOperationalHeadV1.model_construct(
        tag="playbill-review-operational-head-v1",
        initialized=initialized_coordinate is not None,
        initialized_coordinate=initialized_coordinate,
        initialized_generation=initialized_generation,
        partitions=ordered,
        head_digest=placeholder,
    )
    return ReviewOperationalHeadV1(
        initialized=draft.initialized,
        initialized_coordinate=initialized_coordinate,
        initialized_generation=initialized_generation,
        partitions=ordered,
        head_digest=review_operational_head_digest(draft),
    )


def _render(value: BaseModel) -> bytes:
    return canonical_bytes(value.model_dump(mode="json")) + b"\n"


def _payload_digest(payload: dict[str, object]) -> str:
    preimage = dict(payload)
    preimage.pop("tag", None)
    return typed_digest(
        Sha256Value,
        REVIEW_OPERATIONAL_PAYLOAD_DIGEST_DOMAIN,
        preimage,
    ).tagged


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _exclusive_write(path: Path, content: bytes) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:  # pragma: no cover - defensive OS contract
                raise ReviewOperationalStoreError("operational write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    except FileExistsError as exc:
        raise ReviewOperationalStoreError("operational event path is occupied") from exc
    except OSError as exc:
        raise ReviewOperationalStoreError("operational event could not be persisted") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    _fsync_directory(path.parent)


_Loaded: TypeAlias = tuple[tuple["PlaybillReviewOperationalEventV1", dict[str, object]], ...]


@dataclass
class _PartitionReplay:
    """One partition's verified events, and whether it moved since they were read."""

    partition_id: str | None = None
    events: list[tuple[PlaybillReviewOperationalEventV1, dict[str, object]]] = field(
        default_factory=list
    )
    event_ids: dict[object, int] = field(default_factory=dict)
    dirty: bool = True
    # How many of ``events`` the store's generation index holds, and the
    # accepted generations they fall in (to drop them if the partition is
    # read again whole).
    indexed: int = 0
    generations: set[int] = field(default_factory=set)

    def extend(
        self, loaded: Iterable[tuple[PlaybillReviewOperationalEventV1, dict[str, object]]]
    ) -> None:
        for event, payload in loaded:
            self.event_ids.setdefault(payload.get("event_id"), len(self.events))
            self.events.append((event, payload))


@dataclass
class _StoreReplay:
    """What one process verified of one store, and how far it read the touch log."""

    listed: bool = False
    log_identity: tuple[int, int] | None = None
    log_offset: int = 0
    manifest: ReviewOperationalStoreManifestV1 | None = None
    # (family, directory name) -> its replay.
    partitions: dict[tuple[str, str], _PartitionReplay] = field(default_factory=dict)
    head: ReviewOperationalHeadV1 | None = None
    family_events: dict[str, _Loaded] = field(default_factory=dict)
    # A change feed for incremental readers: every touch since the replay was
    # last listed whole (``epoch``), in order.
    epoch: int = 0
    touches: list[tuple[str, str]] = field(default_factory=list)
    # family -> accepted generation -> partition directory -> event positions,
    # maintained from each partition's newly verified events.
    generation_index: dict[str, dict[int, dict[str, list[int]]]] = field(default_factory=dict)
    # family -> partition directories touched but not yet read again.
    unread: dict[str, set[str]] = field(default_factory=dict)

    def touched(self, family: str, name: str) -> None:
        self.partitions.setdefault((family, name), _PartitionReplay()).dirty = True
        self.unread.setdefault(family, set()).add(name)
        self.head = None
        self.family_events.pop(family, None)
        self.touches.append((family, name))
        if len(self.touches) > _TOUCH_FEED_LIMIT:
            # Readers past this many touches behind rebuild once.
            self.epoch += 1
            self.touches.clear()


@dataclass(frozen=True)
class FamilyChanges:
    """What one family gained since a reader's cursor.

    ``full``: the reader's cursor is unknown or stale, and ``partitions`` is
    the whole family. Otherwise ``partitions`` names each partition touched
    since the cursor. Each entry is (partition directory, its verified events
    in sequence order, or None if the partition is gone); the event list is
    the replay's own and must not be modified.
    """

    cursor: tuple[int, int]
    full: bool
    partitions: tuple[
        tuple[str, list[tuple[PlaybillReviewOperationalEventV1, dict[str, object]]] | None], ...
    ]


_REPLAYS: dict[tuple[str, str], _StoreReplay] = {}
_REPLAYS_LOCK = threading.Lock()


def _replay(root: Path, instance_id: str) -> _StoreReplay:
    with _REPLAYS_LOCK:
        return _REPLAYS.setdefault((str(root), instance_id), _StoreReplay())


def reset_review_operational_replays() -> None:
    """Forget every in-process replay, so the next read verifies the whole store again."""

    with _REPLAYS_LOCK:
        _REPLAYS.clear()


class ReviewOperationalStore:
    """Canonical immutable operational events with replay-derived heads."""

    def __init__(
        self,
        exhaust_root: Path,
        *,
        instance_id: str,
        crash_hook: Callable[[str], None] | None = None,
    ) -> None:
        if exhaust_root.is_symlink() or not exhaust_root.is_dir():
            raise ReviewOperationalStoreError("operational exhaust root is not trustworthy")
        self.exhaust_root = exhaust_root.resolve(strict=True)
        self.root = self.exhaust_root / REVIEW_OPERATIONAL_STORE_DIRECTORY
        self._creating_root = self.exhaust_root / f".creating-{REVIEW_OPERATIONAL_STORE_DIRECTORY}"
        self.instance_id = instance_id
        self._lock_path = self.exhaust_root / ".review-operational-v1.lock"
        self._crash_hook = crash_hook

    @contextmanager
    def _locked(self):  # type: ignore[no-untyped-def]
        if self._lock_path.is_symlink():
            raise ReviewOperationalStoreError("operational lock path is not trustworthy")
        if is_previewing() and not self._lock_path.exists():
            # A preview reads; it never creates the lock file. With no lock
            # file there has never been a writer, so there is nothing to race.
            yield
            return
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._lock_path, flags, 0o600)
        except OSError as exc:
            raise ReviewOperationalStoreError("operational lock could not be opened") from exc
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise ReviewOperationalStoreError("operational lock path is not a regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _crash(self, boundary: str) -> None:
        if self._crash_hook is not None:
            self._crash_hook(boundary)

    def _ensure_initialized(
        self,
        *,
        coordinate: AcceptedCoordinate,
        generation: int,
        initialized_at: datetime,
    ) -> ReviewOperationalStoreManifestV1:
        if self.root.exists() or self.root.is_symlink():
            return self._load_manifest()
        if self._creating_root.exists() or self._creating_root.is_symlink():
            manifest = self._load_manifest_at(self._creating_root)
            os.replace(self._creating_root, self.root)
            _fsync_directory(self.exhaust_root)
            return manifest
        self._creating_root.mkdir(mode=0o700)
        os.chmod(self._creating_root, 0o700)
        (self._creating_root / "partitions").mkdir(mode=0o700)
        manifest = ReviewOperationalStoreManifestV1(
            instance_id=self.instance_id,
            initialized_coordinate=coordinate,
            initialized_generation=generation,
            initialized_at=initialized_at,
        )
        _exclusive_write(self._creating_root / "store.json", _render(manifest))
        _fsync_directory(self._creating_root)
        self._crash("after_store_manifest_sync")
        os.replace(self._creating_root, self.root)
        _fsync_directory(self.exhaust_root)
        return manifest

    def _load_manifest(self) -> ReviewOperationalStoreManifestV1:
        return self._load_manifest_at(self.root)

    def _load_manifest_at(self, root: Path) -> ReviewOperationalStoreManifestV1:
        if root.is_symlink() or not root.is_dir():
            raise ReviewOperationalStoreError("operational root is not trustworthy")
        path = root / "store.json"
        if path.is_symlink() or not path.is_file():
            raise ReviewOperationalStoreError("operational store manifest is missing")
        try:
            raw = path.read_bytes()
            manifest = ReviewOperationalStoreManifestV1.model_validate_json(raw)
        except (OSError, ValidationError, ValueError) as exc:
            raise ReviewOperationalStoreError("operational store manifest is malformed") from exc
        if raw != _render(manifest) or manifest.instance_id != self.instance_id:
            raise ReviewOperationalStoreError("operational store manifest does not reproduce")
        partitions = root / "partitions"
        if partitions.is_symlink() or not partitions.is_dir():
            raise ReviewOperationalStoreError("operational partitions root is invalid")
        return manifest

    def _partition_directory(
        self, family: ReviewOperationalFamily, partition_id: str, *, create: bool
    ) -> Path:
        digest = typed_digest(
            Sha256Value,
            REVIEW_OPERATIONAL_PARTITION_PATH_DOMAIN,
            {"family": family, "partition_id": partition_id},
        ).tagged.removeprefix("sha256:")
        directory = self.root / "partitions" / family / digest
        family_root = directory.parent
        if family_root.is_symlink() or (family_root.exists() and not family_root.is_dir()):
            raise ReviewOperationalStoreError("operational family root is invalid")
        if create:
            try:
                family_root.mkdir(mode=0o700, exist_ok=True)
            except OSError as exc:
                raise ReviewOperationalStoreError("operational family root is invalid") from exc
            if family_root.is_symlink() or not family_root.is_dir():
                raise ReviewOperationalStoreError("operational family root is invalid")
            try:
                directory.mkdir(mode=0o700, exist_ok=True)
            except OSError as exc:
                raise ReviewOperationalStoreError("operational partition root is invalid") from exc
            if directory.is_symlink() or not directory.is_dir():
                raise ReviewOperationalStoreError("operational partition root is invalid")
            for child in (directory / "events", directory / "payloads"):
                try:
                    child.mkdir(mode=0o700, exist_ok=True)
                except OSError as exc:
                    raise ReviewOperationalStoreError(
                        "operational partition child is invalid"
                    ) from exc
                if child.is_symlink() or not child.is_dir():
                    raise ReviewOperationalStoreError("operational partition child is invalid")
        return directory

    def _load_partition(
        self, family: ReviewOperationalFamily, partition_id: str
    ) -> tuple[tuple[PlaybillReviewOperationalEventV1, dict[str, object]], ...]:
        directory = self._partition_directory(family, partition_id, create=False)
        events_directory = directory / "events"
        payloads_directory = directory / "payloads"
        if (
            directory.is_symlink()
            or events_directory.is_symlink()
            or not events_directory.is_dir()
            or payloads_directory.is_symlink()
            or not payloads_directory.is_dir()
        ):
            raise ReviewOperationalStoreError("operational partition is invalid")
        paths = tuple(sorted(events_directory.glob("*.json"), key=lambda item: item.name))
        if not paths:
            raise ReviewOperationalStoreError("operational partition is empty")
        previous = review_operational_partition_genesis_digest(
            instance_id=self.instance_id, family=family, partition_id=partition_id
        )
        loaded: list[tuple[PlaybillReviewOperationalEventV1, dict[str, object]]] = []
        for sequence, path in enumerate(paths):
            event, payload = self._load_event(directory, family, partition_id, sequence, previous)
            if path.name != f"{sequence:020d}.json":
                raise ReviewOperationalStoreError("operational event sequence is not contiguous")
            loaded.append((event, payload))
            previous = event.event_digest
        return tuple(loaded)

    def _load_event(
        self,
        directory: Path,
        family: ReviewOperationalFamily,
        partition_id: str,
        sequence: int,
        previous: str,
    ) -> tuple[PlaybillReviewOperationalEventV1, dict[str, object]]:
        for child in (directory, directory / "events", directory / "payloads"):
            if child.is_symlink() or not child.is_dir():
                raise ReviewOperationalStoreError("operational partition is invalid")
        path = directory / "events" / f"{sequence:020d}.json"
        payloads_directory = directory / "payloads"
        if path.is_symlink() or not path.is_file():
            raise ReviewOperationalStoreError("operational event sequence is not contiguous")
        try:
            raw = path.read_bytes()
            event = PlaybillReviewOperationalEventV1.model_validate_json(raw)
        except (OSError, ValidationError, ValueError) as exc:
            raise ReviewOperationalStoreError("operational event is malformed") from exc
        if raw != _render(event):
            raise ReviewOperationalStoreError("operational event is not canonical")
        if (
            event.instance_id != self.instance_id
            or event.family != family
            or event.partition_id != partition_id
            or event.sequence != sequence
            or event.previous_event_digest != previous
        ):
            raise ReviewOperationalStoreError("operational event chain is broken")
        payload_path = payloads_directory / f"{event.payload_digest[7:]}.json"
        if payload_path.is_symlink() or not payload_path.is_file():
            raise ReviewOperationalStoreError("operational event payload is missing")
        try:
            payload_raw = payload_path.read_bytes()
            payload = json.loads(payload_raw)
        except (OSError, ValueError) as exc:
            raise ReviewOperationalStoreError("operational event payload is malformed") from exc
        if not isinstance(payload, dict) or payload_raw != canonical_bytes(payload) + b"\n":
            raise ReviewOperationalStoreError("operational event payload is not canonical")
        if _payload_digest(payload) != event.payload_digest:
            raise ReviewOperationalStoreError("operational event payload digest does not reproduce")
        return event, payload

    # -- the per-process replay -------------------------------------------------

    def _touch(self, family: ReviewOperationalFamily, directory: Path) -> None:
        """Name a partition in the touch log before it changes; the caller holds the lock."""

        path = self.root / _TOUCH_LOG
        if path.is_symlink():
            raise ReviewOperationalStoreError("operational touch log is not trustworthy")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            os.write(descriptor, f"{family}\t{directory.name}\n".encode("ascii"))
        finally:
            os.close(descriptor)

    def _list_partitions(self, replay: _StoreReplay) -> None:
        replay.epoch += 1
        replay.touches.clear()
        replay.partitions.clear()
        replay.family_events.clear()
        replay.generation_index.clear()
        replay.unread.clear()
        replay.head = None
        replay.manifest = None
        partitions = self.root / "partitions"
        if not partitions.is_dir():
            return
        for family in _FAMILIES:
            family_root = partitions / family
            if not family_root.exists() and not family_root.is_symlink():
                continue
            if family_root.is_symlink() or not family_root.is_dir():
                raise ReviewOperationalStoreError("operational family root is invalid")
            for name in os.listdir(family_root):
                if not name.startswith("."):
                    replay.partitions[(family, name)] = _PartitionReplay()
                    replay.unread.setdefault(family, set()).add(name)

    def _sync(self, *, compact: bool = False) -> _StoreReplay:
        """This process's replay, moved forward by the touch log; the caller holds the lock.

        ``compact`` (writers only) starts a new log once this one is large and
        fully applied here.
        """

        replay = _replay(self.root, self.instance_id)
        path = self.root / _TOUCH_LOG
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            metadata = None
        if metadata is not None and not stat.S_ISREG(metadata.st_mode):
            raise ReviewOperationalStoreError("operational touch log is not trustworthy")
        identity = None if metadata is None else (metadata.st_dev, metadata.st_ino)
        size = 0 if metadata is None else metadata.st_size
        if not replay.listed or identity != replay.log_identity or size < replay.log_offset:
            # A new process, or a log replaced or cut short: list the store again.
            self._list_partitions(replay)
            replay.listed = True
        elif size > replay.log_offset:
            with path.open("rb") as stream:
                stream.seek(replay.log_offset)
                added = stream.read(size - replay.log_offset)
            # Only whole lines: a writer holds the lock, so none is partial.
            size = replay.log_offset + added.rfind(b"\n") + 1
            for line in added[: size - replay.log_offset].splitlines():
                family, _separator, name = line.decode("ascii").partition("\t")
                if family in _FAMILIES and name:
                    replay.touched(family, name)
        replay.log_identity = identity
        replay.log_offset = size
        if compact and size > _TOUCH_LOG_COMPACT_BYTES and not is_previewing():
            # Everything named so far is applied here; a fresh log makes every
            # other process list the store once.
            staged = self.root / f".{_TOUCH_LOG}.new"
            staged.unlink(missing_ok=True)
            os.close(os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            os.replace(staged, path)
            fresh = path.lstat()
            replay.log_identity = (fresh.st_dev, fresh.st_ino)
            replay.log_offset = 0
        return replay

    def _manifest(self, replay: _StoreReplay) -> ReviewOperationalStoreManifestV1:
        if replay.manifest is None:
            replay.manifest = self._load_manifest()
        return replay.manifest

    def _replayed(
        self, replay: _StoreReplay, family: ReviewOperationalFamily, name: str
    ) -> _PartitionReplay | None:
        """One partition's verified events, reading only the sequences not yet verified.

        The generation index moves with it: only the newly verified events are
        added, and a partition read again whole (or gone) first drops its own.
        """

        before = replay.partitions.get((family, name))
        if before is not None and not before.dirty:
            return before
        held = self._replay_partition(replay, family, name)
        index = replay.generation_index.setdefault(family, {})
        if before is not None and before is not held:
            for generation in before.generations:
                slot = index.get(generation)
                if slot is not None:
                    slot.pop(name, None)
                    if not slot:
                        del index[generation]
        if held is not None:
            start = held.indexed if held is before else 0
            for position in range(start, len(held.events)):
                generation = held.events[position][0].accepted_generation
                index.setdefault(generation, {}).setdefault(name, []).append(position)
                held.generations.add(generation)
            held.indexed = len(held.events)
        replay.unread.get(family, set()).discard(name)
        return held

    def _replay_partition(
        self, replay: _StoreReplay, family: ReviewOperationalFamily, name: str
    ) -> _PartitionReplay | None:
        held = replay.partitions.get((family, name))
        directory = self.root / "partitions" / family / name
        if not directory.exists() and not directory.is_symlink():
            replay.partitions.pop((family, name), None)
            return None
        if directory.is_symlink() or not directory.is_dir():
            raise ReviewOperationalStoreError("operational partition root is invalid")
        events_directory = directory / "events"
        if (
            events_directory.is_symlink()
            or not events_directory.is_dir()
            or (directory / "payloads").is_symlink()
            or not (directory / "payloads").is_dir()
        ):
            raise ReviewOperationalStoreError("operational partition is invalid")
        held = held or _PartitionReplay()
        if held.partition_id is not None and held.events and self._extends(directory, family, held):
            # Read only the sequences after the last verified one, probing by name.
            previous = held.events[-1][0].event_digest
            sequence = len(held.events)
            while (events_directory / f"{sequence:020d}.json").exists():
                event, payload = self._load_event(
                    directory, family, held.partition_id, sequence, previous
                )
                held.extend(((event, payload),))
                previous = event.event_digest
                sequence += 1
        else:
            # Unread, emptied or cut back: verify the whole chain.
            held = _PartitionReplay()
            first = events_directory / f"{0:020d}.json"
            if first.exists() or first.is_symlink():
                try:
                    partition_id = PlaybillReviewOperationalEventV1.model_validate_json(
                        first.read_bytes()
                    ).partition_id
                except (OSError, ValidationError, ValueError) as exc:
                    raise ReviewOperationalStoreError("operational event is malformed") from exc
                held.partition_id = partition_id
                held.extend(self._load_partition(family, partition_id))
        held.dirty = False
        replay.partitions[(family, name)] = held
        return held

    def _extends(
        self, directory: Path, family: ReviewOperationalFamily, held: _PartitionReplay
    ) -> bool:
        """Whether the partition still holds the last event this replay verified.

        A partition is append-only except when retention cuts it back; the
        last verified event, re-read and matched by digest, proves the prefix
        is the one already verified.
        """

        sequence = len(held.events) - 1
        last = held.events[-1][0]
        previous = (
            held.events[-2][0].event_digest
            if sequence
            else review_operational_partition_genesis_digest(
                instance_id=self.instance_id, family=family, partition_id=last.partition_id
            )
        )
        try:
            event, _payload = self._load_event(
                directory, family, last.partition_id, sequence, previous
            )
        except ReviewOperationalStoreError:
            return False
        return event.event_digest == last.event_digest

    def family_changes(
        self, family: ReviewOperationalFamily, cursor: tuple[int, int] | None
    ) -> FamilyChanges:
        """The partitions of ``family`` touched since ``cursor`` (or all of it).

        The incremental read for summary readers: the work is the touches since
        the cursor and the new events of the partitions they name.
        """

        with self._locked():
            if not self.root.exists() and not self.root.is_symlink():
                return FamilyChanges((-1, 0), True, ())
            replay = self._sync()
            self._manifest(replay)
            position = (replay.epoch, len(replay.touches))
            if cursor is None or cursor[0] != replay.epoch or cursor[1] > len(replay.touches):
                names = sorted(name for held, name in replay.partitions if held == family)
                full = True
            else:
                names = list(
                    dict.fromkeys(
                        name for held, name in replay.touches[cursor[1] :] if held == family
                    )
                )
                full = False
            found: list[
                tuple[str, list[tuple[PlaybillReviewOperationalEventV1, dict[str, object]]] | None]
            ] = []
            for name in names:
                held_replay = self._replayed(replay, family, name)
                if held_replay is not None and not held_replay.events:
                    raise ReviewOperationalStoreError("operational partition is empty")
                found.append((name, None if held_replay is None else held_replay.events))
            return FamilyChanges(position, full, tuple(found))

    def _family_events(self, replay: _StoreReplay, family: ReviewOperationalFamily) -> _Loaded:
        known = replay.family_events.get(family)
        if known is not None:
            return known
        loaded: list[tuple[PlaybillReviewOperationalEventV1, dict[str, object]]] = []
        for key in sorted(name for held_family, name in replay.partitions if held_family == family):
            held = self._replayed(replay, family, key)
            if held is None:
                continue
            if not held.events:
                raise ReviewOperationalStoreError("operational partition is empty")
            loaded.extend(held.events)
        replay.family_events[family] = tuple(loaded)
        return replay.family_events[family]

    def _target(
        self, replay: _StoreReplay, family: ReviewOperationalFamily, partition_id: str
    ) -> _PartitionReplay | None:
        directory = self._partition_directory(family, partition_id, create=False)
        return self._replayed(replay, family, directory.name)

    def append(
        self,
        *,
        family: ReviewOperationalFamily,
        partition_id: str,
        event_id: str,
        payload: BaseModel | dict[str, object],
        coordinate: AcceptedCoordinate,
        generation: int,
        actor_context: GovernedActorContext,
        recorded_at: datetime,
        expected_latest_event_digest: str | None | object = _UNCHECKED_PARTITION_HEAD,
    ) -> PlaybillReviewOperationalEventV1:
        # Ordered consumers retain the original chain and compare-and-append
        # semantics. Independent consumption events use append_batch instead.
        refuse_write_while_previewing("review operational store")
        value = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
        payload_digest = _payload_digest(value)
        payload_bytes = canonical_bytes(value) + b"\n"
        with self._locked():
            self._ensure_initialized(
                coordinate=coordinate, generation=generation, initialized_at=recorded_at
            )
            directory = self._partition_directory(family, partition_id, create=True)
            replay = self._sync(compact=True)
            held = self._replayed(replay, family, directory.name)
            existing = () if held is None else held.events
            if held is not None and event_id in held.event_ids:
                event = existing[held.event_ids[event_id]][0]
                if event.payload_digest != payload_digest:
                    raise ReviewOperationalStoreError(
                        "operational event identity has conflicting payload bytes"
                    )
                return event
            latest = existing[-1][0].event_digest if existing else None
            if (
                expected_latest_event_digest is not _UNCHECKED_PARTITION_HEAD
                and expected_latest_event_digest != latest
            ):
                raise ReviewOperationalConcurrentChangeError
            self._touch(family, directory)
            return self._append_verified(
                directory=directory,
                family=family,
                partition_id=partition_id,
                coordinate=coordinate,
                generation=generation,
                actor_context=actor_context,
                recorded_at=recorded_at,
                sequence=len(existing),
                previous=latest
                or review_operational_partition_genesis_digest(
                    instance_id=self.instance_id, family=family, partition_id=partition_id
                ),
                payload_digest=payload_digest,
                payload_bytes=payload_bytes,
            )

    def check_append(
        self,
        *,
        family: ReviewOperationalFamily,
        partition_id: str,
        event_id: str,
        payload: BaseModel | dict[str, object],
        expected_latest_event_digest: str | None | object = _UNCHECKED_PARTITION_HEAD,
    ) -> None:
        """Everything `append` checks before it writes, writing nothing (R12).

        The same event-identity and partition-head checks, read without
        initializing the store or creating the partition.
        """

        value = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
        payload_digest = _payload_digest(value)
        with self._locked():
            held: _PartitionReplay | None = None
            if self.root.exists():
                replay = self._sync()
                self._manifest(replay)
                held = self._target(replay, family, partition_id)
            if held is not None and event_id in held.event_ids:
                event = held.events[held.event_ids[event_id]][0]
                if event.payload_digest != payload_digest:
                    raise ReviewOperationalStoreError(
                        "operational event identity has conflicting payload bytes"
                    )
                return
            latest = held.events[-1][0].event_digest if held is not None and held.events else None
            if (
                expected_latest_event_digest is not _UNCHECKED_PARTITION_HEAD
                and expected_latest_event_digest != latest
            ):
                raise ReviewOperationalConcurrentChangeError

    def ensure_first(
        self,
        *,
        family: ReviewOperationalFamily,
        partition_id: str,
        payload: BaseModel,
        coordinate: AcceptedCoordinate,
        generation: int,
        actor_context: GovernedActorContext,
        recorded_at: datetime,
    ) -> tuple[PlaybillReviewOperationalEventV1, dict[str, object]]:
        """Return the authenticated first event, or atomically initialize it.

        The consumption epoch is the first event in its fixed partition,
        including retained partitions that also contain old receipts. Later
        events are irrelevant to establishing that epoch; only full readers
        verify their chain. Concurrent initializers return the winner.
        """
        value = payload.model_dump(mode="json")
        payload_bytes = canonical_bytes(value) + b"\n"
        with self._locked():
            self._ensure_initialized(
                coordinate=coordinate, generation=generation, initialized_at=recorded_at
            )
            directory = self._partition_directory(family, partition_id, create=False)
            previous = review_operational_partition_genesis_digest(
                instance_id=self.instance_id, family=family, partition_id=partition_id
            )
            if directory.exists() or directory.is_symlink():
                result = self._load_event(directory, family, partition_id, 0, previous)
                _fsync_directory(directory.parent)
                return result
            self._touch(family, directory)
            event = self._publish_partition(
                directory=directory,
                family=family,
                partition_id=partition_id,
                coordinate=coordinate,
                generation=generation,
                actor_context=actor_context,
                recorded_at=recorded_at,
                payload_digest=_payload_digest(value),
                payload_bytes=payload_bytes,
            )
            return event, value

    def first_payload(
        self, *, family: ReviewOperationalFamily, partition_id: str
    ) -> dict[str, object] | None:
        """The authenticated first event's payload of one partition, or None if absent."""
        with self._locked():
            if not self.root.exists() and not self.root.is_symlink():
                return None
            directory = self._partition_directory(family, partition_id, create=False)
            if not directory.exists() and not directory.is_symlink():
                return None
            previous = review_operational_partition_genesis_digest(
                instance_id=self.instance_id, family=family, partition_id=partition_id
            )
            return self._load_event(directory, family, partition_id, 0, previous)[1]

    def partition_payloads(
        self, *, family: ReviewOperationalFamily, partition_id: str
    ) -> tuple[dict[str, object], ...]:
        """One partition's authenticated payloads in chain order; () if absent."""
        with self._locked():
            if not self.root.exists() and not self.root.is_symlink():
                return ()
            replay = self._sync()
            held = self._target(replay, family, partition_id)
            return () if held is None else tuple(payload for _event, payload in held.events)

    def append_batch(
        self,
        *,
        family: ReviewOperationalFamily,
        entries: Sequence[tuple[str, BaseModel | dict[str, object]]],
        coordinate: AcceptedCoordinate,
        generation: int,
        actor_context: GovernedActorContext,
        recorded_at: datetime,
    ) -> tuple[PlaybillReviewOperationalEventV1, ...]:
        """Publish independent events, each at a deterministic singleton partition.

        Each event retains the V1 envelope, with sequence zero and its own
        genesis. It asserts nothing about any other event. A retry verifies only
        its selected source bytes, including after restart. One lock covers the
        bounded batch; publication is an atomic rename per event, not per batch.
        Full history remains readable with the original verifier and no index
        or cached head becomes authoritative.
        """
        if len(entries) > REVIEW_OPERATIONAL_APPEND_BATCH_LIMIT:
            raise ValueError("operational append batch exceeds the item limit")
        if not entries:
            return ()
        prepared = []
        for event_id, payload in entries:
            value = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
            if not event_id or len(event_id) > 506 or value.get("event_id") != event_id:
                raise ValueError("independent event identity must match its payload")
            prepared.append((event_id, _payload_digest(value), canonical_bytes(value) + b"\n"))
        with self._locked():
            self._ensure_initialized(
                coordinate=coordinate, generation=generation, initialized_at=recorded_at
            )
            results = []
            for event_id, payload_digest, payload_bytes in prepared:
                partition_id = f"event:{event_id}"
                directory = self._partition_directory(family, partition_id, create=False)
                family_root = directory.parent
                if directory.exists() or directory.is_symlink():
                    existing = self._load_partition(family, partition_id)
                    if len(existing) != 1 or existing[0][1].get("event_id") != event_id:
                        raise ReviewOperationalStoreError("independent event partition is invalid")
                    prior = existing[0][0]
                    if (
                        prior.payload_digest != payload_digest
                        or canonical_bytes(existing[0][1]) + b"\n" != payload_bytes
                    ):
                        raise ReviewOperationalStoreError(
                            "operational event identity has conflicting payload bytes"
                        )
                    _fsync_directory(family_root)
                    results.append(prior)
                    continue
                self._touch(family, directory)
                event = self._publish_partition(
                    directory=directory,
                    family=family,
                    partition_id=partition_id,
                    coordinate=coordinate,
                    generation=generation,
                    actor_context=actor_context,
                    recorded_at=recorded_at,
                    payload_digest=payload_digest,
                    payload_bytes=payload_bytes,
                )
                results.append(event)
            return tuple(results)

    def _publish_partition(
        self,
        *,
        directory: Path,
        family: ReviewOperationalFamily,
        partition_id: str,
        coordinate: AcceptedCoordinate,
        generation: int,
        actor_context: GovernedActorContext,
        recorded_at: datetime,
        payload_digest: str,
        payload_bytes: bytes,
    ) -> PlaybillReviewOperationalEventV1:
        """Publish one complete partition while holding the writer lock."""
        family_root = directory.parent
        family_root.mkdir(mode=0o700, exist_ok=True)
        if family_root.is_symlink() or not family_root.is_dir():
            raise ReviewOperationalStoreError("operational family root is invalid")
        _fsync_directory(family_root.parent)
        stage = self.root / f".pending-{family}-{directory.name}"
        if stage.is_symlink():
            raise ReviewOperationalStoreError("operational staging root is invalid")
        if stage.exists():
            # Only this operation's unpublished material is disposable.
            shutil.rmtree(stage)
        stage.mkdir(mode=0o700)
        (stage / "events").mkdir(mode=0o700)
        (stage / "payloads").mkdir(mode=0o700)
        event = self._append_verified(
            directory=stage,
            family=family,
            partition_id=partition_id,
            coordinate=coordinate,
            generation=generation,
            actor_context=actor_context,
            recorded_at=recorded_at,
            sequence=0,
            previous=review_operational_partition_genesis_digest(
                instance_id=self.instance_id, family=family, partition_id=partition_id
            ),
            payload_digest=payload_digest,
            payload_bytes=payload_bytes,
        )
        _fsync_directory(stage)
        os.replace(stage, directory)
        self._crash("after_partition_rename")
        _fsync_directory(family_root)
        return event

    def _append_verified(
        self,
        *,
        directory: Path,
        family: ReviewOperationalFamily,
        partition_id: str,
        coordinate: AcceptedCoordinate,
        generation: int,
        actor_context: GovernedActorContext,
        recorded_at: datetime,
        sequence: int,
        previous: str,
        payload_digest: str,
        payload_bytes: bytes,
    ) -> PlaybillReviewOperationalEventV1:
        """Write one item while the caller owns the verified partition lock."""
        payload_path = directory / "payloads" / f"{payload_digest[7:]}.json"
        if payload_path.exists():
            if payload_path.is_symlink() or payload_path.read_bytes() != payload_bytes:
                raise ReviewOperationalStoreError("operational payload path is occupied")
        else:
            _exclusive_write(payload_path, payload_bytes)
        self._crash("after_payload_sync")
        placeholder = "sha256:" + "0" * 64
        draft = PlaybillReviewOperationalEventV1.model_construct(
            tag="playbill-review-operational-event-v1",
            instance_id=self.instance_id,
            family=family,
            partition_id=partition_id,
            sequence=sequence,
            previous_event_digest=previous,
            accepted_coordinate=coordinate,
            accepted_generation=generation,
            actor_context=actor_context,
            payload_digest=payload_digest,
            recorded_at=recorded_at,
            event_digest=placeholder,
        )
        event = PlaybillReviewOperationalEventV1(
            instance_id=self.instance_id,
            family=family,
            partition_id=partition_id,
            sequence=sequence,
            previous_event_digest=previous,
            accepted_coordinate=coordinate,
            accepted_generation=generation,
            actor_context=actor_context,
            payload_digest=payload_digest,
            recorded_at=recorded_at,
            event_digest=review_operational_event_digest(draft),
        )
        _exclusive_write(directory / "events" / f"{sequence:020d}.json", _render(event))
        self._crash("after_event_sync")
        return event

    def remove_partitions(
        self, *, family: ReviewOperationalFamily, partition_ids: Iterable[str]
    ) -> int:
        """Delete whole partitions under retention, each named in the touch log first.

        Only the retention fold that has durably recorded what they held calls
        this; it returns how many partitions were removed.
        """

        refuse_write_while_previewing("review operational retention")
        removed = 0
        with self._locked():
            if not self.root.exists():
                return 0
            for partition_id in partition_ids:
                directory = self._partition_directory(family, partition_id, create=False)
                if not directory.exists() and not directory.is_symlink():
                    continue
                if directory.is_symlink() or not directory.is_dir():
                    raise ReviewOperationalStoreError("operational partition root is invalid")
                self._touch(family, directory)
                shutil.rmtree(directory)
                removed += 1
            if removed:
                _fsync_directory(self.root / "partitions" / family)
        return removed

    def truncate_partition(
        self, *, family: ReviewOperationalFamily, partition_id: str, keep: int
    ) -> int:
        """Cut a chained partition back to its first ``keep`` events; returns how many went.

        The chain is verified whole first. Events are removed newest first, so
        an interrupted cut still leaves a contiguous chain from sequence zero;
        payload files no kept event names are removed after them.
        """

        refuse_write_while_previewing("review operational retention")
        with self._locked():
            if not self.root.exists():
                return 0
            directory = self._partition_directory(family, partition_id, create=False)
            if not directory.exists() and not directory.is_symlink():
                return 0
            loaded = self._load_partition(family, partition_id)
            if len(loaded) <= keep:
                return 0
            kept = {event.payload_digest[7:] + ".json" for event, _payload in loaded[:keep]}
            self._touch(family, directory)
            for event, _payload in reversed(loaded[keep:]):
                (directory / "events" / f"{event.sequence:020d}.json").unlink()
            _fsync_directory(directory / "events")
            for name in os.listdir(directory / "payloads"):
                if name not in kept:
                    (directory / "payloads" / name).unlink()
            _fsync_directory(directory / "payloads")
            return len(loaded) - keep

    def events(
        self, *, family: ReviewOperationalFamily | None = None
    ) -> tuple[tuple[PlaybillReviewOperationalEventV1, dict[str, object]], ...]:
        """Every verified event, by family, partition directory and sequence."""
        with self._locked():
            if not self.root.exists() and not self.root.is_symlink():
                return ()
            replay = self._sync()
            self._manifest(replay)
            loaded: list[tuple[PlaybillReviewOperationalEventV1, dict[str, object]]] = []
            for current in _FAMILIES if family is None else (family,):
                loaded.extend(self._family_events(replay, current))
            return tuple(loaded)

    def _read_unread(self, replay: _StoreReplay, family: ReviewOperationalFamily) -> None:
        """Bring the generation index up to date: only partitions touched since the last read."""

        for name in sorted(replay.unread.get(family, ())):
            held = self._replayed(replay, family, name)
            if held is not None and not held.events:
                raise ReviewOperationalStoreError("operational partition is empty")

    def has_events(self, *, family: ReviewOperationalFamily) -> bool:
        """Whether ``family`` holds any verified event.

        Reads only the partitions touched since the last read, then answers
        from the generation index: no history is visited.
        """
        with self._locked():
            if not self.root.exists() and not self.root.is_symlink():
                return False
            replay = self._sync()
            self._manifest(replay)
            self._read_unread(replay, family)
            return bool(replay.generation_index.get(family))

    def events_in_generation_window(
        self, *, family: ReviewOperationalFamily, lower: int, upper: int
    ) -> tuple[tuple[PlaybillReviewOperationalEventV1, dict[str, object]], ...]:
        """The verified events of ``family`` recorded at accepted generations ``lower..upper``.

        Ordered as :meth:`events` orders them (partition directory, then
        sequence). The work is the partitions touched since the last read plus
        the events in the window, read from the generation index; history
        outside the window is never visited.
        """
        with self._locked():
            if not self.root.exists() and not self.root.is_symlink():
                return ()
            replay = self._sync()
            self._manifest(replay)
            self._read_unread(replay, family)
            index = replay.generation_index.get(family, {})
            if upper < lower:
                return ()
            generations: Iterable[int] = (
                range(lower, upper + 1)
                if upper - lower < len(index)
                else (generation for generation in index if lower <= generation <= upper)
            )
            picked = sorted(
                (name, position)
                for generation in generations
                for name, positions in index.get(generation, {}).items()
                for position in positions
            )
            return tuple(
                replay.partitions[(family, name)].events[position] for name, position in picked
            )

    def head(self) -> ReviewOperationalHeadV1:
        """Every partition's head, rebuilt only when a partition moved since the last one."""
        with self._locked():
            if not self.root.exists() and not self.root.is_symlink():
                return build_review_operational_head(
                    initialized_coordinate=None, initialized_generation=None, partitions=()
                )
            replay = self._sync()
            if replay.head is not None:
                return replay.head
            manifest = self._manifest(replay)
            heads: list[ReviewOperationalPartitionHeadV1] = []
            for family in _FAMILIES:
                for name in sorted(key for held, key in replay.partitions if held == family):
                    held_replay = self._replayed(replay, family, name)
                    if held_replay is None:
                        continue
                    if not held_replay.events:
                        raise ReviewOperationalStoreError("operational partition is empty")
                    event = held_replay.events[-1][0]
                    heads.append(
                        ReviewOperationalPartitionHeadV1(
                            family=event.family,
                            partition_id=event.partition_id,
                            sequence=event.sequence,
                            event_digest=event.event_digest,
                        )
                    )
            replay.head = build_review_operational_head(
                initialized_coordinate=manifest.initialized_coordinate,
                initialized_generation=manifest.initialized_generation,
                partitions=tuple(heads),
            )
            return replay.head


__all__ = [
    "FamilyChanges",
    "PlaybillReviewOperationalEventV1",
    "REVIEW_OPERATIONAL_EVENT_DIGEST_DOMAIN",
    "REVIEW_OPERATIONAL_HEAD_DIGEST_DOMAIN",
    "REVIEW_OPERATIONAL_PARTITION_GENESIS_DOMAIN",
    "REVIEW_OPERATIONAL_STORE_DIRECTORY",
    "ReviewOperationalFamily",
    "ReviewOperationalHeadV1",
    "ReviewOperationalPartitionHeadV1",
    "ReviewOperationalConcurrentChangeError",
    "ReviewOperationalStore",
    "ReviewOperationalStoreError",
    "build_review_operational_head",
    "reset_review_operational_replays",
    "review_operational_event_digest",
    "review_operational_head_digest",
    "review_operational_partition_genesis_digest",
]
