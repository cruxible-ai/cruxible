"""Append-only operational observations outside Playbill governed state.

This store records what one daemon served or mechanically observed.  Its files
are not ledger members and must never participate in semantic, generation, or
candidate identity.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import stat
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.temporal import ensure_utc
from cruxible_core.governance.actor_context import GovernedActorContext

REVIEW_OPERATIONAL_EVENT_DIGEST_DOMAIN = "playbill-review-operational-event-v1"
REVIEW_OPERATIONAL_PARTITION_GENESIS_DOMAIN = "playbill-review-operational-partition-genesis-v1"
REVIEW_OPERATIONAL_HEAD_DIGEST_DOMAIN = "playbill-review-operational-head-v1"
REVIEW_OPERATIONAL_PARTITION_PATH_DOMAIN = "playbill-review-operational-partition-path-v1"
REVIEW_OPERATIONAL_PAYLOAD_DIGEST_DOMAIN = "playbill-review-operational-payload-v1"
REVIEW_OPERATIONAL_STORE_DIRECTORY = "review-operational-v1"

ReviewOperationalFamily: TypeAlias = Literal[
    "curation", "audit", "consumption", "block_observation"
]
REVIEW_OPERATIONAL_APPEND_BATCH_LIMIT = 256
_UNCHECKED_PARTITION_HEAD = object()


class ReviewOperationalStoreError(PlaybillError):
    """Operational state is corrupt, unsafe, or concurrently changed."""

    code = "playbill.curation.operational_store_invalid"

    @property
    def error_code(self) -> str:
        return self.code

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


class ReviewOperationalConcurrentChangeError(PlaybillError):
    """The caller's expected operational partition head is no longer current."""

    code = "playbill.curation.concurrent_change"

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
        value = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
        payload_digest = _payload_digest(value)
        payload_bytes = canonical_bytes(value) + b"\n"
        with self._locked():
            self._ensure_initialized(
                coordinate=coordinate, generation=generation, initialized_at=recorded_at
            )
            directory = self._partition_directory(family, partition_id, create=True)
            existing = (
                self._load_partition(family, partition_id)
                if any((directory / "events").glob("*.json"))
                else ()
            )
            for event, prior_payload in existing:
                if prior_payload.get("event_id") == event_id:
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
            directory = self._partition_directory(family, partition_id, create=False)
            if not directory.exists() and not directory.is_symlink():
                return ()
            return tuple(payload for _event, payload in self._load_partition(family, partition_id))

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

    def events(
        self, *, family: ReviewOperationalFamily | None = None
    ) -> tuple[tuple[PlaybillReviewOperationalEventV1, dict[str, object]], ...]:
        with self._locked():
            if not self.root.exists() and not self.root.is_symlink():
                return ()
            self._load_manifest()
            loaded: list[tuple[PlaybillReviewOperationalEventV1, dict[str, object]]] = []
            families: tuple[ReviewOperationalFamily, ...] = (
                (family,)
                if family is not None
                else (
                    "audit",
                    "block_observation",
                    "consumption",
                    "curation",
                )
            )
            for current_family in families:
                family_root = self.root / "partitions" / current_family
                if not family_root.exists():
                    continue
                if family_root.is_symlink() or not family_root.is_dir():
                    raise ReviewOperationalStoreError("operational family root is invalid")
                for directory in sorted(family_root.iterdir(), key=lambda item: item.name):
                    if directory.is_symlink() or not directory.is_dir():
                        raise ReviewOperationalStoreError("operational partition root is invalid")
                    event_paths = tuple(sorted((directory / "events").glob("*.json")))
                    if not event_paths:
                        raise ReviewOperationalStoreError("operational partition is empty")
                    try:
                        first = PlaybillReviewOperationalEventV1.model_validate_json(
                            event_paths[0].read_bytes()
                        )
                    except (OSError, ValidationError, ValueError) as exc:
                        raise ReviewOperationalStoreError("operational event is malformed") from exc
                    loaded.extend(self._load_partition(current_family, first.partition_id))
            return tuple(loaded)

    def head(self) -> ReviewOperationalHeadV1:
        with self._locked():
            if not self.root.exists() and not self.root.is_symlink():
                return build_review_operational_head(
                    initialized_coordinate=None, initialized_generation=None, partitions=()
                )
            manifest = self._load_manifest()
            heads: list[ReviewOperationalPartitionHeadV1] = []
            for event, _payload in self.events_unlocked():
                if heads and (
                    heads[-1].family == event.family
                    and heads[-1].partition_id == event.partition_id
                ):
                    heads[-1] = ReviewOperationalPartitionHeadV1(
                        family=event.family,
                        partition_id=event.partition_id,
                        sequence=event.sequence,
                        event_digest=event.event_digest,
                    )
                else:
                    heads.append(
                        ReviewOperationalPartitionHeadV1(
                            family=event.family,
                            partition_id=event.partition_id,
                            sequence=event.sequence,
                            event_digest=event.event_digest,
                        )
                    )
            return build_review_operational_head(
                initialized_coordinate=manifest.initialized_coordinate,
                initialized_generation=manifest.initialized_generation,
                partitions=tuple(heads),
            )

    def events_unlocked(
        self,
    ) -> tuple[tuple[PlaybillReviewOperationalEventV1, dict[str, object]], ...]:
        loaded: list[tuple[PlaybillReviewOperationalEventV1, dict[str, object]]] = []
        for family in ("audit", "block_observation", "consumption", "curation"):
            family_root = self.root / "partitions" / family
            if not family_root.exists():
                continue
            if family_root.is_symlink() or not family_root.is_dir():
                raise ReviewOperationalStoreError("operational family root is invalid")
            for directory in sorted(family_root.iterdir(), key=lambda item: item.name):
                if directory.is_symlink() or not directory.is_dir():
                    raise ReviewOperationalStoreError("operational partition root is invalid")
                paths = tuple(sorted((directory / "events").glob("*.json")))
                if not paths:
                    raise ReviewOperationalStoreError("operational partition is empty")
                try:
                    first = PlaybillReviewOperationalEventV1.model_validate_json(
                        paths[0].read_bytes()
                    )
                except (OSError, ValidationError, ValueError) as exc:
                    raise ReviewOperationalStoreError("operational event is malformed") from exc
                loaded.extend(self._load_partition(family, first.partition_id))
        return tuple(loaded)


__all__ = [
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
    "review_operational_event_digest",
    "review_operational_head_digest",
    "review_operational_partition_genesis_digest",
]
