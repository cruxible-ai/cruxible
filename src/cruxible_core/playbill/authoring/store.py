"""Durable append-only protocol state for daemon-owned AuthoringIntents."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import threading
from collections import OrderedDict
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeAlias, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from cruxible_client.contracts.authoring.models import (
    AuthoringIntentV1,
    AuthoringIntentV2,
    AuthoringPayloadV1,
    AuthoringProgramStampV1,
    InsertionExpectationV2,
    authoring_program_stamp_operation_key,
)
from cruxible_client.contracts.canonical import (
    Sha256Value,
    canonical_bytes,
    normalize_canonical,
    typed_digest,
)
from cruxible_client.contracts.errors import PlaybillError
from cruxible_client.contracts.primitives import canonical_json
from cruxible_core.playbill.id_prefixes import resolve_id_prefix

AUTHORING_INTENT_EVENT_DIGEST_DOMAIN = "playbill-authoring-intent-event-v1"
AUTHORING_INTENT_EVENT_V2_DIGEST_DOMAIN = "playbill-authoring-intent-event-v2"
AUTHORING_INTENT_EVENT_V3_DIGEST_DOMAIN = "playbill-authoring-intent-event-v3"
_TERMINAL_STATES = frozenset({"accepted", "superseded", "terminal"})
_LIVE_INSERTION_STATES = frozenset(
    {"awaiting_claim_acceptance", "pending", "prepared", "confirming"}
)


def _intent_is_pending(intent: AuthoringIntentV1) -> bool:
    expectation = intent.insertion_expectation
    if expectation is not None and expectation.state in _LIVE_INSERTION_STATES:
        return True
    return intent.candidate_status.state not in _TERMINAL_STATES


class AuthoringIntentStoreError(PlaybillError):
    """Durable AuthoringIntent state is missing, corrupt, or concurrently changed."""


class _StrictStoreModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuthoringIntentEventV1(_StrictStoreModel):
    tag: Literal["playbill-authoring-intent-event-v1"] = "playbill-authoring-intent-event-v1"
    sequence: int = Field(ge=0)
    previous_event_digest: str | None
    operation_key: str
    intent: AuthoringIntentV1
    event_digest: str

    @field_validator("previous_event_digest", "operation_key", "event_digest")
    @classmethod
    def _digests(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _reproduces(self, info: ValidationInfo) -> "AuthoringIntentEventV1":
        _verify_authoring_event_digest(self, info.context)
        return self


class AuthoringIntentEventV2(_StrictStoreModel):
    tag: Literal["playbill-authoring-intent-event-v2"] = "playbill-authoring-intent-event-v2"
    sequence: int = Field(ge=0)
    previous_event_digest: str | None
    operation_key: str
    intent: AuthoringIntentV2
    event_digest: str

    @field_validator("previous_event_digest", "operation_key", "event_digest")
    @classmethod
    def _digests(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _reproduces(self, info: ValidationInfo) -> "AuthoringIntentEventV2":
        _verify_authoring_event_digest(self, info.context)
        return self


class AuthoringIntentEventV3(_StrictStoreModel):
    tag: Literal["playbill-authoring-intent-event-v3"] = "playbill-authoring-intent-event-v3"
    sequence: int = Field(ge=0)
    previous_event_digest: str | None
    operation_key: str
    intent: AuthoringIntentV2
    program_stamp: AuthoringProgramStampV1
    event_digest: str

    @field_validator("previous_event_digest", "operation_key", "event_digest")
    @classmethod
    def _digests(cls, value: str | None) -> str | None:
        if value is not None:
            Sha256Value.from_tagged(value)
        return value

    @model_validator(mode="after")
    def _reproduces(self, info: ValidationInfo) -> "AuthoringIntentEventV3":
        _verify_authoring_event_digest(self, info.context)
        return self


AuthoringIntentEventAny: TypeAlias = (
    AuthoringIntentEventV1 | AuthoringIntentEventV2 | AuthoringIntentEventV3
)


@dataclass
class _EventDecodeContext:
    """One decoder's output slot, never populated from the wire or retained."""

    rendered: bytes | None = None


def _verify_authoring_event_digest(event: AuthoringIntentEventAny, context: object) -> None:
    if type(context) is not _EventDecodeContext:
        if event.event_digest != authoring_intent_event_digest(event):
            raise ValueError("AuthoringIntent event digest does not reproduce")
        return
    # Nested models have completed their full validation. Their own serializers
    # select the frozen source-presence and preflight-certificate wire shapes.
    context.rendered = None
    normalized = normalize_canonical(event.model_dump(mode="json"))
    assert isinstance(normalized, dict)
    preimage = dict(normalized)
    preimage.pop("tag")
    preimage.pop("event_digest")
    # Every runtime value was normalized above; the selected domain is frozen
    # ASCII. This is the public digest preimage with no second normalization.
    expected = (
        "sha256:"
        + hashlib.sha256(
            canonical_json({"tag": _authoring_event_digest_domain(event), **preimage}).encode(
                "utf-8"
            )
        ).hexdigest()
    )
    if event.event_digest != expected:
        raise ValueError("AuthoringIntent event digest does not reproduce")
    # Publish only after digest reproduction; a caller still has to compare the
    # supplied raw bytes before treating this representation as canonical.
    context.rendered = canonical_json(normalized).encode("utf-8") + b"\n"


_StateT = TypeVar("_StateT")


@dataclass(frozen=True)
class AuthoringIntentPublicationState:
    """Current publication protocol fields, without authored bodies or preflight."""

    intent_id: str
    insertion_expectations: tuple[InsertionExpectationV2, ...]


@dataclass(frozen=True)
class _ValidatedEvent:
    raw_digest: bytes
    raw_size: int
    event: AuthoringIntentEventAny


# Serialized byte weight bounds retained input size, not Python heap usage.
# Entries are private: frozen contract models still contain mutable containers.
_HISTORY_MEMO_MAX_BYTES = 256 * 1024 * 1024
_HISTORY_MEMO_MAX_STREAMS = 128
_HISTORY_MEMO: OrderedDict[Path, tuple[_ValidatedEvent, ...]] = OrderedDict()
_HISTORY_MEMO_BYTES = 0
_HISTORY_MEMO_LOCK = threading.Lock()


def _reset_authoring_history_memo() -> None:
    global _HISTORY_MEMO_BYTES
    with _HISTORY_MEMO_LOCK:
        _HISTORY_MEMO.clear()
        _HISTORY_MEMO_BYTES = 0


def _history_memo_get(directory: Path) -> tuple[_ValidatedEvent, ...]:
    with _HISTORY_MEMO_LOCK:
        cached = _HISTORY_MEMO.get(directory, ())
        if cached:
            _HISTORY_MEMO.move_to_end(directory)
        return cached


def _history_memo_put(directory: Path, events: tuple[_ValidatedEvent, ...]) -> None:
    global _HISTORY_MEMO_BYTES
    with _HISTORY_MEMO_LOCK:
        previous = _HISTORY_MEMO.pop(directory, ())
        _HISTORY_MEMO_BYTES -= sum(item.raw_size for item in previous)
        weight = sum(item.raw_size for item in events)
        if weight <= _HISTORY_MEMO_MAX_BYTES:
            _HISTORY_MEMO[directory] = events
            _HISTORY_MEMO_BYTES += weight
        while _HISTORY_MEMO and (
            _HISTORY_MEMO_BYTES > _HISTORY_MEMO_MAX_BYTES
            or len(_HISTORY_MEMO) > _HISTORY_MEMO_MAX_STREAMS
        ):
            _, evicted = _HISTORY_MEMO.popitem(last=False)
            _HISTORY_MEMO_BYTES -= sum(item.raw_size for item in evicted)


def authoring_intent_event_digest(event: AuthoringIntentEventAny) -> str:
    payload = event.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("event_digest")
    return typed_digest(
        Sha256Value,
        _authoring_event_digest_domain(event),
        payload,
    ).tagged


def _authoring_event_digest_domain(event: AuthoringIntentEventAny) -> str:
    if isinstance(event, AuthoringIntentEventV3):
        return AUTHORING_INTENT_EVENT_V3_DIGEST_DOMAIN
    if isinstance(event, AuthoringIntentEventV2):
        return AUTHORING_INTENT_EVENT_V2_DIGEST_DOMAIN
    return AUTHORING_INTENT_EVENT_DIGEST_DOMAIN


def build_authoring_intent_event(
    *,
    sequence: int,
    previous_event_digest: str | None,
    operation_key: str,
    intent: AuthoringIntentV1,
    program_stamp: AuthoringProgramStampV1 | None = None,
) -> AuthoringIntentEventAny:
    placeholder = "sha256:" + "0" * 64
    if program_stamp is not None:
        if not isinstance(intent, AuthoringIntentV2):
            raise ValueError("program stamps require a v2 AuthoringIntent")
        event_v3 = AuthoringIntentEventV3.model_construct(
            tag="playbill-authoring-intent-event-v3",
            sequence=sequence,
            previous_event_digest=previous_event_digest,
            operation_key=operation_key,
            intent=intent,
            program_stamp=program_stamp,
            event_digest=placeholder,
        )
        return AuthoringIntentEventV3(
            sequence=sequence,
            previous_event_digest=previous_event_digest,
            operation_key=operation_key,
            intent=intent,
            program_stamp=program_stamp,
            event_digest=authoring_intent_event_digest(event_v3),
        )
    if isinstance(intent, AuthoringIntentV2):
        event_v2 = AuthoringIntentEventV2.model_construct(
            tag="playbill-authoring-intent-event-v2",
            sequence=sequence,
            previous_event_digest=previous_event_digest,
            operation_key=operation_key,
            intent=intent,
            event_digest=placeholder,
        )
        return AuthoringIntentEventV2(
            sequence=sequence,
            previous_event_digest=previous_event_digest,
            operation_key=operation_key,
            intent=intent,
            event_digest=authoring_intent_event_digest(event_v2),
        )
    event = AuthoringIntentEventV1.model_construct(
        tag="playbill-authoring-intent-event-v1",
        sequence=sequence,
        previous_event_digest=previous_event_digest,
        operation_key=operation_key,
        intent=intent,
        event_digest=placeholder,
    )
    return AuthoringIntentEventV1(
        sequence=sequence,
        previous_event_digest=previous_event_digest,
        operation_key=operation_key,
        intent=intent,
        event_digest=authoring_intent_event_digest(event),
    )


def _authoring_event_input(
    raw: bytes,
) -> tuple[type[AuthoringIntentEventAny], dict[str, object]]:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("AuthoringIntent event must be an object")
    if payload.get("tag") == "playbill-authoring-intent-event-v2":
        return AuthoringIntentEventV2, payload
    if payload.get("tag") == "playbill-authoring-intent-event-v3":
        return AuthoringIntentEventV3, payload
    return AuthoringIntentEventV1, payload


def _parse_authoring_intent_event(raw: bytes) -> AuthoringIntentEventAny:
    model, payload = _authoring_event_input(raw)
    return model.model_validate(payload)


def _decode_authoring_intent_event(raw: bytes) -> tuple[AuthoringIntentEventAny, bytes]:
    """Fully validate a fresh event and return its model-derived canonical bytes."""

    model, payload = _authoring_event_input(raw)
    context = _EventDecodeContext()
    event = model.model_validate(payload, context=context)
    assert context.rendered is not None
    return event, context.rendered


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
                raise AuthoringIntentStoreError("AuthoringIntent write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    except FileExistsError as exc:
        raise AuthoringIntentStoreError("AuthoringIntent event path is occupied") from exc
    except OSError as exc:
        raise AuthoringIntentStoreError("AuthoringIntent event could not be persisted") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    _fsync_directory(path.parent)


class AuthoringIntentStore:
    """An immutable event stream whose latest canonical snapshot is the intent."""

    def __init__(
        self,
        exhaust_root: Path,
        *,
        crash_hook: Callable[[str], None] | None = None,
        token_factory: Callable[[], str] | None = None,
        read_only: bool = False,
    ) -> None:
        if exhaust_root.is_symlink() or not exhaust_root.is_dir():
            raise AuthoringIntentStoreError("AuthoringIntent exhaust root is not trustworthy")
        self.root = exhaust_root.resolve(strict=True) / "authoring-intents"
        if not read_only:
            self.root.mkdir(mode=0o700, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise AuthoringIntentStoreError("AuthoringIntent root is not trustworthy")
        if not read_only:
            os.chmod(self.root, 0o700)
        self._lock_path = self.root / ".lock"
        self._crash_hook = crash_hook
        self._token_factory = token_factory or (lambda: secrets.token_hex(16))
        self._read_only = read_only

    def _crash(self, boundary: str) -> None:
        if self._crash_hook is not None:
            self._crash_hook(boundary)

    @contextmanager
    def _locked(self):  # type: ignore[no-untyped-def]
        descriptor = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def mint_intent_id(self) -> str:
        return f"AIT-{self._token_factory()}"

    def create(self, intent: AuthoringIntentV1, *, operation_key: str) -> AuthoringIntentV1:
        with self._locked():
            self._recover_creating_directories()
            existing = self._active_by_fingerprint(
                intent.create_fingerprint,
                actor_id=intent.actor_id,
            )
            if existing is not None:
                return existing
            directory = self.root / intent.intent_id
            if directory.exists():
                current = self._validated_events(directory)[-1].intent
                if current == intent:
                    return current.model_copy(deep=True)
                raise AuthoringIntentStoreError("minted AuthoringIntent path is occupied")
            temporary = self.root / f".creating-{intent.intent_id}-{secrets.token_hex(8)}"
            events = temporary / "events"
            events.mkdir(mode=0o700, parents=True)
            event = build_authoring_intent_event(
                sequence=0,
                previous_event_digest=None,
                operation_key=operation_key,
                intent=intent,
            )
            _exclusive_write(events / "00000000000000000000.json", self._render_event(event))
            self._crash("after_create_event_sync")
            os.replace(temporary, directory)
            _fsync_directory(self.root)
            self._crash("after_create_publish")
            return intent

    def _recover_creating_directories(self) -> None:
        """Publish any fully synced create event left before the atomic rename."""

        for temporary in sorted(self.root.glob(".creating-AIT-*"), key=lambda item: item.name):
            if temporary.is_symlink() or not temporary.is_dir():
                raise AuthoringIntentStoreError("AuthoringIntent create staging is invalid")
            paths = tuple((temporary / "events").glob("*.json"))
            if len(paths) != 1 or paths[0].name != "00000000000000000000.json":
                raise AuthoringIntentStoreError("AuthoringIntent create staging is incomplete")
            try:
                raw = paths[0].read_bytes()
                event = _parse_authoring_intent_event(raw)
            except (OSError, ValidationError, ValueError) as exc:
                raise AuthoringIntentStoreError(
                    "AuthoringIntent create staging is malformed"
                ) from exc
            if (
                raw != self._render_event(event)
                or event.sequence != 0
                or event.previous_event_digest is not None
                or not temporary.name.startswith(f".creating-{event.intent.intent_id}-")
            ):
                raise AuthoringIntentStoreError("AuthoringIntent create staging does not reproduce")
            final = self.root / event.intent.intent_id
            if final.exists():
                continue
            os.replace(temporary, final)
            _fsync_directory(self.root)

    def resolve_intent_id(self, intent_id: str) -> str:
        """Accept a unique AIT- prefix where a full intent id is expected."""

        return resolve_id_prefix(
            intent_id,
            tuple(path.name for path in self._intent_directories()),
            marker="AIT-",
            label="AuthoringIntent",
        )

    def get(self, intent_id: str, *, actor_id: str) -> AuthoringIntentV1:
        intent_id = self.resolve_intent_id(intent_id)
        with self._locked():
            events = self._validated_events(self.root / intent_id)
            intent = events[-1].intent
            if intent.actor_id != actor_id:
                raise AuthoringIntentStoreError("AuthoringIntent belongs to another actor")
            return intent.model_copy(deep=True)

    def list_pending(self, *, actor_id: str) -> tuple[AuthoringIntentV1, ...]:
        with self._locked():
            pending: list[AuthoringIntentV1] = []
            for directory in self._intent_directories():
                intent = self._validated_events(directory)[-1].intent
                if intent.actor_id == actor_id and _intent_is_pending(intent):
                    pending.append(intent.model_copy(deep=True))
            return tuple(sorted(pending, key=lambda item: item.intent_id.encode("ascii")))

    def events(self) -> tuple[AuthoringIntentEventAny, ...]:
        """Return every durable intent transition in canonical stream order.

        Curation consumes these records as immutable attempt evidence.  It does
        not infer from mutable intent snapshots or inspect the filesystem
        outside this validated store boundary.
        """

        if self._read_only:
            return tuple(
                event.model_copy(deep=True)
                for directory in self._intent_directories()
                for event in self._validated_events(directory)
            )
        with self._locked():
            self._recover_creating_directories()
            return tuple(
                event.model_copy(deep=True)
                for directory in self._intent_directories()
                for event in self._validated_events(directory)
            )

    def latest_intents(self) -> tuple[AuthoringIntentV1, ...]:
        """Latest validated recorded snapshots, without replay history or refresh.

        Read-only consumers do not recover staged writes. Writable consumers use
        the existing writer lock and recovery boundary. Returned snapshots are
        owned by the caller; protocol refresh remains the coordinator's job.
        """

        return self._latest_states(lambda intent: intent.model_copy(deep=True))

    def publication_states(self) -> tuple[AuthoringIntentPublicationState, ...]:
        """Validated current publication fields, detached from private history."""

        return self._latest_states(
            lambda intent: AuthoringIntentPublicationState(
                intent_id=intent.intent_id,
                insertion_expectations=tuple(
                    item.model_copy(deep=True) for item in intent.insertion_expectations
                ),
            )
        )

    def _latest_states(
        self, project: Callable[[AuthoringIntentV1], _StateT]
    ) -> tuple[_StateT, ...]:
        def snapshots() -> tuple[_StateT, ...]:
            return tuple(
                project(self._validated_events(directory)[-1].intent)
                for directory in self._intent_directories()
            )

        if self._read_only:
            return snapshots()
        with self._locked():
            self._recover_creating_directories()
            return snapshots()

    def latest_transition(
        self,
        intent_id: str,
        *,
        actor_id: str,
    ) -> tuple[AuthoringIntentV1 | None, AuthoringIntentEventAny]:
        """Return the exact predecessor and latest event for protocol retry checks."""

        with self._locked():
            events = self._validated_events(self.root / intent_id)
            latest = events[-1]
            if latest.intent.actor_id != actor_id:
                raise AuthoringIntentStoreError("AuthoringIntent belongs to another actor")
            predecessor = None if len(events) == 1 else events[-2].intent.model_copy(deep=True)
            return predecessor, latest.model_copy(deep=True)

    def operation_result(
        self,
        intent_id: str,
        *,
        actor_id: str,
        operation_key: str,
    ) -> AuthoringIntentV1 | None:
        """Return a previously committed operation result without appending an event."""

        with self._locked():
            events = self._validated_events(self.root / intent_id)
            if events[-1].intent.actor_id != actor_id:
                raise AuthoringIntentStoreError("AuthoringIntent belongs to another actor")
            return next(
                (
                    event.intent.model_copy(deep=True)
                    for event in events
                    if event.operation_key == operation_key
                ),
                None,
            )

    def transition(
        self,
        intent_id: str,
        *,
        actor_id: str,
        operation_key: str,
        transform: Callable[[AuthoringIntentV1], AuthoringIntentV1],
        allow_rebase: bool = False,
        program_stamp: AuthoringProgramStampV1 | None = None,
    ) -> AuthoringIntentV1:
        """Append one idempotent state transition under the store-wide CAS lock."""

        with self._locked():
            self._recover_creating_directories()
            directory = self.root / intent_id
            events = self._validated_events(directory)
            for event in events:
                if event.operation_key == operation_key:
                    return event.intent.model_copy(deep=True)
            current = events[-1].intent
            if current.actor_id != actor_id:
                raise AuthoringIntentStoreError("AuthoringIntent belongs to another actor")
            updated = transform(current.model_copy(deep=True))
            self._validate_transition(current, updated, allow_rebase=allow_rebase)
            event = build_authoring_intent_event(
                sequence=len(events),
                previous_event_digest=events[-1].event_digest,
                operation_key=operation_key,
                intent=updated,
                program_stamp=program_stamp,
            )
            path = directory / "events" / f"{event.sequence:020d}.json"
            _exclusive_write(path, self._render_event(event))
            self._crash("after_transition_event_sync")
            return updated

    def record_program_stamp(
        self,
        intent_id: str,
        *,
        actor_id: str,
        program_stamp: AuthoringProgramStampV1,
    ) -> AuthoringIntentV1:
        current = self.get(intent_id, actor_id=actor_id)
        operation_key = authoring_program_stamp_operation_key(
            intent_id=intent_id,
            intent_revision=current.intent_revision,
            program_stamp=program_stamp,
        )
        return self.transition(
            intent_id,
            actor_id=actor_id,
            operation_key=operation_key,
            transform=lambda intent: intent,
            program_stamp=program_stamp,
        )

    def _active_by_fingerprint(
        self,
        fingerprint: str,
        *,
        actor_id: str,
    ) -> AuthoringIntentV1 | None:
        matches: list[AuthoringIntentV1] = []
        for directory in self._intent_directories():
            intent = self._validated_events(directory)[-1].intent
            if (
                intent.actor_id == actor_id
                and intent.create_fingerprint == fingerprint
                and _intent_is_pending(intent)
            ):
                matches.append(intent)
        if len(matches) > 1:
            raise AuthoringIntentStoreError("active AuthoringIntent fingerprint is not unique")
        return None if not matches else matches[0].model_copy(deep=True)

    def _intent_directories(self) -> tuple[Path, ...]:
        return tuple(
            path
            for path in sorted(self.root.glob("AIT-*"), key=lambda item: item.name)
            if path.is_dir() and not path.is_symlink()
        )

    def _load_events(self, directory: Path) -> tuple[AuthoringIntentEventAny, ...]:
        """Caller-owned full history; narrow reads avoid copying old snapshots."""

        return tuple(event.model_copy(deep=True) for event in self._validated_events(directory))

    def _validated_events(self, directory: Path) -> tuple[AuthoringIntentEventAny, ...]:
        """Private parsed events, reused only after checking exact persisted bytes.

        Every invocation still checks paths, content hashes and stream links.
        Only JSON/model/canonical validation of identical bytes is elided. A
        valid shorter prefix retains the cold loader's semantics; no cached
        suffix is resurrected. Cache publication happens after complete success.
        """
        if directory.is_symlink() or not directory.is_dir():
            raise AuthoringIntentStoreError("AuthoringIntent does not exist")
        events_directory = directory / "events"
        if events_directory.is_symlink() or not events_directory.is_dir():
            raise AuthoringIntentStoreError("AuthoringIntent event directory is invalid")
        paths = tuple(sorted(events_directory.glob("*.json"), key=lambda item: item.name))
        if not paths:
            raise AuthoringIntentStoreError("AuthoringIntent event stream is empty")
        cached = _history_memo_get(directory)
        validated: list[_ValidatedEvent] = []
        # Most transitions repeat a large immutable payload. Share it only
        # inside private validated state and only under its verified commitment.
        # Outward copies remain independent, including the transform input.
        # The commitment is the CREATE FINGERPRINT, never the payload digest: a
        # change set's payload digest deliberately drops its rationale, so two
        # revisions that differ only in prose share one payload digest and are
        # not one payload. The fingerprint digests the whole payload, so equal
        # fingerprints on one reproduced event mean equal payloads.
        payloads: dict[tuple[type[object], str], AuthoringPayloadV1] = {
            (
                type(item.event.intent.payload),
                item.event.intent.create_fingerprint,
            ): item.event.intent.payload
            for item in cached
        }
        events: list[AuthoringIntentEventAny] = []
        previous: str | None = None
        operation_keys: set[str] = set()
        for sequence, path in enumerate(paths):
            if path.name != f"{sequence:020d}.json" or path.is_symlink() or not path.is_file():
                raise AuthoringIntentStoreError("AuthoringIntent event sequence is not contiguous")
            try:
                raw = path.read_bytes()
                raw_digest = hashlib.sha256(raw).digest()
                old = cached[sequence] if sequence < len(cached) else None
                if old is not None and old.raw_size == len(raw) and old.raw_digest == raw_digest:
                    validated_event = old
                else:
                    event, rendered = _decode_authoring_intent_event(raw)
                    if raw != rendered:
                        raise AuthoringIntentStoreError("AuthoringIntent event is not canonical")
                    key = (type(event.intent.payload), event.intent.create_fingerprint)
                    payload = payloads.setdefault(key, event.intent.payload)
                    if payload is not event.intent.payload:
                        event = event.model_copy(
                            update={"intent": event.intent.model_copy(update={"payload": payload})}
                        )
                    validated_event = _ValidatedEvent(raw_digest, len(raw), event)
                event = validated_event.event
            except (OSError, ValidationError, ValueError) as exc:
                raise AuthoringIntentStoreError("AuthoringIntent event is malformed") from exc
            if event.sequence != sequence or event.previous_event_digest != previous:
                raise AuthoringIntentStoreError("AuthoringIntent event chain is broken")
            if event.operation_key in operation_keys:
                raise AuthoringIntentStoreError("AuthoringIntent operation key was replayed")
            if event.intent.intent_id != directory.name:
                raise AuthoringIntentStoreError("AuthoringIntent event names another stream")
            events.append(event)
            validated.append(validated_event)
            operation_keys.add(event.operation_key)
            previous = event.event_digest
        _history_memo_put(directory, tuple(validated))
        return tuple(events)

    @staticmethod
    def _render_event(event: AuthoringIntentEventAny) -> bytes:
        return canonical_bytes(event.model_dump(mode="json")) + b"\n"

    @staticmethod
    def _validate_transition(
        current: AuthoringIntentV1,
        updated: AuthoringIntentV1,
        *,
        allow_rebase: bool = False,
    ) -> None:
        immutable = (
            "intent_id",
            "instance_id",
            "actor_id",
            "canonical_timestamp",
        )
        if any(getattr(current, name) != getattr(updated, name) for name in immutable):
            raise AuthoringIntentStoreError("AuthoringIntent transition changed immutable identity")
        if current.base_coordinate != updated.base_coordinate:
            if not allow_rebase:
                raise AuthoringIntentStoreError(
                    "AuthoringIntent transition changed immutable base coordinate"
                )
            if (
                current.semantic_identity != updated.semantic_identity
                or current.payload != updated.payload
                or current.payload_digest != updated.payload_digest
                or current.create_fingerprint != updated.create_fingerprint
                or current.insertion_expectation != updated.insertion_expectation
                or current.insertion_expectations != updated.insertion_expectations
                or current.change_set_claim_identities != updated.change_set_claim_identities
                or updated.intent_revision != current.intent_revision + 1
                or updated.last_preflight is not None
                or updated.candidate_status.state != "draft"
            ):
                raise AuthoringIntentStoreError(
                    "AuthoringIntent rebase changed more than its checked protocol fields"
                )
        if updated.intent_revision < current.intent_revision:
            raise AuthoringIntentStoreError("AuthoringIntent revision regressed")


__all__ = [
    "AUTHORING_INTENT_EVENT_DIGEST_DOMAIN",
    "AUTHORING_INTENT_EVENT_V2_DIGEST_DOMAIN",
    "AUTHORING_INTENT_EVENT_V3_DIGEST_DOMAIN",
    "AuthoringIntentEventAny",
    "AuthoringIntentEventV1",
    "AuthoringIntentEventV2",
    "AuthoringIntentEventV3",
    "AuthoringIntentStore",
    "AuthoringIntentStoreError",
    "authoring_intent_event_digest",
    "build_authoring_intent_event",
]
