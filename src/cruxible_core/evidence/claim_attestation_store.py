"""Crash-safe evidence-plane ledger for principal-authored Claim attestations."""

from __future__ import annotations

import fcntl
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes
from cruxible_client.contracts.claim_attestation_store import (
    ClaimAttestationEvent,
    ClaimAttestationEventPayload,
    ClaimAttestationHeadMapDelta,
    ClaimAttestationHeadMapEntry,
    ClaimAttestationHeadMapNode,
    ClaimAttestationPartitionGenesis,
    ClaimAttestationPartitionHead,
    ClaimAttestationPublishedPointer,
    ClaimAttestationPublishedRoot,
    ClaimAttestationStoreManifest,
    claim_attestation_event_digest,
    claim_attestation_event_payload_digest,
    claim_attestation_head_map_delta_digest,
    claim_attestation_head_map_node_digest,
    claim_attestation_partition_digest,
    claim_attestation_partition_genesis_digest,
    claim_attestation_partition_head_digest,
    claim_attestation_published_root_digest,
)
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestation,
    ClaimAttestationAppendResult,
    VerifiedClaimAttestation,
    claim_attestation_v2_envelope_digest,
    claim_attestation_v2_statement_digest,
    claim_attestation_verification_account_digest,
)
from cruxible_client.contracts.errors import CruxibleError
from cruxible_client.contracts.projection import AcceptedCoordinate

STORE_DIRECTORY = "claim-attestations-v1"
LOCK_FILE = ".claim-attestations-v1.lock"
NULL_DIGEST = "sha256:" + "0" * 64


class ClaimAttestationStoreError(CruxibleError):
    """Persisted evidence is corrupt, ambiguous, or temporarily poisoned."""

    def __init__(self, code: str, message: str) -> None:
        self.error_code = code
        super().__init__(f"{code}: {message}")


def _error(suffix: str, message: str) -> ClaimAttestationStoreError:
    return ClaimAttestationStoreError(f"cruxible.claim_attestation.{suffix}", message)


def _render(value: BaseModel) -> bytes:
    return canonical_bytes(value.model_dump(mode="json")) + b"\n"


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _exclusive_write(path: Path, content: bytes) -> None:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        view = memoryview(content)
        while view:
            count = os.write(descriptor, view)
            if count <= 0:  # pragma: no cover - OS contract
                raise _error("store_corrupt", "attestation store write made no progress")
            view = view[count:]
        os.fsync(descriptor)
    except FileExistsError:
        if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
            raise _error("store_corrupt", "attestation content-addressed path is occupied")
        return
    except OSError as exc:
        raise _error("store_corrupt", "attestation object could not be persisted") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    _fsync_directory(path.parent)


def _file_identity(path: Path) -> tuple[int, ...] | None:
    """One stat's identity of a stored object; None when it is absent or unreadable."""

    try:
        status = os.lstat(path)
    except OSError:
        return None
    return (
        status.st_dev,
        status.st_ino,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
        status.st_mode,
    )


def _replace_pointer(path: Path, content: bytes) -> None:
    temp = path.parent / f".{path.name}.tmp.{os.getpid()}"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        view = memoryview(content)
        while view:
            count = os.write(descriptor, view)
            if count <= 0:  # pragma: no cover - OS contract
                raise _error("store_corrupt", "attestation pointer write made no progress")
            view = view[count:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temp, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise _error(
            "store_corrupt", "attestation published pointer could not be replaced"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temp.exists():
            temp.unlink()


@dataclass
class _Chain:
    """The replay-validated published chain, extended in place as roots land.

    ``heads`` is the partition-head map at the tip, kept as one dict: a
    published transition changes one head, so extending the chain by a root
    costs one head, never a copy of every attested Claim's.
    """

    roots: list[ClaimAttestationPublishedRoot] = field(default_factory=list)
    index: dict[str, int] = field(default_factory=dict)
    heads: dict[str, ClaimAttestationPartitionHead] = field(default_factory=dict)
    root_by_event: dict[str, ClaimAttestationPublishedRoot] = field(default_factory=dict)
    #: Each partition's published event digests, in chain order.
    partition_events: dict[str, list[str]] = field(default_factory=dict)

    @property
    def tip(self) -> ClaimAttestationPublishedRoot:
        return self.roots[-1]

    def position(self, event_digest: str) -> int:
        return self.index[self.root_by_event[event_digest].root_digest]


@dataclass
class _Frontier:
    """What the threshold and outstanding reducers and idempotency need, at one root.

    Per-process and rebuilt from the replay-validated chain on a cold start;
    extended by each published event. Never persisted, never authority.
    """

    root_digest: str
    #: (partition, basis, principal) -> latest event digest
    latest: dict[tuple[str, str, str], str] = field(default_factory=dict)
    #: (partition, principal, statement digest) -> event digest
    idempotency: dict[tuple[str, str, str], str] = field(default_factory=dict)
    #: new-capture events citing a Capture: every one stays outstanding
    outstanding: set[str] = field(default_factory=set)

    def add(self, event: ClaimAttestationEvent, payload: ClaimAttestationEventPayload) -> None:
        statement = payload.attestation.statement
        self.latest[
            (event.partition_digest, statement.attestation_basis, statement.attesting_principal_id)
        ] = event.event_digest
        self.idempotency.setdefault(
            (event.partition_digest, statement.attesting_principal_id, payload.statement_digest),
            event.event_digest,
        )
        if statement.attestation_basis == "new_capture" and statement.cited_capture_digests:
            self.outstanding.add(event.event_digest)

    def selected(self) -> set[str]:
        return set(self.latest.values()) | self.outstanding


class ClaimAttestationEvidenceStore:
    """One-event published-root transitions with deterministic roll-forward."""

    def __init__(
        self,
        exhaust_root: Path,
        *,
        instance_id: str,
        crash_hook: Callable[[str], None] | None = None,
    ) -> None:
        if exhaust_root.is_symlink() or not exhaust_root.is_dir():
            raise _error("store_corrupt", "attestation exhaust root is not trustworthy")
        self.exhaust_root = exhaust_root.resolve(strict=True)
        self.root = self.exhaust_root / STORE_DIRECTORY
        self.lock_path = self.exhaust_root / LOCK_FILE
        self.instance_id = instance_id
        self.crash_hook = crash_hook
        self._poisoned = False
        self._chain_cache: _Chain | None = None
        self._frontier: _Frontier | None = None
        self._partition_tips_verified = False
        self._partition_tips: dict[str, ClaimAttestationEvent] = {}
        self._unpublished_checked = False
        self._legacy_accelerators_retired = False
        self._root_children_verified = False
        self._root_children: dict[str, str] = {}
        # Each verified pair with the file identity of every object it was read
        # from (event, marker, payload, verification account): a later read
        # serves it only while all of them still hold, so a lost or rewritten
        # object refuses warm exactly as it does cold.
        self._event_pair_cache: dict[
            str,
            tuple[
                tuple[ClaimAttestationEvent, ClaimAttestationEventPayload],
                tuple[tuple[Path, tuple[int, ...] | None], ...],
            ],
        ] = {}

    @contextmanager
    def _locked(self) -> Iterator[None]:
        if self.lock_path.is_symlink():
            raise _error("store_corrupt", "attestation lock is not trustworthy")
        try:
            descriptor = os.open(
                self.lock_path,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except OSError as exc:
            raise _error("store_corrupt", "attestation lock could not be opened") from exc
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise _error("store_corrupt", "attestation lock is not a regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            except (ValidationError, ValueError) as exc:
                raise _error(
                    "store_corrupt",
                    "attestation store validation failed",
                ) from exc
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _crash(self, boundary: str) -> None:
        if self.crash_hook is not None:
            self.crash_hook(boundary)

    def _object_path(self, kind: str, digest: str) -> Path:
        Sha256Value.from_tagged(digest)
        return self.root / "objects" / kind / "sha256" / f"{digest[7:]}.json"

    def _write_object(self, kind: str, digest: str, value: BaseModel) -> None:
        _exclusive_write(self._object_path(kind, digest), _render(value))

    def _load_object(self, kind: str, digest: str, model: type[BaseModel]) -> BaseModel:
        path = self._object_path(kind, digest)
        if path.is_symlink() or not path.is_file():
            raise _error("store_corrupt", f"attestation {kind} object is missing")
        try:
            raw = path.read_bytes()
            value = model.model_validate_json(raw)
        except (OSError, ValidationError, ValueError) as exc:
            raise _error("store_corrupt", f"attestation {kind} object is malformed") from exc
        if raw != _render(value):
            raise _error("store_corrupt", f"attestation {kind} object is not canonical")
        return value

    def _ensure_initialized(
        self, *, coordinate: AcceptedCoordinate, initialized_at: datetime
    ) -> None:
        if self.root.exists() or self.root.is_symlink():
            self._load_manifest()
            return
        try:
            self.root.mkdir(mode=0o700)
            for kind in (
                "payload",
                "event",
                "map-node",
                "published-root",
                "verification-account",
            ):
                (self.root / "objects" / kind / "sha256").mkdir(parents=True, mode=0o700)
            (self.root / "partitions").mkdir(mode=0o700)
        except OSError as exc:
            raise _error("store_corrupt", "attestation store could not be initialized") from exc
        manifest = ClaimAttestationStoreManifest(
            instance_id=self.instance_id,
            initialized_coordinate=coordinate,
            initialized_at=initialized_at,
        )
        _exclusive_write(self.root / "manifest.json", _render(manifest))
        empty_map = self._head_map(())
        self._write_object("map-node", empty_map.map_digest, empty_map)
        genesis = self._published_root(
            sequence=0,
            previous=None,
            event_digest=None,
            partition_map_digest=empty_map.map_digest,
        )
        self._write_object("published-root", genesis.root_digest, genesis)
        _replace_pointer(
            self.root / "published.json",
            _render(ClaimAttestationPublishedPointer(root_digest=genesis.root_digest)),
        )
        _fsync_directory(self.root)

    def _load_manifest(self) -> ClaimAttestationStoreManifest:
        path = self.root / "manifest.json"
        if self.root.is_symlink() or not self.root.is_dir() or path.is_symlink():
            raise _error("store_corrupt", "attestation store root is invalid")
        try:
            raw = path.read_bytes()
            manifest = ClaimAttestationStoreManifest.model_validate_json(raw)
        except (OSError, ValidationError, ValueError) as exc:
            raise _error("store_corrupt", "attestation store manifest is malformed") from exc
        if raw != _render(manifest) or manifest.instance_id != self.instance_id:
            raise _error("store_corrupt", "attestation store manifest does not reproduce")
        return manifest

    @staticmethod
    def _head_map(
        entries: tuple[ClaimAttestationHeadMapEntry, ...],
    ) -> ClaimAttestationHeadMapNode:
        ordered = tuple(sorted(entries, key=lambda item: item.partition_digest.encode("ascii")))
        draft = ClaimAttestationHeadMapNode.model_construct(
            tag="playbill-claim-attestation-head-map-node-v1",
            entries=ordered,
            map_digest=NULL_DIGEST,
        )
        return ClaimAttestationHeadMapNode(
            entries=ordered,
            map_digest=claim_attestation_head_map_node_digest(draft),
        )

    @staticmethod
    def _head_map_delta(
        previous_map_digest: str, head: ClaimAttestationPartitionHead
    ) -> ClaimAttestationHeadMapDelta:
        draft = ClaimAttestationHeadMapDelta.model_construct(
            tag="playbill-claim-attestation-head-map-delta-v1",
            previous_map_digest=previous_map_digest,
            head=head,
            map_digest=NULL_DIGEST,
        )
        return ClaimAttestationHeadMapDelta(
            previous_map_digest=previous_map_digest,
            head=head,
            map_digest=claim_attestation_head_map_delta_digest(draft),
        )

    def _load_map(self, digest: str) -> ClaimAttestationHeadMapNode | ClaimAttestationHeadMapDelta:
        """A full map (stores written before the delta) or one delta transition."""

        path = self._object_path("map-node", digest)
        if path.is_symlink() or not path.is_file():
            raise _error("store_corrupt", "attestation map-node object is missing")
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise _error("store_corrupt", "attestation map-node object is malformed") from exc
        model: type[ClaimAttestationHeadMapNode] | type[ClaimAttestationHeadMapDelta] = (
            ClaimAttestationHeadMapDelta
            if b'"tag":"playbill-claim-attestation-head-map-delta-v1"' in raw
            else ClaimAttestationHeadMapNode
        )
        value = self._load_object("map-node", digest, model)
        assert isinstance(value, ClaimAttestationHeadMapNode | ClaimAttestationHeadMapDelta)
        if value.map_digest != digest:
            raise _error("store_corrupt", "attestation map-node object is misaddressed")
        return value

    def _published_root(
        self,
        *,
        sequence: int,
        previous: str | None,
        event_digest: str | None,
        partition_map_digest: str,
    ) -> ClaimAttestationPublishedRoot:
        draft = ClaimAttestationPublishedRoot.model_construct(
            tag="playbill-claim-attestation-published-root-v1",
            instance_id=self.instance_id,
            sequence=sequence,
            previous_published_root_digest=previous,
            event_digest=event_digest,
            partition_map_digest=partition_map_digest,
            root_digest=NULL_DIGEST,
        )
        return ClaimAttestationPublishedRoot(
            instance_id=self.instance_id,
            sequence=sequence,
            previous_published_root_digest=previous,
            event_digest=event_digest,
            partition_map_digest=partition_map_digest,
            root_digest=claim_attestation_published_root_digest(draft),
        )

    def _empty_root(self) -> ClaimAttestationPublishedRoot:
        empty_map = self._head_map(())
        return self._published_root(
            sequence=0,
            previous=None,
            event_digest=None,
            partition_map_digest=empty_map.map_digest,
        )

    @staticmethod
    def _partition_head(event: ClaimAttestationEvent) -> ClaimAttestationPartitionHead:
        draft = ClaimAttestationPartitionHead.model_construct(
            tag="playbill-claim-attestation-partition-head-v1",
            partition_digest=event.partition_digest,
            sequence=event.sequence,
            event_digest=event.event_digest,
            head_digest=NULL_DIGEST,
        )
        return ClaimAttestationPartitionHead(
            partition_digest=event.partition_digest,
            sequence=event.sequence,
            event_digest=event.event_digest,
            head_digest=claim_attestation_partition_head_digest(draft),
        )

    def _load_pointer(self) -> ClaimAttestationPublishedPointer:
        path = self.root / "published.json"
        try:
            raw = path.read_bytes()
            pointer = ClaimAttestationPublishedPointer.model_validate_json(raw)
        except (OSError, ValidationError, ValueError) as exc:
            return self._recover_pointer(exc)
        if raw != _render(pointer):
            return self._recover_pointer(ValueError("pointer is not canonical"))
        return pointer

    def _all_root_objects(self) -> tuple[ClaimAttestationPublishedRoot, ...]:
        directory = self.root / "objects" / "published-root" / "sha256"
        roots: list[ClaimAttestationPublishedRoot] = []
        for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
            try:
                raw = path.read_bytes()
                root = ClaimAttestationPublishedRoot.model_validate_json(raw)
            except (OSError, ValidationError, ValueError) as exc:
                raise _error("store_corrupt", "attestation published root is malformed") from exc
            if raw != _render(root) or path.stem != root.root_digest[7:]:
                raise _error("store_corrupt", "attestation published root does not reproduce")
            roots.append(root)
        return tuple(roots)

    def _recover_pointer(self, cause: BaseException) -> ClaimAttestationPublishedPointer:
        candidates: list[ClaimAttestationPublishedRoot] = []
        for root in self._all_root_objects():
            try:
                self._validated_chain(root.root_digest)
            except ClaimAttestationStoreError:
                continue
            candidates.append(root)
        if not candidates:
            raise _error("store_corrupt", "no replay-valid attestation root exists") from cause
        maximum = max(item.sequence for item in candidates)
        maximal = tuple(item for item in candidates if item.sequence == maximum)
        if len(maximal) != 1:
            raise _error("recovery_ambiguous", "attestation published-root recovery is ambiguous")
        pointer = ClaimAttestationPublishedPointer(root_digest=maximal[0].root_digest)
        _replace_pointer(self.root / "published.json", _render(pointer))
        return pointer

    def _ensure_root_children(self) -> None:
        """Verify the immutable root object set once, then extend it in O(1)."""

        if self._root_children_verified:
            return
        children: dict[str, str] = {}
        for root in self._all_root_objects():
            predecessor = root.previous_published_root_digest
            if predecessor is None:
                continue
            existing = children.get(predecessor)
            if existing is not None and existing != root.root_digest:
                raise _error("store_corrupt", "attestation published-root chain forks")
            children[predecessor] = root.root_digest
        self._root_children = children
        self._root_children_verified = True

    def _chain_marker_path(self, event: ClaimAttestationEvent) -> Path:
        return self.root / "partitions" / event.partition_digest[7:] / f"{event.sequence:020d}.json"

    def _partition_tip_path(self, partition_digest: str) -> Path:
        Sha256Value.from_tagged(partition_digest)
        return self.root / "partitions" / partition_digest[7:] / "head.json"

    def _load_marker(self, path: Path) -> ClaimAttestationEvent:
        if path.is_symlink() or not path.is_file():
            raise _error("store_corrupt", "attestation chain marker is invalid")
        try:
            raw = path.read_bytes()
            event = ClaimAttestationEvent.model_validate_json(raw)
        except (OSError, ValidationError, ValueError) as exc:
            raise _error("store_corrupt", "attestation chain marker is malformed") from exc
        if raw != _render(event):
            raise _error("store_corrupt", "attestation chain marker is not canonical")
        return event

    def _partition_events(self, partition_digest: str) -> tuple[ClaimAttestationEvent, ...]:
        directory = self.root / "partitions" / partition_digest[7:]
        if not directory.exists():
            return ()
        if directory.is_symlink() or not directory.is_dir():
            raise _error("store_corrupt", "attestation partition path is invalid")
        genesis_path = directory / "genesis.json"
        if genesis_path.is_symlink() or not genesis_path.is_file():
            raise _error("store_corrupt", "attestation partition genesis is missing")
        try:
            genesis_raw = genesis_path.read_bytes()
            genesis = ClaimAttestationPartitionGenesis.model_validate_json(genesis_raw)
        except (OSError, ValidationError, ValueError) as exc:
            raise _error("store_corrupt", "attestation partition genesis is malformed") from exc
        if genesis_raw != _render(genesis) or genesis.partition_digest != partition_digest:
            raise _error("store_corrupt", "attestation partition genesis does not reproduce")
        paths = tuple(
            path
            for path in sorted(directory.glob("[0-9]*.json"), key=lambda item: item.name)
            if path.name != "genesis.json"
        )
        events = tuple(self._load_marker(path) for path in paths)
        previous = claim_attestation_partition_genesis_digest(partition_digest)
        for index, event in enumerate(events, start=1):
            if (
                paths[index - 1].name != f"{index:020d}.json"
                or event.partition_digest != partition_digest
                or event.sequence != index
                or event.previous_event_digest != previous
            ):
                raise _error("store_corrupt", "attestation partition chain is broken")
            previous = event.event_digest
        return events

    def _ensure_partition_tips(self) -> None:
        """Verify every durable chain once, then use one mutable tip per partition."""

        if self._partition_tips_verified:
            return
        tips: dict[str, ClaimAttestationEvent] = {}
        partitions = self.root / "partitions"
        for directory in sorted(partitions.iterdir(), key=lambda item: item.name):
            if directory.is_symlink() or not directory.is_dir():
                raise _error("store_corrupt", "attestation partition directory is invalid")
            partition_digest = "sha256:" + directory.name
            events = self._partition_events(partition_digest)
            if not events:
                continue
            tip = events[-1]
            tips[partition_digest] = tip
            path = self._partition_tip_path(partition_digest)
            expected = _render(self._partition_head(tip))
            try:
                current = None if path.is_symlink() or not path.is_file() else path.read_bytes()
            except OSError:
                current = None
            if current != expected:
                _replace_pointer(path, expected)
        self._partition_tips = tips
        self._partition_tips_verified = True

    def _record_partition_tip(self, event: ClaimAttestationEvent) -> None:
        _replace_pointer(
            self._partition_tip_path(event.partition_digest),
            _render(self._partition_head(event)),
        )
        self._partition_tips[event.partition_digest] = event

    @staticmethod
    def _event_extends_map(
        event: ClaimAttestationEvent,
        prior_map: dict[str, ClaimAttestationPartitionHead],
    ) -> bool:
        predecessor = prior_map.get(event.partition_digest)
        expected_sequence = 1 if predecessor is None else predecessor.sequence + 1
        expected_digest = (
            claim_attestation_partition_genesis_digest(event.partition_digest)
            if predecessor is None
            else predecessor.event_digest
        )
        return (
            event.sequence == expected_sequence and event.previous_event_digest == expected_digest
        )

    def _validated_chain(self, root_digest: str) -> _Chain:
        """The chain ending at ``root_digest``, replay-validated.

        The validated chain is kept and extended: a root that descends from its
        tip validates only the roots after it, each one map transition (one
        head for a delta, the whole map for a store written before deltas).
        Anything else (a cold start, a root off another branch) validates from
        genesis.
        """

        cache = self._chain_cache
        if cache is not None and cache.tip.root_digest == root_digest:
            return cache
        pending: list[ClaimAttestationPublishedRoot] = []
        seen: set[str] = set()
        current: str | None = root_digest
        while current is not None and (cache is None or current not in cache.index):
            if current in seen:
                raise _error("store_corrupt", "attestation published-root chain forks or cycles")
            seen.add(current)
            root = self._load_object("published-root", current, ClaimAttestationPublishedRoot)
            assert isinstance(root, ClaimAttestationPublishedRoot)
            pending.append(root)
            current = root.previous_published_root_digest
        pending.reverse()
        if cache is not None and current is not None and current == cache.tip.root_digest:
            chain = cache
        else:
            if cache is not None and current is not None:
                pending = [*cache.roots[: cache.index[current] + 1], *pending]
            chain = _Chain()
            if not pending or pending[0].sequence != 0:
                raise _error("store_corrupt", "attestation published-root chain lacks genesis")
        self._chain_cache = None
        for root in pending:
            self._extend_chain(chain, root)
        self._chain_cache = chain
        return chain

    def _extend_chain(self, chain: _Chain, root: ClaimAttestationPublishedRoot) -> None:
        index = len(chain.roots)
        if root.instance_id != self.instance_id or root.sequence != index:
            raise _error("store_corrupt", "attestation published-root sequence is broken")
        if index and root.previous_published_root_digest != chain.tip.root_digest:
            raise _error("store_corrupt", "attestation published-root predecessor differs")
        node = self._load_map(root.partition_map_digest)
        if index == 0:
            if (
                root.event_digest is not None
                or not isinstance(node, ClaimAttestationHeadMapNode)
                or node.entries
            ):
                raise _error("store_corrupt", "attestation genesis is not empty")
        else:
            assert root.event_digest is not None
            if root.event_digest in chain.root_by_event:
                raise _error("store_corrupt", "published attestation event is repeated")
            event = self._load_object("event", root.event_digest, ClaimAttestationEvent)
            assert isinstance(event, ClaimAttestationEvent)
            marker = self._load_marker(self._chain_marker_path(event))
            if marker != event:
                raise _error("store_corrupt", "attestation event object and marker differ")
            if event.instance_id != self.instance_id or not self._event_extends_map(
                event, chain.heads
            ):
                raise _error(
                    "store_corrupt",
                    "published attestation event does not extend its partition head",
                )
            head = self._partition_head(event)
            if isinstance(node, ClaimAttestationHeadMapDelta):
                if node.previous_map_digest != chain.tip.partition_map_digest or node.head != head:
                    raise _error("store_corrupt", "attestation published map transition differs")
            else:
                expected = dict(chain.heads)
                expected[event.partition_digest] = head
                if {item.partition_digest: item.head for item in node.entries} != expected:
                    raise _error("store_corrupt", "attestation published map transition differs")
            chain.heads[event.partition_digest] = head
            chain.root_by_event[event.event_digest] = root
            chain.partition_events.setdefault(event.partition_digest, []).append(event.event_digest)
        chain.roots.append(root)
        chain.index[root.root_digest] = index

    def _recover_unpublished(self) -> None:
        self._ensure_partition_tips()
        pointer = self._load_pointer()
        chain = self._validated_chain(pointer.root_digest)
        published_map = chain.heads
        if not set(published_map).issubset(self._partition_tips):
            raise _error("store_corrupt", "published attestation partition has no durable tip")
        eligible: list[ClaimAttestationEvent] = []
        for partition_digest, tip in self._partition_tips.items():
            published_head = published_map.get(partition_digest)
            if published_head is not None and tip.event_digest == published_head.event_digest:
                continue
            if not self._event_extends_map(tip, published_map):
                raise _error(
                    "store_corrupt",
                    "unpublished attestation event forks or skips its partition head",
                )
            eligible.append(tip)
        if len(eligible) > 1:
            raise _error("store_corrupt", "more than one unpublished attestation event exists")
        if eligible:
            self._publish_event(eligible[0], previous_root=chain.tip, crash=False)

    def recover(self) -> None:
        with self._locked():
            if not self.root.exists():
                self._poisoned = False
                return
            self._load_manifest()
            self._partition_tips_verified = False
            self._partition_tips = {}
            self._unpublished_checked = False
            self._root_children_verified = False
            self._root_children = {}
            self._load_pointer()
            self._ensure_root_children()
            self._recover_unpublished()
            self._unpublished_checked = True
            self._poisoned = False

    def _ready(self) -> ClaimAttestationPublishedRoot:
        if self._poisoned:
            raise _error(
                "store_poisoned",
                "attestation store requires recovery; run `cruxible claim recover-attestation`",
            )
        # Recover or refuse an unreadable pointer before the global fork check,
        # so multiple replay-valid maximal roots retain their distinct typed
        # recovery_ambiguous classification.
        self._load_pointer()
        self._ensure_root_children()
        if not self._unpublished_checked:
            # Every durable partition tip is compared with the published map once
            # per process (and again after `recover`): an event this process
            # appends is published before `append` returns, and one that is not
            # poisons the store until recovery.
            self._recover_unpublished()
            self._unpublished_checked = True
        pointer = self._load_pointer()
        chain = self._validated_chain(pointer.root_digest)
        self._retire_legacy_accelerators()
        self._current_frontier(chain)
        return chain.tip

    def _retire_legacy_accelerators(self) -> None:
        """Remove the accelerator files stores used to keep; idempotent, any crash point.

        They were rebuildable caches, verified against a full recomputation on
        every cold read and rewritten whole on every append. The reducer
        frontier now lives in process only, so the files are collected once.
        """

        if self._legacy_accelerators_retired:
            return
        directory = self.root / "accelerators"
        if directory.is_symlink():
            raise _error("store_corrupt", "stale attestation cache path is not trustworthy")
        if directory.is_dir():
            try:
                for path in directory.iterdir():
                    path.unlink()
                directory.rmdir()
            except OSError as exc:
                raise _error(
                    "store_corrupt", "stale attestation cache could not be collected"
                ) from exc
            _fsync_directory(self.root)
        self._legacy_accelerators_retired = True

    def _event_pair(
        self, event_digest: str
    ) -> tuple[ClaimAttestationEvent, ClaimAttestationEventPayload]:
        """One published event and its payload, authenticated against its marker.

        A pair read before is served from memory only while every object it was
        read from keeps its file identity (four stats); otherwise it is read
        and authenticated again, so a lost or rewritten object refuses warm
        exactly as it does in a new process.
        """

        cached = self._event_pair_cache.get(event_digest)
        if cached is not None:
            pair, identities = cached
            if all(_file_identity(path) == seen for path, seen in identities):
                return pair
        # Every object's identity is taken before its read and again after all
        # reads. Published attestation objects are immutable once written, so an
        # identity that is missing or moved across its read is corruption: it
        # refuses now, and nothing is remembered, so every later read refuses too.
        event_path = self._object_path("event", event_digest)
        before = [(event_path, _file_identity(event_path))]
        event = self._load_object("event", event_digest, ClaimAttestationEvent)
        assert isinstance(event, ClaimAttestationEvent)
        marker_path = self._chain_marker_path(event)
        payload_path = self._object_path("payload", event.payload_digest)
        before += [(marker_path, _file_identity(marker_path))]
        marker = self._load_marker(marker_path)
        if marker != event:
            raise _error("store_corrupt", "attestation event object and marker differ")
        before += [(payload_path, _file_identity(payload_path))]
        payload = self._load_object("payload", event.payload_digest, ClaimAttestationEventPayload)
        assert isinstance(payload, ClaimAttestationEventPayload)
        account_path = self._object_path(
            "verification-account", payload.verification_account_digest
        )
        before += [(account_path, _file_identity(account_path))]
        account = self._load_object(
            "verification-account",
            payload.verification_account_digest,
            VerifiedClaimAttestation,
        )
        if account != payload.verification_account:
            raise _error("store_corrupt", "attestation verification account object differs")
        identities = tuple((path, _file_identity(path)) for path, _seen in before)
        if any(seen is None for _path, seen in before) or identities != tuple(before):
            raise _error("store_corrupt", "attestation evidence object changed while it was read")
        pair = (event, payload)
        self._event_pair_cache[event_digest] = (pair, identities)
        return pair

    def _frontier_through(self, chain: _Chain, position: int) -> _Frontier:
        """The reducer frontier at the root at ``position``, folded from genesis."""

        frontier = _Frontier(root_digest=chain.roots[position].root_digest)
        for root in chain.roots[1 : position + 1]:
            assert root.event_digest is not None
            frontier.add(*self._event_pair(root.event_digest))
        return frontier

    def _current_frontier(self, chain: _Chain) -> _Frontier:
        """The frontier at the chain tip: extended from the kept one when it is an ancestor."""

        frontier = self._frontier
        if frontier is not None and frontier.root_digest == chain.tip.root_digest:
            return frontier
        start = None if frontier is None else chain.index.get(frontier.root_digest)
        if frontier is None or start is None:
            frontier = self._frontier_through(chain, len(chain.roots) - 1)
        else:
            for root in chain.roots[start + 1 :]:
                assert root.event_digest is not None
                frontier.add(*self._event_pair(root.event_digest))
            frontier.root_digest = chain.tip.root_digest
        self._frontier = frontier
        return frontier

    def _publish_event(
        self,
        event: ClaimAttestationEvent,
        *,
        previous_root: ClaimAttestationPublishedRoot,
        crash: bool,
    ) -> ClaimAttestationPublishedRoot:
        chain = self._validated_chain(previous_root.root_digest)
        if event.instance_id != self.instance_id or not self._event_extends_map(event, chain.heads):
            raise _error(
                "store_corrupt",
                "attestation event does not extend the published partition head",
            )
        head = self._partition_head(event)
        # One head and the map it extends: never a restatement of every head.
        node = self._head_map_delta(previous_root.partition_map_digest, head)
        self._write_object("map-node", node.map_digest, node)
        root = self._published_root(
            sequence=previous_root.sequence + 1,
            previous=previous_root.root_digest,
            event_digest=event.event_digest,
            partition_map_digest=node.map_digest,
        )
        existing_child = self._root_children.get(previous_root.root_digest)
        if existing_child is not None and existing_child != root.root_digest:
            raise _error("store_corrupt", "attestation published-root chain forks")
        self._write_object("published-root", root.root_digest, root)
        self._root_children[previous_root.root_digest] = root.root_digest
        if crash:
            self._crash("after_step3")
        _replace_pointer(
            self.root / "published.json",
            _render(ClaimAttestationPublishedPointer(root_digest=root.root_digest)),
        )
        # The transition was built from the validated tip: extend it in place.
        chain.heads[event.partition_digest] = head
        chain.root_by_event[event.event_digest] = root
        chain.partition_events.setdefault(event.partition_digest, []).append(event.event_digest)
        chain.roots.append(root)
        chain.index[root.root_digest] = root.sequence
        if self._frontier is not None and self._frontier.root_digest == previous_root.root_digest:
            self._frontier.add(event, self._payload_for_event(event))
            self._frontier.root_digest = root.root_digest
        if crash:
            self._crash("after_step4")
        return root

    def append(
        self,
        *,
        attestation: ClaimAttestation,
        verification_account: VerifiedClaimAttestation,
        note: str | None,
    ) -> ClaimAttestationAppendResult:
        statement = attestation.statement
        statement_digest = claim_attestation_v2_statement_digest(statement)
        envelope_digest = claim_attestation_v2_envelope_digest(attestation)
        account_digest = claim_attestation_verification_account_digest(verification_account)
        partition_digest = claim_attestation_partition_digest(
            instance_id=self.instance_id,
            claim_identity=statement.claim_identity,
            claim_artifact_digest=statement.claim_artifact_digest,
        )
        with self._locked():
            self._ensure_initialized(
                coordinate=verification_account.append_coordinate,
                initialized_at=verification_account.recorded_at,
            )
            current_root = self._ready()
            assert self._frontier is not None
            duplicate_digest = self._frontier.idempotency.get(
                (partition_digest, statement.attesting_principal_id, statement_digest)
            )
            if duplicate_digest is not None:
                event = self._load_object("event", duplicate_digest, ClaimAttestationEvent)
                assert isinstance(event, ClaimAttestationEvent)
                payload = self._payload_for_event(event)
                if payload.envelope_digest != envelope_digest or payload.attestation != attestation:
                    raise _error(
                        "idempotency_payload_mismatch",
                        "stored attestation differs under the idempotency key",
                    )
                recorded = self._root_for_event(event.event_digest)
                return self._result(event, payload, recorded, current_root)
            assert self._chain_cache is not None
            partition_head = self._chain_cache.heads.get(partition_digest)
            previous = (
                claim_attestation_partition_genesis_digest(partition_digest)
                if partition_head is None
                else partition_head.event_digest
            )
            payload_draft = ClaimAttestationEventPayload.model_construct(
                tag="playbill-claim-attestation-event-payload-v1",
                statement_digest=statement_digest,
                envelope_digest=envelope_digest,
                verification_account_digest=account_digest,
                attestation=attestation,
                verification_account=verification_account,
                note=note,
                recorded_coordinate=verification_account.append_coordinate,
                current_at_append=verification_account.current_at_append,
                attesting_principal_id=statement.attesting_principal_id,
                submitted_by=verification_account.submitted_by,
                recorded_at=verification_account.recorded_at,
                payload_digest=NULL_DIGEST,
            )
            payload = ClaimAttestationEventPayload(
                **{
                    **payload_draft.model_dump(mode="json"),
                    "payload_digest": claim_attestation_event_payload_digest(payload_draft),
                }
            )
            event_draft = ClaimAttestationEvent.model_construct(
                tag="playbill-claim-attestation-event-v1",
                instance_id=self.instance_id,
                partition_digest=partition_digest,
                sequence=1 if partition_head is None else partition_head.sequence + 1,
                previous_event_digest=previous,
                payload_digest=payload.payload_digest,
                event_digest=NULL_DIGEST,
            )
            event = ClaimAttestationEvent(
                **{
                    **event_draft.model_dump(mode="json"),
                    "event_digest": claim_attestation_event_digest(event_draft),
                }
            )
            self._write_object("payload", payload.payload_digest, payload)
            self._write_object("event", event.event_digest, event)
            self._write_object("verification-account", account_digest, verification_account)
            self._crash("after_step1")
            directory = self._chain_marker_path(event).parent
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            if directory.is_symlink() or not directory.is_dir():
                raise _error("store_corrupt", "attestation partition path is invalid")
            genesis_path = directory / "genesis.json"
            if not genesis_path.exists():
                genesis = ClaimAttestationPartitionGenesis(
                    partition_digest=partition_digest,
                    genesis_digest=claim_attestation_partition_genesis_digest(partition_digest),
                )
                _exclusive_write(genesis_path, _render(genesis))
            try:
                _exclusive_write(self._chain_marker_path(event), _render(event))
                self._record_partition_tip(event)
                self._crash("after_step2")
                root = self._publish_event(event, previous_root=current_root, crash=True)
            except BaseException:
                if self._chain_marker_path(event).exists():
                    self._poisoned = True
                raise
            return self._result(event, payload, root, root)

    def duplicate(
        self,
        *,
        attestation: ClaimAttestation,
    ) -> ClaimAttestationAppendResult | None:
        """Return an authenticated duplicate as a read, before append eligibility."""

        statement = attestation.statement
        statement_digest = claim_attestation_v2_statement_digest(statement)
        envelope_digest = claim_attestation_v2_envelope_digest(attestation)
        partition_digest = claim_attestation_partition_digest(
            instance_id=self.instance_id,
            claim_identity=statement.claim_identity,
            claim_artifact_digest=statement.claim_artifact_digest,
        )
        with self._locked():
            if not self.root.exists():
                return None
            self._load_manifest()
            current_root = self._ready()
            assert self._frontier is not None
            event_digest = self._frontier.idempotency.get(
                (partition_digest, statement.attesting_principal_id, statement_digest)
            )
            if event_digest is not None:
                event = self._load_object("event", event_digest, ClaimAttestationEvent)
                assert isinstance(event, ClaimAttestationEvent)
                payload = self._payload_for_event(event)
                if payload.envelope_digest != envelope_digest or payload.attestation != attestation:
                    raise _error(
                        "idempotency_payload_mismatch",
                        "stored attestation differs under the idempotency key",
                    )
                recorded = self._root_for_event(event.event_digest)
                return self._result(event, payload, recorded, current_root)
            return None

    def _payload_for_event(self, event: ClaimAttestationEvent) -> ClaimAttestationEventPayload:
        value = self._load_object("payload", event.payload_digest, ClaimAttestationEventPayload)
        assert isinstance(value, ClaimAttestationEventPayload)
        account = self._load_object(
            "verification-account",
            value.verification_account_digest,
            VerifiedClaimAttestation,
        )
        if account != value.verification_account:
            raise _error("store_corrupt", "attestation verification account object differs")
        return value

    def _root_for_event(self, event_digest: str) -> ClaimAttestationPublishedRoot:
        match = self._validated_chain(self._load_pointer().root_digest).root_by_event.get(
            event_digest
        )
        if match is None:
            raise _error("store_corrupt", "attestation event has no unique recorded head")
        return match

    @staticmethod
    def _result(
        event: ClaimAttestationEvent,
        payload: ClaimAttestationEventPayload,
        recorded: ClaimAttestationPublishedRoot,
        current: ClaimAttestationPublishedRoot,
    ) -> ClaimAttestationAppendResult:
        return ClaimAttestationAppendResult(
            event_digest=event.event_digest,
            partition_digest=event.partition_digest,
            statement_digest=payload.statement_digest,
            envelope_digest=payload.envelope_digest,
            partition_sequence=event.sequence,
            recorded_coordinate=payload.recorded_coordinate,
            recorded_head=recorded.root_digest,
            current_head=current.root_digest,
            submitted_by=payload.submitted_by,
            recorded_at=payload.recorded_at,
        )

    def head(self) -> str:
        with self._locked():
            if not self.root.exists():
                return self._empty_root().root_digest
            self._load_manifest()
            return self._ready().root_digest

    def events(
        self, *, at_head: str | None = None, partition_digest: str | None = None
    ) -> tuple[tuple[ClaimAttestationEvent, ClaimAttestationEventPayload], ...]:
        """Every published event through ``at_head`` (the current head by default).

        ``partition_digest`` keeps one Claim version's events: read from that
        partition's own list, never by walking every event in the store.
        """

        with self._locked():
            if not self.root.exists():
                if at_head is not None and at_head != self._empty_root().root_digest:
                    raise _error("attestation_head_unknown", "attestation head is unknown")
                return ()
            self._load_manifest()
            current = self._ready()
            chain = self._validated_chain(current.root_digest)
            position = chain.index.get(at_head or current.root_digest)
            if position is None:
                raise _error(
                    "attestation_head_unknown",
                    "attestation head is not a replay-valid ancestor",
                )
            if partition_digest is None:
                digests = [root.event_digest for root in chain.roots[1 : position + 1]]
            else:
                digests = [
                    digest
                    for digest in chain.partition_events.get(partition_digest, ())
                    if chain.position(digest) <= position
                ]
            return tuple(self._event_pair(digest) for digest in digests if digest is not None)

    def events_after(
        self, root_digest: str | None
    ) -> tuple[str, tuple[tuple[int, ClaimAttestationEvent, ClaimAttestationEventPayload], ...]]:
        """The current head and every event published after ``root_digest``, in chain order.

        Each event comes with its chain position (its published root's
        sequence). Only the roots past ``root_digest`` are read, over the cached
        validated chain, so a caller following the door pays for what is new;
        ``None`` reads from genesis. A root that is not a replay-valid ancestor
        of the head is refused (``attestation_head_unknown``): the follower
        must start over.
        """

        with self._locked():
            if not self.root.exists():
                empty = self._empty_root().root_digest
                if root_digest not in {None, empty}:
                    raise _error("attestation_head_unknown", "attestation head is unknown")
                return empty, ()
            self._load_manifest()
            current = self._ready()
            chain = self._validated_chain(current.root_digest)
            position = 0 if root_digest is None else chain.index.get(root_digest)
            if position is None:
                raise _error(
                    "attestation_head_unknown",
                    "attestation head is not a replay-valid ancestor",
                )
            return current.root_digest, tuple(
                (root.sequence, *self._event_pair(root.event_digest))
                for root in chain.roots[position + 1 :]
                if root.event_digest is not None
            )

    def fold_events(
        self, *, at_head: str | None = None
    ) -> tuple[tuple[ClaimAttestationEvent, ClaimAttestationEventPayload], ...]:
        """Return only events needed by the threshold and outstanding reducers."""

        with self._locked():
            if not self.root.exists():
                if at_head is not None and at_head != self._empty_root().root_digest:
                    raise _error("attestation_head_unknown", "attestation head is unknown")
                return ()
            self._load_manifest()
            current = self._ready()
            chain = self._validated_chain(current.root_digest)
            position = chain.index.get(at_head or current.root_digest)
            if position is None:
                raise _error(
                    "attestation_head_unknown",
                    "attestation head is not a replay-valid ancestor",
                )
            frontier = (
                self._current_frontier(chain)
                if position == len(chain.roots) - 1
                # A historical head folds its own prefix: a read of the past.
                else self._frontier_through(chain, position)
            )
            selected = sorted(frontier.selected(), key=chain.position)
            return tuple(self._event_pair(digest) for digest in selected)


__all__ = [
    "ClaimAttestationEvidenceStore",
    "ClaimAttestationStoreError",
    "LOCK_FILE",
    "NULL_DIGEST",
    "STORE_DIRECTORY",
]
