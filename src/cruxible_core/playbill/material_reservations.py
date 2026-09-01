"""Durable non-CAS leases closing body-store to journal-append reachability gaps."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_client.contracts.errors import PlaybillExecutionError
from cruxible_core.playbill.cas import BodyAccessContext, ContentAddressedBodyStore
from cruxible_core.playbill.exhaust.records import (
    JournalEventKindV1,
    StoredProcedureJournalRecordV1,
    parse_journal_payload,
)

PENDING_RESERVATION_DOMAIN = "playbill-pending-admission-material-reservation-v1"
RUN_RESERVATION_DOMAIN = "playbill-run-material-reservation-v1"
RUN_RESERVATION_V2_DOMAIN = "playbill-run-material-reservation-v2"
RUN_MATERIAL_INVOCATION_DOMAIN = "playbill-run-material-invocation-v1"
_STORE_MARKER_NAME = ".procedure-material-store-v1"
_STORE_MARKER_BYTES = b"playbill-procedure-material-store-v1\n"


class ProcedureMaterialReservationError(PlaybillExecutionError):
    """Reservation storage is missing, corrupt, or inconsistent."""


class ProcedureMaterialRecoveryRequired(ProcedureMaterialReservationError):
    code = "run_recovery_required"


class _StrictReservationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _sha256(value: str | None) -> str | None:
    if value is not None:
        Sha256Value.from_tagged(value)
    return value


class PendingAdmissionMaterialReservationV1(_StrictReservationModel):
    tag: Literal["playbill-pending-admission-material-reservation-v1"] = (
        "playbill-pending-admission-material-reservation-v1"
    )
    reservation_id: str
    instance_id: str
    run_id: str
    admission_binding_digest: str
    input_name: str
    plane: Literal["landed_capture", "exhaust"]
    body_digest: str
    intended_event_kind: Literal["admission_bound"] = "admission_bound"

    _digests = field_validator("reservation_id", "admission_binding_digest", "body_digest")(_sha256)

    @model_validator(mode="after")
    def _identity(self) -> "PendingAdmissionMaterialReservationV1":
        if self.reservation_id != pending_reservation_id(self):
            raise ValueError("pending-admission reservation id does not reproduce")
        return self


class RunMaterialReservationV1(_StrictReservationModel):
    tag: Literal["playbill-run-material-reservation-v1"] = "playbill-run-material-reservation-v1"
    reservation_id: str
    instance_id: str
    run_id: str | None
    admission_binding_digest: str | None
    invocation_id: str
    body_digest: str
    intended_event_kind: JournalEventKindV1

    _digests = field_validator(
        "reservation_id",
        "admission_binding_digest",
        "invocation_id",
        "body_digest",
    )(_sha256)

    @model_validator(mode="after")
    def _identity(self) -> "RunMaterialReservationV1":
        if self.reservation_id != run_reservation_id(self):
            raise ValueError("run-material reservation id does not reproduce")
        return self


class RunMaterialReservationV2(_StrictReservationModel):
    """Runtime lease keyed by the actual Provider invocation identity."""

    tag: Literal["playbill-run-material-reservation-v2"] = "playbill-run-material-reservation-v2"
    reservation_id: str
    instance_id: str
    run_id: str
    admission_binding_digest: str
    journal_partition_id: str
    invocation_id: str
    body_digest: str
    intended_event_kind: JournalEventKindV1

    _digests = field_validator(
        "reservation_id", "admission_binding_digest", "invocation_id", "body_digest"
    )(_sha256)

    @model_validator(mode="after")
    def _identity(self) -> RunMaterialReservationV2:
        if self.reservation_id != run_reservation_v2_id(self):
            raise ValueError("run-material v2 reservation id does not reproduce")
        return self


MaterialReservationV1 = (
    PendingAdmissionMaterialReservationV1 | RunMaterialReservationV1 | RunMaterialReservationV2
)


def _reservation_preimage(record: MaterialReservationV1) -> dict[str, object]:
    payload = record.model_dump(mode="json")
    payload.pop("tag")
    payload.pop("reservation_id")
    return payload


def pending_reservation_id(record: PendingAdmissionMaterialReservationV1) -> str:
    return typed_digest(
        Sha256Value,
        PENDING_RESERVATION_DOMAIN,
        _reservation_preimage(record),
    ).tagged


def run_reservation_id(record: RunMaterialReservationV1) -> str:
    return typed_digest(
        Sha256Value,
        RUN_RESERVATION_DOMAIN,
        _reservation_preimage(record),
    ).tagged


def run_reservation_v2_id(record: RunMaterialReservationV2) -> str:
    return typed_digest(
        Sha256Value,
        RUN_RESERVATION_V2_DOMAIN,
        _reservation_preimage(record),
    ).tagged


def make_pending_reservation(
    *,
    instance_id: str,
    run_id: str,
    admission_binding_digest: str,
    input_name: str,
    plane: Literal["landed_capture", "exhaust"],
    body_digest: str,
) -> PendingAdmissionMaterialReservationV1:
    provisional = PendingAdmissionMaterialReservationV1.model_construct(
        reservation_id="sha256:" + "0" * 64,
        instance_id=instance_id,
        run_id=run_id,
        admission_binding_digest=admission_binding_digest,
        input_name=input_name,
        plane=plane,
        body_digest=body_digest,
        intended_event_kind="admission_bound",
    )
    return PendingAdmissionMaterialReservationV1.model_validate(
        {
            **provisional.model_dump(mode="json"),
            "reservation_id": pending_reservation_id(provisional),
        }
    )


def run_material_invocation_id(
    *,
    instance_id: str,
    partition_id: str,
    event_kind: JournalEventKindV1,
    run_id: str | None,
    admission_binding_digest: str | None,
    body_digest: str,
) -> str:
    return typed_digest(
        Sha256Value,
        RUN_MATERIAL_INVOCATION_DOMAIN,
        {
            "instance_id": instance_id,
            "partition_id": partition_id,
            "event_kind": event_kind,
            "run_id": run_id,
            "admission_binding_digest": admission_binding_digest,
            "body_digest": body_digest,
        },
    ).tagged


def make_run_reservation(
    *,
    instance_id: str,
    partition_id: str,
    event_kind: JournalEventKindV1,
    run_id: str | None,
    admission_binding_digest: str | None,
    body_digest: str,
) -> RunMaterialReservationV1:
    invocation_id = run_material_invocation_id(
        instance_id=instance_id,
        partition_id=partition_id,
        event_kind=event_kind,
        run_id=run_id,
        admission_binding_digest=admission_binding_digest,
        body_digest=body_digest,
    )
    provisional = RunMaterialReservationV1.model_construct(
        reservation_id="sha256:" + "0" * 64,
        instance_id=instance_id,
        run_id=run_id,
        admission_binding_digest=admission_binding_digest,
        invocation_id=invocation_id,
        body_digest=body_digest,
        intended_event_kind=event_kind,
    )
    return RunMaterialReservationV1.model_validate(
        {
            **provisional.model_dump(mode="json"),
            "reservation_id": run_reservation_id(provisional),
        }
    )


def make_run_reservation_v2(
    *,
    instance_id: str,
    run_id: str,
    admission_binding_digest: str,
    partition_id: str,
    invocation_id: str,
    body_digest: str,
    event_kind: JournalEventKindV1,
) -> RunMaterialReservationV2:
    provisional = RunMaterialReservationV2.model_construct(
        reservation_id="sha256:" + "0" * 64,
        instance_id=instance_id,
        run_id=run_id,
        admission_binding_digest=admission_binding_digest,
        journal_partition_id=partition_id,
        invocation_id=invocation_id,
        body_digest=body_digest,
        intended_event_kind=event_kind,
    )
    return RunMaterialReservationV2.model_validate(
        {
            **provisional.model_dump(mode="json"),
            "reservation_id": run_reservation_v2_id(provisional),
        }
    )


def reserve_admission_material_body(
    *,
    bodies: ContentAddressedBodyStore,
    instance_id: str,
    run_id: str,
    admission_binding_digest: str,
    input_name: str,
    plane: Literal["landed_capture", "exhaust"],
    content: bytes,
) -> PendingAdmissionMaterialReservationV1:
    """Reserve before CAS visibility and leave the lease for admission-bound promotion."""

    body_digest = bodies.digest_bytes(content).tagged
    reservation = make_pending_reservation(
        instance_id=instance_id,
        run_id=run_id,
        admission_binding_digest=admission_binding_digest,
        input_name=input_name,
        plane=plane,
        body_digest=body_digest,
    )
    store = ProcedureMaterialReservationStore(bodies.reservation_root)
    with store.locked():
        store.reserve_locked(reservation)
        metadata = bodies.store(content)
        if metadata.digest != reservation.body_digest:
            raise ProcedureMaterialReservationError(
                "CAS store did not reproduce its pending material reservation"
            )
    return reservation


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class ProcedureMaterialReservationStore:
    """Canonical sidecars and their process-shared reachability lock."""

    def __init__(self, root: Path) -> None:
        parent = root.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if parent.is_symlink() or not parent.is_dir():
            raise ProcedureMaterialReservationError("reservation parent is not trustworthy")
        marker = parent / _STORE_MARKER_NAME
        if marker.exists() or marker.is_symlink():
            if marker.is_symlink() or not marker.is_file():
                raise ProcedureMaterialRecoveryRequired(
                    "run_recovery_required: reservation store marker is corrupt"
                )
            try:
                marker_bytes = marker.read_bytes()
            except OSError as exc:
                raise ProcedureMaterialRecoveryRequired(
                    "run_recovery_required: reservation store marker is unreadable"
                ) from exc
            if marker_bytes != _STORE_MARKER_BYTES:
                raise ProcedureMaterialRecoveryRequired(
                    "run_recovery_required: reservation store marker is corrupt"
                )
            if not root.exists() or root.is_symlink() or not root.is_dir():
                raise ProcedureMaterialRecoveryRequired(
                    "run_recovery_required: reservation root is missing or corrupt"
                )
        else:
            try:
                root.mkdir(mode=0o700, exist_ok=True)
            except OSError as exc:
                raise ProcedureMaterialReservationError(
                    "reservation root cannot be initialized"
                ) from exc
            if root.is_symlink() or not root.is_dir():
                raise ProcedureMaterialReservationError("reservation root is not trustworthy")
            descriptor, temp_name = tempfile.mkstemp(prefix=".store-marker-", dir=parent)
            temp = Path(temp_name)
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb", closefd=True) as handle:
                    handle.write(_STORE_MARKER_BYTES)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, marker)
                _fsync_directory(parent)
            finally:
                if temp.exists():
                    temp.unlink()
        os.chmod(root, 0o700)
        self.root = root.resolve(strict=True)
        self._marker_path = self.root.parent / marker.name
        self._lock_path = self.root / "reachability.lock"
        try:
            descriptor = os.open(
                self._lock_path,
                os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise ProcedureMaterialReservationError(
                        "reservation reachability lock is not a regular file"
                    )
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise ProcedureMaterialReservationError(
                "reservation reachability lock is not trustworthy"
            ) from exc
        _fsync_directory(self.root)

    @contextmanager
    def locked(self) -> Iterator[None]:
        if (
            not self.root.is_dir()
            or self.root.is_symlink()
            or not self._marker_path.is_file()
            or self._marker_path.is_symlink()
        ):
            raise ProcedureMaterialRecoveryRequired(
                "run_recovery_required: reservation root is missing or corrupt"
            )
        try:
            if self._marker_path.read_bytes() != _STORE_MARKER_BYTES:
                raise ProcedureMaterialRecoveryRequired(
                    "run_recovery_required: reservation store marker is corrupt"
                )
            descriptor = os.open(
                self._lock_path,
                os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                raise ProcedureMaterialRecoveryRequired(
                    "run_recovery_required: reservation reachability lock is corrupt"
                )
        except ProcedureMaterialRecoveryRequired:
            raise
        except OSError as exc:
            raise ProcedureMaterialRecoveryRequired(
                "run_recovery_required: reservation reachability lock is missing or corrupt"
            ) from exc
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _path(self, reservation_id: str) -> Path:
        digest = Sha256Value.from_tagged(reservation_id)
        return self.root / f"{digest.value}.json"

    def reserve_locked(self, record: MaterialReservationV1) -> None:
        path = self._path(record.reservation_id)
        content = canonical_bytes(record.model_dump(mode="json")) + b"\n"
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not path.is_file() or path.read_bytes() != content:
                raise ProcedureMaterialReservationError(
                    "reservation identity collides with different bytes"
                )
            return
        descriptor, temp_name = tempfile.mkstemp(prefix=".reservation-", dir=self.root)
        temp = Path(temp_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            _fsync_directory(self.root)
        finally:
            if temp.exists():
                temp.unlink()

    def reserve(self, record: MaterialReservationV1) -> None:
        with self.locked():
            self.reserve_locked(record)

    def release_locked(self, reservation_id: str) -> None:
        path = self._path(reservation_id)
        if not path.exists() and not path.is_symlink():
            return
        if path.is_symlink() or not path.is_file():
            raise ProcedureMaterialReservationError("reservation file is not trustworthy")
        path.unlink()
        _fsync_directory(self.root)

    def release(self, reservation_id: str) -> None:
        with self.locked():
            self.release_locked(reservation_id)

    def active_locked(self) -> tuple[MaterialReservationV1, ...]:
        records: list[MaterialReservationV1] = []
        for path in sorted(self.root.glob("*.json"), key=lambda item: item.name):
            try:
                if path.is_symlink() or not path.is_file():
                    raise ValueError("reservation path is not a regular file")
                raw = path.read_bytes()
                payload = json.loads(raw)
                tag = payload.get("tag") if isinstance(payload, dict) else None
                model = (
                    PendingAdmissionMaterialReservationV1
                    if tag == "playbill-pending-admission-material-reservation-v1"
                    else RunMaterialReservationV1
                    if tag == "playbill-run-material-reservation-v1"
                    else RunMaterialReservationV2
                    if tag == "playbill-run-material-reservation-v2"
                    else None
                )
                if model is None:
                    raise ValueError("unknown reservation tag")
                record = model.model_validate(payload)
                expected = canonical_bytes(record.model_dump(mode="json")) + b"\n"
                if raw != expected or path != self._path(record.reservation_id):
                    raise ValueError("reservation bytes/path are not canonical")
            except Exception as exc:
                raise ProcedureMaterialRecoveryRequired(
                    "run_recovery_required: reservation sidecar is corrupt"
                ) from exc
            records.append(record)
        return tuple(records)

    def active(self) -> tuple[MaterialReservationV1, ...]:
        with self.locked():
            return self.active_locked()

    def recover(
        self,
        records: Sequence[StoredProcedureJournalRecordV1],
        *,
        bodies: ContentAddressedBodyStore,
    ) -> tuple[str, ...]:
        """Release leases after an authenticated, complete journal scan.

        ``records`` must contain the complete authenticated scan for this instance;
        a partial scan cannot prove that a pre-append reservation is unreferenced.
        """

        return self._recover(records, bodies=bodies, include_pending_admission=True)

    def recover_run_material(
        self,
        records: Sequence[StoredProcedureJournalRecordV1],
        *,
        bodies: ContentAddressedBodyStore,
    ) -> tuple[str, ...]:
        """Recover append-window leases without racing pending Line admission material."""

        return self._recover(records, bodies=bodies, include_pending_admission=False)

    def _recover(
        self,
        records: Sequence[StoredProcedureJournalRecordV1],
        *,
        bodies: ContentAddressedBodyStore,
        include_pending_admission: bool,
    ) -> tuple[str, ...]:

        released: list[str] = []
        access = BodyAccessContext(principal_id="procedure-material-recovery", can_read_body=True)
        with self.locked():
            for reservation in self.active_locked():
                if (
                    isinstance(reservation, PendingAdmissionMaterialReservationV1)
                    and not include_pending_admission
                ):
                    continue
                matches: list[StoredProcedureJournalRecordV1] = []
                for stored in records:
                    record = stored.record
                    if record.stream.instance_id != reservation.instance_id:
                        continue
                    if record.event_kind != reservation.intended_event_kind:
                        continue
                    if record.run_id != reservation.run_id:
                        continue
                    if record.admission_binding_digest != reservation.admission_binding_digest:
                        continue
                    if isinstance(reservation, RunMaterialReservationV2):
                        if record.partition_id != reservation.journal_partition_id:
                            continue
                        try:
                            payload = parse_journal_payload(
                                bodies.read(record.payload_digest, access=access)
                            )
                        except Exception as exc:
                            raise ProcedureMaterialRecoveryRequired(
                                "run_recovery_required: runtime material reference cannot "
                                "be authenticated"
                            ) from exc
                        if (
                            record.payload_digest == reservation.body_digest
                            and isinstance(payload, dict)
                            and payload.get("invocation_id") == reservation.invocation_id
                        ):
                            matches.append(stored)
                        continue
                    if isinstance(reservation, RunMaterialReservationV1):
                        expected_invocation = run_material_invocation_id(
                            instance_id=record.stream.instance_id,
                            partition_id=record.partition_id,
                            event_kind=record.event_kind,
                            run_id=record.run_id,
                            admission_binding_digest=record.admission_binding_digest,
                            body_digest=record.payload_digest,
                        )
                        if (
                            record.payload_digest == reservation.body_digest
                            and reservation.invocation_id == expected_invocation
                        ):
                            matches.append(stored)
                        continue
                    try:
                        payload = parse_journal_payload(
                            bodies.read(record.payload_digest, access=access)
                        )
                    except Exception as exc:
                        raise ProcedureMaterialRecoveryRequired(
                            "run_recovery_required: admission reference cannot be authenticated"
                        ) from exc
                    members = _validated_admission_material_members(payload)
                    if any(
                        member.input_name == reservation.input_name
                        and member.plane == reservation.plane
                        and member.body_digest == reservation.body_digest
                        for member in members
                    ):
                        matches.append(stored)
                if len(matches) > 1:
                    raise ProcedureMaterialRecoveryRequired(
                        "run_recovery_required: reservation has multiple authenticated references"
                    )
                self.release_locked(reservation.reservation_id)
                released.append(reservation.reservation_id)
        return tuple(released)

    def reachable_body_digests(
        self,
        records: Sequence[StoredProcedureJournalRecordV1],
        *,
        bodies: ContentAddressedBodyStore,
    ) -> frozenset[str]:
        """Return active leases plus authenticated journal and manifest references."""

        access = BodyAccessContext(principal_id="procedure-material-gc", can_read_body=True)
        with self.locked():
            active = {item.body_digest for item in self.active_locked()}
            journal: set[str] = set()
            material: set[str] = set()
            for stored in records:
                record = stored.record
                journal.add(record.payload_digest)
                if record.event_kind != "admission_bound":
                    continue
                try:
                    payload = parse_journal_payload(
                        bodies.read(record.payload_digest, access=access)
                    )
                    members = _validated_admission_material_members(payload)
                except Exception as exc:
                    raise ProcedureMaterialRecoveryRequired(
                        "run_recovery_required: admission reachability cannot be authenticated"
                    ) from exc
                material.update(
                    member.body_digest for member in members if member.body_digest is not None
                )
            return frozenset(active | journal | material)


def _validated_admission_material_members(payload: object) -> tuple[Any, ...]:
    """Parse a V3 admission manifest without creating a module import cycle."""

    if not isinstance(payload, dict):
        raise ProcedureMaterialRecoveryRequired(
            "run_recovery_required: admission payload is not an object"
        )
    tag = payload.get("tag")
    if tag not in {
        "playbill-procedure-admission-bound-payload-v2",
        "playbill-procedure-admission-bound-payload-v3",
        "playbill-procedure-admission-bound-payload-v4",
        "playbill-procedure-admission-bound-payload-v5",
    }:
        raise ProcedureMaterialRecoveryRequired(
            "run_recovery_required: admission payload version is unsupported"
        )
    try:
        from cruxible_core.playbill.procedures.execution import (
            ProcedureAdmissionBoundPayloadV2,
            ProcedureAdmissionBoundPayloadV3,
            ProcedureAdmissionBoundPayloadV4,
            ProcedureAdmissionBoundPayloadV5,
        )

        if tag == "playbill-procedure-admission-bound-payload-v2":
            ProcedureAdmissionBoundPayloadV2.model_validate(payload)
            return ()
        bound = (
            ProcedureAdmissionBoundPayloadV5.model_validate(payload)
            if tag == "playbill-procedure-admission-bound-payload-v5"
            else ProcedureAdmissionBoundPayloadV4.model_validate(payload)
            if tag == "playbill-procedure-admission-bound-payload-v4"
            else ProcedureAdmissionBoundPayloadV3.model_validate(payload)
        )
    except Exception as exc:
        raise ProcedureMaterialRecoveryRequired(
            "run_recovery_required: admission material manifest is corrupt"
        ) from exc
    return tuple(bound.admission_material_manifest.members)


__all__ = [
    "PendingAdmissionMaterialReservationV1",
    "ProcedureMaterialRecoveryRequired",
    "ProcedureMaterialReservationError",
    "ProcedureMaterialReservationStore",
    "RunMaterialReservationV1",
    "RunMaterialReservationV2",
    "make_pending_reservation",
    "make_run_reservation",
    "make_run_reservation_v2",
    "pending_reservation_id",
    "run_material_invocation_id",
    "run_reservation_id",
    "run_reservation_v2_id",
    "reserve_admission_material_body",
]
