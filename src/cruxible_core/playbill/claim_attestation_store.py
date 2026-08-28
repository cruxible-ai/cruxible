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
    ClaimAttestationEventPayloadV1,
    ClaimAttestationEventV1,
    ClaimAttestationHeadMapEntryV1,
    ClaimAttestationHeadMapNodeV1,
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
            yield
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

    def _chain_marker_path(self, event: ClaimAttestationEventV1) -> Path:
        return self.root / "partitions" / event.partition_digest[7:] / f"{event.sequence:020d}.json"

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

    def _validated_chain(
        self, root_digest: str
    ) -> tuple[tuple[ClaimAttestationPublishedRootV1, ClaimAttestationHeadMapNodeV1], ...]:
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
                matching = tuple(
                    event
                    for event in self._all_markers()
                    if event.event_digest == root.event_digest
                )
                if len(matching) != 1:
                    raise _error("store_corrupt", "published attestation event is not unique")
                event = matching[0]
                expected = dict(prior_map)
                expected[event.partition_digest] = self._partition_head(event)
                if current_map != expected:
                    raise _error("store_corrupt", "attestation published map transition differs")
            loaded.append((root, node))
            prior_map = current_map
        return tuple(loaded)

    def _recover_unpublished(self) -> None:
        pointer = self._load_pointer()
        chain = self._validated_chain(pointer.root_digest)
        published = {root.event_digest for root, _node in chain if root.event_digest is not None}
        eligible = tuple(
            event for event in self._all_markers() if event.event_digest not in published
        )
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
            self._recover_unpublished()
            self._poisoned = False

    def _ready(self) -> ClaimAttestationPublishedRootV1:
        if self._poisoned:
            raise _error("store_poisoned", "attestation store requires synchronous recovery")
        self._recover_unpublished()
        pointer = self._load_pointer()
        chain = self._validated_chain(pointer.root_digest)
        return chain[-1][0]

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
        self._write_object("published-root", root.root_digest, root)
        if crash:
            self._crash("after_step3")
        _replace_pointer(
            self.root / "published.json",
            _render(ClaimAttestationPublishedPointerV1(root_digest=root.root_digest)),
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
            events = self._partition_events(partition_digest)
            for event in events:
                payload = self._payload_for_event(event)
                if (
                    payload.attesting_principal_id == statement.attesting_principal_id
                    and payload.statement_digest == statement_digest
                ):
                    if (
                        payload.envelope_digest != envelope_digest
                        or payload.attestation != attestation
                    ):
                        raise _error(
                            "idempotency_payload_mismatch",
                            "stored attestation differs under the idempotency key",
                        )
                    recorded = self._root_for_event(event.event_digest)
                    return self._result(event, payload, recorded, current_root)
            previous = (
                claim_attestation_partition_genesis_digest(partition_digest)
                if not events
                else events[-1].event_digest
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
                sequence=len(events) + 1,
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
                self._crash("after_step2")
                root = self._publish_event(event, previous_root=current_root, crash=True)
            except BaseException:
                if self._chain_marker_path(event).exists():
                    self._poisoned = True
                raise
            return self._result(event, payload, root, root)

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
        chain = self._validated_chain(self._load_pointer().root_digest)
        matches = tuple(root for root, _node in chain if root.event_digest == event_digest)
        if len(matches) != 1:
            raise _error("store_corrupt", "attestation event has no unique recorded head")
        return matches[0]

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
                return NULL_DIGEST
            self._load_manifest()
            return self._ready().root_digest

    def events(
        self, *, at_head: str | None = None
    ) -> tuple[tuple[ClaimAttestationEventV1, ClaimAttestationEventPayloadV1], ...]:
        with self._locked():
            if not self.root.exists():
                if at_head is not None and at_head != NULL_DIGEST:
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


__all__ = [
    "ClaimAttestationEvidenceStore",
    "ClaimAttestationStoreError",
    "LOCK_FILE",
    "NULL_DIGEST",
    "STORE_DIRECTORY",
]
