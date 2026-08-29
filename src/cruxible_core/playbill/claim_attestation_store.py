"""Crash-safe evidence-plane ledger for principal-authored Claim attestations."""

from __future__ import annotations

import fcntl
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError

from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes
from cruxible_client.contracts.claim_attestation_store import (
    ClaimAttestationAcceleratorV1,
    ClaimAttestationEventPayloadV1,
    ClaimAttestationEventV1,
    ClaimAttestationHeadMapEntryV1,
    ClaimAttestationHeadMapNodeV1,
    ClaimAttestationOutstandingMembershipV1,
    ClaimAttestationPartitionGenesisV1,
    ClaimAttestationPartitionHeadV1,
    ClaimAttestationPublishedPointerV1,
    ClaimAttestationPublishedRootV1,
    ClaimAttestationStoreManifestV1,
    claim_attestation_event_digest,
    claim_attestation_event_payload_digest,
    claim_attestation_head_map_node_digest,
    claim_attestation_partition_digest,
    claim_attestation_partition_genesis_digest,
    claim_attestation_partition_head_digest,
    claim_attestation_published_root_digest,
)
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestationAppendResultV1,
    ClaimAttestationV2,
    VerifiedClaimAttestationV2,
    claim_attestation_v2_envelope_digest,
    claim_attestation_v2_statement_digest,
    claim_attestation_verification_account_digest,
)
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.projection import AcceptedCoordinate

STORE_DIRECTORY = "claim-attestations-v1"
LOCK_FILE = ".claim-attestations-v1.lock"
NULL_DIGEST = "sha256:" + "0" * 64


class ClaimAttestationStoreError(PlaybillError):
    """Persisted evidence is corrupt, ambiguous, or temporarily poisoned."""

    def __init__(self, code: str, message: str) -> None:
        self.error_code = code
        super().__init__(f"{code}: {message}")


def _error(suffix: str, message: str) -> ClaimAttestationStoreError:
    return ClaimAttestationStoreError(f"playbill.claim_attestation.{suffix}", message)


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
        self._validated_chain_cache: (
            tuple[tuple[ClaimAttestationPublishedRootV1, ClaimAttestationHeadMapNodeV1], ...] | None
        ) = None
        self._accelerator_cache: ClaimAttestationAcceleratorV1 | None = None
        self._partition_tips_verified = False
        self._partition_tips: dict[str, ClaimAttestationEventV1] = {}
        self._recorded_root_by_event: dict[str, ClaimAttestationPublishedRootV1] = {}
        self._root_children_verified = False
        self._root_children: dict[str, str] = {}
        self._event_pair_cache: dict[
            str, tuple[ClaimAttestationEventV1, ClaimAttestationEventPayloadV1]
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
            (self.root / "accelerators").mkdir(mode=0o700)
        except OSError as exc:
            raise _error("store_corrupt", "attestation store could not be initialized") from exc
        manifest = ClaimAttestationStoreManifestV1(
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
            _render(ClaimAttestationPublishedPointerV1(root_digest=genesis.root_digest)),
        )
        _fsync_directory(self.root)

    def _load_manifest(self) -> ClaimAttestationStoreManifestV1:
        path = self.root / "manifest.json"
        if self.root.is_symlink() or not self.root.is_dir() or path.is_symlink():
            raise _error("store_corrupt", "attestation store root is invalid")
        try:
            raw = path.read_bytes()
            manifest = ClaimAttestationStoreManifestV1.model_validate_json(raw)
        except (OSError, ValidationError, ValueError) as exc:
            raise _error("store_corrupt", "attestation store manifest is malformed") from exc
        if raw != _render(manifest) or manifest.instance_id != self.instance_id:
            raise _error("store_corrupt", "attestation store manifest does not reproduce")
        return manifest

    @staticmethod
    def _head_map(
        entries: tuple[ClaimAttestationHeadMapEntryV1, ...],
    ) -> ClaimAttestationHeadMapNodeV1:
        ordered = tuple(sorted(entries, key=lambda item: item.partition_digest.encode("ascii")))
        draft = ClaimAttestationHeadMapNodeV1.model_construct(
            tag="playbill-claim-attestation-head-map-node-v1",
            entries=ordered,
            map_digest=NULL_DIGEST,
        )
        return ClaimAttestationHeadMapNodeV1(
            entries=ordered,
            map_digest=claim_attestation_head_map_node_digest(draft),
        )

    def _published_root(
        self,
        *,
        sequence: int,
        previous: str | None,
        event_digest: str | None,
        partition_map_digest: str,
    ) -> ClaimAttestationPublishedRootV1:
        draft = ClaimAttestationPublishedRootV1.model_construct(
            tag="playbill-claim-attestation-published-root-v1",
            instance_id=self.instance_id,
            sequence=sequence,
            previous_published_root_digest=previous,
            event_digest=event_digest,
            partition_map_digest=partition_map_digest,
            root_digest=NULL_DIGEST,
        )
        return ClaimAttestationPublishedRootV1(
            instance_id=self.instance_id,
            sequence=sequence,
            previous_published_root_digest=previous,
            event_digest=event_digest,
            partition_map_digest=partition_map_digest,
            root_digest=claim_attestation_published_root_digest(draft),
        )

    def _empty_root(self) -> ClaimAttestationPublishedRootV1:
        empty_map = self._head_map(())
        return self._published_root(
            sequence=0,
            previous=None,
            event_digest=None,
            partition_map_digest=empty_map.map_digest,
        )

    @staticmethod
    def _partition_head(event: ClaimAttestationEventV1) -> ClaimAttestationPartitionHeadV1:
        draft = ClaimAttestationPartitionHeadV1.model_construct(
            tag="playbill-claim-attestation-partition-head-v1",
            partition_digest=event.partition_digest,
            sequence=event.sequence,
            event_digest=event.event_digest,
            head_digest=NULL_DIGEST,
        )
        return ClaimAttestationPartitionHeadV1(
            partition_digest=event.partition_digest,
            sequence=event.sequence,
            event_digest=event.event_digest,
            head_digest=claim_attestation_partition_head_digest(draft),
        )

    def _load_pointer(self) -> ClaimAttestationPublishedPointerV1:
        path = self.root / "published.json"
        try:
            raw = path.read_bytes()
            pointer = ClaimAttestationPublishedPointerV1.model_validate_json(raw)
        except (OSError, ValidationError, ValueError) as exc:
            return self._recover_pointer(exc)
        if raw != _render(pointer):
            return self._recover_pointer(ValueError("pointer is not canonical"))
        return pointer

    def _all_root_objects(self) -> tuple[ClaimAttestationPublishedRootV1, ...]:
        directory = self.root / "objects" / "published-root" / "sha256"
        roots: list[ClaimAttestationPublishedRootV1] = []
        for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
            try:
                raw = path.read_bytes()
                root = ClaimAttestationPublishedRootV1.model_validate_json(raw)
            except (OSError, ValidationError, ValueError) as exc:
                raise _error("store_corrupt", "attestation published root is malformed") from exc
            if raw != _render(root) or path.stem != root.root_digest[7:]:
                raise _error("store_corrupt", "attestation published root does not reproduce")
            roots.append(root)
        return tuple(roots)

    def _recover_pointer(self, cause: BaseException) -> ClaimAttestationPublishedPointerV1:
        candidates: list[ClaimAttestationPublishedRootV1] = []
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
        pointer = ClaimAttestationPublishedPointerV1(root_digest=maximal[0].root_digest)
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

    def _chain_marker_path(self, event: ClaimAttestationEventV1) -> Path:
        return self.root / "partitions" / event.partition_digest[7:] / f"{event.sequence:020d}.json"

    def _partition_tip_path(self, partition_digest: str) -> Path:
        Sha256Value.from_tagged(partition_digest)
        return self.root / "partitions" / partition_digest[7:] / "head.json"

    def _load_marker(self, path: Path) -> ClaimAttestationEventV1:
        if path.is_symlink() or not path.is_file():
            raise _error("store_corrupt", "attestation chain marker is invalid")
        try:
            raw = path.read_bytes()
            event = ClaimAttestationEventV1.model_validate_json(raw)
        except (OSError, ValidationError, ValueError) as exc:
            raise _error("store_corrupt", "attestation chain marker is malformed") from exc
        if raw != _render(event):
            raise _error("store_corrupt", "attestation chain marker is not canonical")
        return event

    def _partition_events(self, partition_digest: str) -> tuple[ClaimAttestationEventV1, ...]:
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
            genesis = ClaimAttestationPartitionGenesisV1.model_validate_json(genesis_raw)
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

    def _all_markers(self) -> tuple[ClaimAttestationEventV1, ...]:
        events: list[ClaimAttestationEventV1] = []
        partitions = self.root / "partitions"
        for directory in sorted(partitions.iterdir(), key=lambda item: item.name):
            if directory.is_symlink() or not directory.is_dir():
                raise _error("store_corrupt", "attestation partition directory is invalid")
            events.extend(self._partition_events("sha256:" + directory.name))
        return tuple(events)

    def _ensure_partition_tips(self) -> None:
        """Verify every durable chain once, then use one mutable tip per partition."""

        if self._partition_tips_verified:
            return
        tips: dict[str, ClaimAttestationEventV1] = {}
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

    def _record_partition_tip(self, event: ClaimAttestationEventV1) -> None:
        _replace_pointer(
            self._partition_tip_path(event.partition_digest),
            _render(self._partition_head(event)),
        )
        self._partition_tips[event.partition_digest] = event

    @staticmethod
    def _event_extends_map(
        event: ClaimAttestationEventV1,
        prior_map: dict[str, ClaimAttestationPartitionHeadV1],
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

    def _validated_chain(
        self, root_digest: str
    ) -> tuple[tuple[ClaimAttestationPublishedRootV1, ClaimAttestationHeadMapNodeV1], ...]:
        if (
            self._validated_chain_cache is not None
            and self._validated_chain_cache[-1][0].root_digest == root_digest
        ):
            return self._validated_chain_cache
        reversed_chain: list[ClaimAttestationPublishedRootV1] = []
        seen: set[str] = set()
        current = root_digest
        while current not in seen:
            seen.add(current)
            root = self._load_object("published-root", current, ClaimAttestationPublishedRootV1)
            assert isinstance(root, ClaimAttestationPublishedRootV1)
            reversed_chain.append(root)
            if root.previous_published_root_digest is None:
                break
            current = root.previous_published_root_digest
        else:
            raise _error("store_corrupt", "attestation published-root chain forks or cycles")
        chain = tuple(reversed(reversed_chain))
        if not chain or chain[0].sequence != 0:
            raise _error("store_corrupt", "attestation published-root chain lacks genesis")
        loaded: list[tuple[ClaimAttestationPublishedRootV1, ClaimAttestationHeadMapNodeV1]] = []
        prior_map: dict[str, ClaimAttestationPartitionHeadV1] = {}
        published_events: set[str] = set()
        recorded_roots: dict[str, ClaimAttestationPublishedRootV1] = {}
        for index, root in enumerate(chain):
            if root.instance_id != self.instance_id or root.sequence != index:
                raise _error("store_corrupt", "attestation published-root sequence is broken")
            if index and root.previous_published_root_digest != chain[index - 1].root_digest:
                raise _error("store_corrupt", "attestation published-root predecessor differs")
            node = self._load_object(
                "map-node", root.partition_map_digest, ClaimAttestationHeadMapNodeV1
            )
            assert isinstance(node, ClaimAttestationHeadMapNodeV1)
            current_map = {item.partition_digest: item.head for item in node.entries}
            if index == 0:
                if root.event_digest is not None or current_map:
                    raise _error("store_corrupt", "attestation genesis is not empty")
            else:
                assert root.event_digest is not None
                if root.event_digest in published_events:
                    raise _error("store_corrupt", "published attestation event is repeated")
                published_event = self._load_object(
                    "event", root.event_digest, ClaimAttestationEventV1
                )
                assert isinstance(published_event, ClaimAttestationEventV1)
                marker = self._load_marker(self._chain_marker_path(published_event))
                if marker != published_event:
                    raise _error("store_corrupt", "attestation event object and marker differ")
                if published_event.instance_id != self.instance_id or not self._event_extends_map(
                    published_event, prior_map
                ):
                    raise _error(
                        "store_corrupt",
                        "published attestation event does not extend its partition head",
                    )
                expected = dict(prior_map)
                expected[published_event.partition_digest] = self._partition_head(published_event)
                if current_map != expected:
                    raise _error("store_corrupt", "attestation published map transition differs")
                published_events.add(root.event_digest)
                recorded_roots[root.event_digest] = root
            loaded.append((root, node))
            prior_map = current_map
        result = tuple(loaded)
        self._validated_chain_cache = result
        self._recorded_root_by_event = recorded_roots
        return result

    def _recover_unpublished(self) -> None:
        self._ensure_partition_tips()
        pointer = self._load_pointer()
        chain = self._validated_chain(pointer.root_digest)
        published_map = {item.partition_digest: item.head for item in chain[-1][1].entries}
        if not set(published_map).issubset(self._partition_tips):
            raise _error("store_corrupt", "published attestation partition has no durable tip")
        eligible: list[ClaimAttestationEventV1] = []
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
            self._publish_event(eligible[0], previous_root=chain[-1][0], crash=False)

    def recover(self) -> None:
        with self._locked():
            if not self.root.exists():
                self._poisoned = False
                return
            self._load_manifest()
            self._partition_tips_verified = False
            self._partition_tips = {}
            self._root_children_verified = False
            self._root_children = {}
            self._load_pointer()
            self._ensure_root_children()
            self._recover_unpublished()
            self._poisoned = False

    def _ready(self) -> ClaimAttestationPublishedRootV1:
        if self._poisoned:
            raise _error(
                "store_poisoned",
                "attestation store requires recovery; run "
                "`cruxible playbill claim-attestation recover`",
            )
        # Recover or refuse an unreadable pointer before the global fork check,
        # so multiple replay-valid maximal roots retain their distinct typed
        # recovery_ambiguous classification.
        self._load_pointer()
        self._ensure_root_children()
        self._recover_unpublished()
        pointer = self._load_pointer()
        chain = self._validated_chain(pointer.root_digest)
        current = chain[-1][0]
        self._verified_accelerator(chain)
        return current

    def _accelerator_path(self, root_digest: str) -> Path:
        Sha256Value.from_tagged(root_digest)
        return self.root / "accelerators" / f"{root_digest[7:]}.json"

    def _event_pairs_for_chain(
        self,
        chain: tuple[tuple[ClaimAttestationPublishedRootV1, ClaimAttestationHeadMapNodeV1], ...],
    ) -> tuple[tuple[ClaimAttestationEventV1, ClaimAttestationEventPayloadV1], ...]:
        pairs: list[tuple[ClaimAttestationEventV1, ClaimAttestationEventPayloadV1]] = []
        for root, _node in chain[1:]:
            assert root.event_digest is not None
            event = self._load_object("event", root.event_digest, ClaimAttestationEventV1)
            assert isinstance(event, ClaimAttestationEventV1)
            marker = self._load_marker(self._chain_marker_path(event))
            if marker != event:
                raise _error("store_corrupt", "attestation event object and marker differ")
            pair = (event, self._payload_for_event(event))
            self._event_pair_cache[event.event_digest] = pair
            pairs.append(pair)
        return tuple(pairs)

    @staticmethod
    def _build_accelerator(
        root_digest: str,
        pairs: tuple[tuple[ClaimAttestationEventV1, ClaimAttestationEventPayloadV1], ...],
    ) -> ClaimAttestationAcceleratorV1:
        latest: dict[tuple[str, str, str], tuple[int, str]] = {}
        idempotency: list[tuple[str, str, str, str]] = []
        memberships: list[ClaimAttestationOutstandingMembershipV1] = []
        for event, payload in pairs:
            statement = payload.attestation.statement
            latest_key = (
                event.partition_digest,
                statement.attestation_basis,
                statement.attesting_principal_id,
            )
            previous = latest.get(latest_key)
            if previous is None or event.sequence > previous[0]:
                latest[latest_key] = (event.sequence, event.event_digest)
            idempotency.append(
                (
                    event.partition_digest,
                    statement.attesting_principal_id,
                    payload.statement_digest,
                    event.event_digest,
                )
            )
            if statement.attestation_basis == "new_capture":
                memberships.extend(
                    ClaimAttestationOutstandingMembershipV1(
                        claim_identity=statement.claim_identity,
                        capture_digest=capture_digest,
                        event_digest=event.event_digest,
                    )
                    for capture_digest in statement.cited_capture_digests
                )
        return ClaimAttestationAcceleratorV1(
            at_published_root_digest=root_digest,
            latest_event_by_principal=tuple(
                sorted((*key, value[1]) for key, value in latest.items())
            ),
            idempotency_entries=tuple(sorted(idempotency)),
            outstanding_memberships=tuple(
                sorted(
                    memberships,
                    key=lambda item: (
                        item.claim_identity.qualified.encode("utf-8"),
                        item.capture_digest.encode("ascii"),
                        item.event_digest.encode("ascii"),
                    ),
                )
            ),
        )

    def _verified_accelerator(
        self,
        chain: tuple[tuple[ClaimAttestationPublishedRootV1, ClaimAttestationHeadMapNodeV1], ...],
    ) -> ClaimAttestationAcceleratorV1:
        root_digest = chain[-1][0].root_digest
        if (
            self._accelerator_cache is not None
            and self._accelerator_cache.at_published_root_digest == root_digest
        ):
            path = self._accelerator_path(root_digest)
            try:
                if (
                    not path.is_symlink()
                    and path.is_file()
                    and path.read_bytes() == _render(self._accelerator_cache)
                ):
                    return self._accelerator_cache
            except OSError:
                pass
        pairs = self._event_pairs_for_chain(chain)
        expected = self._build_accelerator(root_digest, pairs)
        path = self._accelerator_path(root_digest)
        actual: ClaimAttestationAcceleratorV1 | None = None
        if path.is_file() and not path.is_symlink():
            try:
                raw = path.read_bytes()
                parsed = ClaimAttestationAcceleratorV1.model_validate_json(raw)
                if raw == _render(parsed):
                    actual = parsed
            except (OSError, ValidationError, ValueError):
                actual = None
        if actual != expected:
            _replace_pointer(path, _render(expected))
        self._accelerator_cache = expected
        return expected

    @staticmethod
    def _extend_accelerator(
        previous: ClaimAttestationAcceleratorV1,
        *,
        root_digest: str,
        event: ClaimAttestationEventV1,
        payload: ClaimAttestationEventPayloadV1,
    ) -> ClaimAttestationAcceleratorV1:
        statement = payload.attestation.statement
        latest = {
            (partition, basis, principal): digest
            for partition, basis, principal, digest in previous.latest_event_by_principal
        }
        latest[
            (
                event.partition_digest,
                statement.attestation_basis,
                statement.attesting_principal_id,
            )
        ] = event.event_digest
        idempotency = set(previous.idempotency_entries)
        idempotency.add(
            (
                event.partition_digest,
                statement.attesting_principal_id,
                payload.statement_digest,
                event.event_digest,
            )
        )
        memberships = list(previous.outstanding_memberships)
        if statement.attestation_basis == "new_capture":
            memberships.extend(
                ClaimAttestationOutstandingMembershipV1(
                    claim_identity=statement.claim_identity,
                    capture_digest=capture_digest,
                    event_digest=event.event_digest,
                )
                for capture_digest in statement.cited_capture_digests
            )
        return ClaimAttestationAcceleratorV1(
            at_published_root_digest=root_digest,
            latest_event_by_principal=tuple(
                sorted((*key, digest) for key, digest in latest.items())
            ),
            idempotency_entries=tuple(sorted(idempotency)),
            outstanding_memberships=tuple(
                sorted(
                    memberships,
                    key=lambda item: (
                        item.claim_identity.qualified.encode("utf-8"),
                        item.capture_digest.encode("ascii"),
                        item.event_digest.encode("ascii"),
                    ),
                )
            ),
        )

    def _publish_event(
        self,
        event: ClaimAttestationEventV1,
        *,
        previous_root: ClaimAttestationPublishedRootV1,
        crash: bool,
    ) -> ClaimAttestationPublishedRootV1:
        chain = self._validated_chain(previous_root.root_digest)
        previous_map = chain[-1][1]
        heads = {item.partition_digest: item.head for item in previous_map.entries}
        if event.instance_id != self.instance_id or not self._event_extends_map(event, heads):
            raise _error(
                "store_corrupt",
                "attestation event does not extend the published partition head",
            )
        heads[event.partition_digest] = self._partition_head(event)
        node = self._head_map(
            tuple(
                ClaimAttestationHeadMapEntryV1(partition_digest=key, head=value)
                for key, value in heads.items()
            )
        )
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
            _render(ClaimAttestationPublishedPointerV1(root_digest=root.root_digest)),
        )
        new_chain = (*chain, (root, node))
        self._validated_chain_cache = new_chain
        self._recorded_root_by_event[event.event_digest] = root
        if (
            self._accelerator_cache is not None
            and self._accelerator_cache.at_published_root_digest == previous_root.root_digest
        ):
            payload = self._payload_for_event(event)
            self._accelerator_cache = self._extend_accelerator(
                self._accelerator_cache,
                root_digest=root.root_digest,
                event=event,
                payload=payload,
            )
            _replace_pointer(
                self._accelerator_path(root.root_digest),
                _render(self._accelerator_cache),
            )
        if crash:
            self._crash("after_step4")
        return root

    def append(
        self,
        *,
        attestation: ClaimAttestationV2,
        verification_account: VerifiedClaimAttestationV2,
        note: str | None,
    ) -> ClaimAttestationAppendResultV1:
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
            assert self._accelerator_cache is not None
            duplicate_digest = next(
                (
                    event_digest
                    for partition, principal, digest, event_digest in (
                        self._accelerator_cache.idempotency_entries
                    )
                    if partition == partition_digest
                    and principal == statement.attesting_principal_id
                    and digest == statement_digest
                ),
                None,
            )
            if duplicate_digest is not None:
                event = self._load_object("event", duplicate_digest, ClaimAttestationEventV1)
                assert isinstance(event, ClaimAttestationEventV1)
                payload = self._payload_for_event(event)
                if payload.envelope_digest != envelope_digest or payload.attestation != attestation:
                    raise _error(
                        "idempotency_payload_mismatch",
                        "stored attestation differs under the idempotency key",
                    )
                recorded = self._root_for_event(event.event_digest)
                return self._result(event, payload, recorded, current_root)
            assert self._validated_chain_cache is not None
            current_map = self._validated_chain_cache[-1][1]
            partition_head = next(
                (
                    item.head
                    for item in current_map.entries
                    if item.partition_digest == partition_digest
                ),
                None,
            )
            previous = (
                claim_attestation_partition_genesis_digest(partition_digest)
                if partition_head is None
                else partition_head.event_digest
            )
            payload_draft = ClaimAttestationEventPayloadV1.model_construct(
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
            payload = ClaimAttestationEventPayloadV1(
                **{
                    **payload_draft.model_dump(mode="json"),
                    "payload_digest": claim_attestation_event_payload_digest(payload_draft),
                }
            )
            event_draft = ClaimAttestationEventV1.model_construct(
                tag="playbill-claim-attestation-event-v1",
                instance_id=self.instance_id,
                partition_digest=partition_digest,
                sequence=1 if partition_head is None else partition_head.sequence + 1,
                previous_event_digest=previous,
                payload_digest=payload.payload_digest,
                event_digest=NULL_DIGEST,
            )
            event = ClaimAttestationEventV1(
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
                genesis = ClaimAttestationPartitionGenesisV1(
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
            self._event_pair_cache[event.event_digest] = (event, payload)
            return self._result(event, payload, root, root)

    def duplicate(
        self,
        *,
        attestation: ClaimAttestationV2,
    ) -> ClaimAttestationAppendResultV1 | None:
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
            assert self._accelerator_cache is not None
            event_digest = next(
                (
                    candidate
                    for partition, principal, digest, candidate in (
                        self._accelerator_cache.idempotency_entries
                    )
                    if partition == partition_digest
                    and principal == statement.attesting_principal_id
                    and digest == statement_digest
                ),
                None,
            )
            if event_digest is not None:
                event = self._load_object("event", event_digest, ClaimAttestationEventV1)
                assert isinstance(event, ClaimAttestationEventV1)
                payload = self._payload_for_event(event)
                if payload.envelope_digest != envelope_digest or payload.attestation != attestation:
                    raise _error(
                        "idempotency_payload_mismatch",
                        "stored attestation differs under the idempotency key",
                    )
                recorded = self._root_for_event(event.event_digest)
                return self._result(event, payload, recorded, current_root)
            return None

    def _payload_for_event(self, event: ClaimAttestationEventV1) -> ClaimAttestationEventPayloadV1:
        value = self._load_object("payload", event.payload_digest, ClaimAttestationEventPayloadV1)
        assert isinstance(value, ClaimAttestationEventPayloadV1)
        account = self._load_object(
            "verification-account",
            value.verification_account_digest,
            VerifiedClaimAttestationV2,
        )
        if account != value.verification_account:
            raise _error("store_corrupt", "attestation verification account object differs")
        return value

    def _root_for_event(self, event_digest: str) -> ClaimAttestationPublishedRootV1:
        self._validated_chain(self._load_pointer().root_digest)
        match = self._recorded_root_by_event.get(event_digest)
        if match is None:
            raise _error("store_corrupt", "attestation event has no unique recorded head")
        return match

    @staticmethod
    def _result(
        event: ClaimAttestationEventV1,
        payload: ClaimAttestationEventPayloadV1,
        recorded: ClaimAttestationPublishedRootV1,
        current: ClaimAttestationPublishedRootV1,
    ) -> ClaimAttestationAppendResultV1:
        return ClaimAttestationAppendResultV1(
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
        self, *, at_head: str | None = None
    ) -> tuple[tuple[ClaimAttestationEventV1, ClaimAttestationEventPayloadV1], ...]:
        with self._locked():
            if not self.root.exists():
                if at_head is not None and at_head != self._empty_root().root_digest:
                    raise _error("attestation_head_unknown", "attestation head is unknown")
                return ()
            self._load_manifest()
            current = self._ready()
            target = at_head or current.root_digest
            chain = self._validated_chain(current.root_digest)
            target_indexes = tuple(
                index for index, (root, _node) in enumerate(chain) if root.root_digest == target
            )
            if len(target_indexes) != 1:
                raise _error(
                    "attestation_head_unknown",
                    "attestation head is not a replay-valid ancestor",
                )
            roots = chain[1 : target_indexes[0] + 1]
            loaded: list[tuple[ClaimAttestationEventV1, ClaimAttestationEventPayloadV1]] = []
            for root, _node in roots:
                assert root.event_digest is not None
                event = self._load_object("event", root.event_digest, ClaimAttestationEventV1)
                assert isinstance(event, ClaimAttestationEventV1)
                marker = self._load_marker(self._chain_marker_path(event))
                if marker != event:
                    raise _error("store_corrupt", "attestation event object and marker differ")
                loaded.append((event, self._payload_for_event(event)))
            return tuple(loaded)

    def fold_events(
        self, *, at_head: str | None = None
    ) -> tuple[tuple[ClaimAttestationEventV1, ClaimAttestationEventPayloadV1], ...]:
        """Return only events needed by the threshold and outstanding reducers."""

        with self._locked():
            if not self.root.exists():
                if at_head is not None and at_head != self._empty_root().root_digest:
                    raise _error("attestation_head_unknown", "attestation head is unknown")
                return ()
            self._load_manifest()
            current = self._ready()
            chain = self._validated_chain(current.root_digest)
            target_indexes = tuple(
                index for index, (root, _node) in enumerate(chain) if root.root_digest == at_head
            )
            if at_head is None:
                target_chain = chain
            elif len(target_indexes) == 1:
                target_chain = chain[: target_indexes[0] + 1]
            else:
                raise _error(
                    "attestation_head_unknown",
                    "attestation head is not a replay-valid ancestor",
                )
            accelerator = self._verified_accelerator(target_chain)
            selected = {entry[3] for entry in accelerator.latest_event_by_principal} | {
                entry.event_digest for entry in accelerator.outstanding_memberships
            }
            ordered: list[tuple[ClaimAttestationEventV1, ClaimAttestationEventPayloadV1]] = []
            for root, _node in target_chain[1:]:
                assert root.event_digest is not None
                if root.event_digest not in selected:
                    continue
                pair = self._event_pair_cache.get(root.event_digest)
                if pair is None:
                    event = self._load_object("event", root.event_digest, ClaimAttestationEventV1)
                    assert isinstance(event, ClaimAttestationEventV1)
                    pair = (event, self._payload_for_event(event))
                    self._event_pair_cache[root.event_digest] = pair
                ordered.append(pair)
            return tuple(ordered)


__all__ = [
    "ClaimAttestationEvidenceStore",
    "ClaimAttestationStoreError",
    "LOCK_FILE",
    "NULL_DIGEST",
    "STORE_DIRECTORY",
]
