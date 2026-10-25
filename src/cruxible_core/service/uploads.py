"""Capture upload sessions: parts staged under a daemon-owned root, read once, deleted.

Layout, under the operator's ``upload_root`` (outside the state root)::

    <upload_root>/<instance_id>/<upload_id>/session.json   the session (0600)
    <upload_root>/<instance_id>/<upload_id>/parts/<n>      each part as sent (0600)
    <upload_root>/<instance_id>/<upload_id>/uploads/<name> the assembled file, only
                                                           while its run executes

Every directory is created by the daemon (0700) and checked to be a real
directory, never a link; every file is opened with ``O_NOFOLLOW`` and created
with ``O_EXCL``. A session is bound to the principal that opened it: another
principal's upload id reads as not found.

One lock file per instance (``<upload_root>/<instance_id>/.lock``, an exclusive
``flock``) serializes every lifecycle decision across stores, threads and
processes: admission (sweep, count against the open-session cap, create and
publish the session record), the completion claim, and expiry removal. A
session is therefore never seen half-initialized, and the cap holds under
concurrent opens.

Completing claims the session under that lock by creating its ``completing``
lease with ``O_EXCL`` and holding an exclusive ``flock`` on it for the whole
run. Expiry never removes a session whose lease is held: an idle session
expires, a live completion keeps its staged file until its own exit removes the
whole session directory (however the run exits), and a lease left by a crashed
process (no holder) is recovered by the next sweep or claim. Expired sessions of
the instance are swept when the next one opens: the sweep reads only this
instance's session directories, never the instance's state.

No message here carries uploaded bytes: refusals name lengths, part numbers and
digests only.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import re
import secrets
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, ValidationError

from cruxible_client.contracts.artifacts import ArtifactPin
from cruxible_client.contracts.captures import capture_retention_policy
from cruxible_client.contracts.procedures.models import SourceNode
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.uploads import (
    UPLOAD_PART_SIZE,
    CaptureUploadOpenRequest,
    CaptureUploadSession,
    upload_id,
    upload_part_count,
)
from cruxible_client.contracts.workspace_file import WORKSPACE_FILE_INTERFACE_DIGESTS
from cruxible_core.service.procedures.procedure_runs import ProcedureSurfaceError

if TYPE_CHECKING:
    from cruxible_core.runtime.instance import PlaybillInstance

#: The staging directory's name: the logical workspace an acquire Procedure's
#: ``workspace.file`` read resolves (named or unnamed) during an upload's run.
UPLOAD_WORKSPACE_NAME = "uploads"
_SESSION_FILE = "session.json"
_PARTS = "parts"
_COMPLETING = "completing"
_LOCK_FILE = ".lock"
_INSTANCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_FILE_FLAGS = os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


class CaptureUploadRefused(ProcedureSurfaceError):
    """A Capture upload refusal the caller can repair; never carries uploaded bytes."""

    code = "cruxible.upload.refused"

    def __init__(self, reason: str, message: str, *, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = f"cruxible.upload.{reason}"
        self.error_code = self.code
        self.http_status = http_status


def _not_found() -> CaptureUploadRefused:
    return CaptureUploadRefused(
        "not_found",
        "No open upload has that id for you: it was completed, it expired, or another "
        "principal opened it. Open a new upload.",
        http_status=404,
    )


@dataclass(frozen=True)
class UploadLimits:
    max_bytes: int
    session_seconds: float
    max_open_sessions: int


class CaptureUploadSessionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    upload_id: str
    principal_id: str
    filename: str
    byte_length: int
    sha256: str
    part_count: int
    created_at: datetime
    expires_at: datetime


def _real_directory(path: Path) -> bool:
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(status.st_mode):
        raise CaptureUploadRefused(
            "staging_unavailable",
            "The upload staging area holds something other than a directory; the "
            "operator must clear it.",
            http_status=500,
        )
    return True


def _make_directory(path: Path) -> None:
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    if not _real_directory(path):
        raise CaptureUploadRefused(
            "staging_unavailable", "The upload staging area cannot be created.", http_status=500
        )


def _write_new(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE_FLAGS, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)


def _read(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | _FILE_FLAGS)
    try:
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read()
    finally:
        os.close(descriptor)


def _remove_tree(path: Path) -> None:
    # rmtree never follows a link out of the tree it removes.
    shutil.rmtree(path, ignore_errors=True)


def _completion_live(directory: Path) -> bool:
    """Whether a process holds this session's completion lease right now."""

    try:
        descriptor = os.open(directory / _COMPLETING, os.O_RDONLY | _FILE_FLAGS)
    except OSError:
        return False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(descriptor)
    return False


class CaptureUploadStore:
    """One instance's upload sessions under the daemon's upload root."""

    def __init__(self, root: Path, instance_id: str, limits: UploadLimits) -> None:
        if not _INSTANCE_ID_RE.fullmatch(instance_id):
            raise CaptureUploadRefused(
                "staging_unavailable", "This instance id cannot name a staging directory."
            )
        self.root = root
        self.instance_root = root / instance_id
        self.limits = limits

    # -- sessions ---------------------------------------------------------------

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """The instance's upload lock: every lifecycle decision is made under it."""

        _make_directory(self.root)
        _make_directory(self.instance_root)
        descriptor = os.open(
            self.instance_root / _LOCK_FILE, os.O_RDWR | os.O_CREAT | _FILE_FLAGS, 0o600
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _session_dir(self, value: str) -> Path:
        try:
            upload_id(value)
        except ValueError:
            raise _not_found() from None
        return self.instance_root / value

    def _record(self, directory: Path) -> CaptureUploadSessionRecord | None:
        try:
            if not _real_directory(directory):
                return None
            return CaptureUploadSessionRecord.model_validate_json(_read(directory / _SESSION_FILE))
        except (OSError, ValidationError):
            return None

    def _owned(
        self, principal_id: str, value: str, now: datetime, *, held: bool = False
    ) -> tuple[Path, CaptureUploadSessionRecord]:
        directory = self._session_dir(value)
        record = self._record(directory)
        if record is None or record.principal_id != principal_id or record.upload_id != value:
            raise _not_found()
        if record.expires_at <= now:
            if held:
                self._expire(directory, record)
            else:
                with self._locked():
                    self._expire(directory, record)
        return directory, record

    def _expire(self, directory: Path, record: CaptureUploadSessionRecord) -> None:
        """Remove an expired session, unless its completion is running (lock held)."""

        if _completion_live(directory):
            raise CaptureUploadRefused(
                "in_progress",
                f"Upload {record.upload_id} is completing past its expiry; wait for its result.",
                http_status=409,
            )
        _remove_tree(directory)
        raise CaptureUploadRefused(
            "expired",
            f"Upload {record.upload_id} expired at {record.expires_at.isoformat()}; "
            "open a new upload.",
            http_status=410,
        )

    def _received(self, directory: Path, record: CaptureUploadSessionRecord) -> tuple[int, ...]:
        parts = directory / _PARTS
        held = []
        for part in range(record.part_count):
            try:
                if stat.S_ISREG(os.lstat(parts / str(part)).st_mode):
                    held.append(part)
            except FileNotFoundError:
                continue
        return tuple(held)

    def _view(self, directory: Path, record: CaptureUploadSessionRecord) -> CaptureUploadSession:
        return CaptureUploadSession(
            upload_id=record.upload_id,
            filename=record.filename,
            byte_length=record.byte_length,
            sha256=record.sha256,
            part_size=UPLOAD_PART_SIZE,
            part_count=record.part_count,
            parts_received=self._received(directory, record),
            expires_at=record.expires_at,
        )

    def _sweep(self, now: datetime) -> int:
        """Remove this instance's expired or unreadable sessions; count the open ones.

        Runs under the instance lock, so no session is mid-initialization. A
        session whose completion lease is held is live whatever its expiry.
        """

        open_sessions = 0
        try:
            entries = os.listdir(self.instance_root)
        except FileNotFoundError:
            return 0
        for name in entries:
            if name == _LOCK_FILE:
                continue
            directory = self.instance_root / name
            try:
                is_directory = stat.S_ISDIR(os.lstat(directory).st_mode)
            except FileNotFoundError:
                continue
            if not is_directory:
                # Nothing but session directories belongs here.
                os.unlink(directory)
                continue
            if _completion_live(directory):
                open_sessions += 1
                continue
            record = self._record(directory)
            if record is None or record.expires_at <= now:
                _remove_tree(directory)
                continue
            open_sessions += 1
        return open_sessions

    def open(
        self, *, principal_id: str, request: CaptureUploadOpenRequest, now: datetime
    ) -> CaptureUploadSession:
        if request.byte_length > self.limits.max_bytes:
            raise CaptureUploadRefused(
                "too_large",
                f"An upload carries at most {self.limits.max_bytes} bytes; this one declares "
                f"{request.byte_length}.",
                http_status=413,
            )
        with self._locked():
            if self._sweep(now) >= self.limits.max_open_sessions:
                raise CaptureUploadRefused(
                    "sessions_exhausted",
                    f"This instance already has {self.limits.max_open_sessions} open uploads; "
                    "complete one or let it expire.",
                    http_status=429,
                )
            return self._create(principal_id=principal_id, request=request, now=now)

    def _create(
        self, *, principal_id: str, request: CaptureUploadOpenRequest, now: datetime
    ) -> CaptureUploadSession:
        value = secrets.token_hex(16)
        directory = self.instance_root / value
        os.mkdir(directory, 0o700)
        os.mkdir(directory / _PARTS, 0o700)
        record = CaptureUploadSessionRecord(
            upload_id=value,
            principal_id=principal_id,
            filename=request.filename,
            byte_length=request.byte_length,
            sha256=request.sha256,
            part_count=upload_part_count(request.byte_length),
            created_at=now,
            expires_at=now + timedelta(seconds=self.limits.session_seconds),
        )
        _write_new(directory / _SESSION_FILE, record.model_dump_json().encode("utf-8"))
        return self._view(directory, record)

    def session(self, *, principal_id: str, upload: str, now: datetime) -> CaptureUploadSession:
        directory, record = self._owned(principal_id, upload, now)
        return self._view(directory, record)

    def put_part(
        self, *, principal_id: str, upload: str, part: int, content: bytes, now: datetime
    ) -> CaptureUploadSession:
        directory, record = self._owned(principal_id, upload, now)
        if (directory / _COMPLETING).exists():
            raise CaptureUploadRefused(
                "in_progress",
                f"Upload {upload} is completing; it takes no more parts.",
                http_status=409,
            )
        if not 0 <= part < record.part_count:
            raise CaptureUploadRefused(
                "part_out_of_range",
                f"Upload {upload} has parts 0 to {record.part_count - 1}; part {part} is not one.",
            )
        expected = (
            record.byte_length - UPLOAD_PART_SIZE * (record.part_count - 1)
            if part == record.part_count - 1
            else UPLOAD_PART_SIZE
        )
        if len(content) != expected:
            raise CaptureUploadRefused(
                "part_size_mismatch",
                f"Part {part} of upload {upload} is {expected} bytes; {len(content)} arrived.",
            )
        target = directory / _PARTS / str(part)
        staging = directory / _PARTS / f".{part}.{secrets.token_hex(8)}"
        try:
            _write_new(staging, content)
        except FileNotFoundError:
            # Completed or expired while this part was in flight.
            raise _not_found() from None
        try:
            os.link(staging, target, follow_symlinks=False)
        except FileExistsError:
            # A retried part is accepted only when it is the same bytes.
            same = hashlib.sha256(_read(target)).digest() == hashlib.sha256(content).digest()
            if not same:
                raise CaptureUploadRefused(
                    "part_conflict",
                    f"Part {part} of upload {upload} already arrived with other bytes.",
                    http_status=409,
                ) from None
        finally:
            os.unlink(staging)
        return self._view(directory, record)

    @contextmanager
    def staged(
        self, *, principal_id: str, upload: str, now: datetime
    ) -> Iterator[tuple[Path, CaptureUploadSession]]:
        """Claim, assemble and verify one upload; yield its staging root and session.

        The whole session (parts, staged file) is removed when the block exits,
        however it exits; a second completion of the same upload is refused.
        """

        with self._locked():
            directory, record = self._owned(principal_id, upload, now, held=True)
            view = self._view(directory, record)
            if view.missing_parts:
                shown = ", ".join(str(part) for part in view.missing_parts[:16])
                raise CaptureUploadRefused(
                    "incomplete",
                    f"Upload {upload} still lacks part(s) {shown}"
                    + (" and more" if len(view.missing_parts) > 16 else "")
                    + "; send them, then complete it.",
                    http_status=409,
                )
            lease = self._claim(directory, upload)
        try:
            staging_root = directory / UPLOAD_WORKSPACE_NAME
            os.mkdir(staging_root, 0o700)
            self._assemble(directory, record, staging_root / record.filename)
            yield staging_root, view
        finally:
            # The lease is held until the session is gone, so no sweep or
            # expiry can take the staged file from under the run.
            _remove_tree(directory)
            os.close(lease)

    def _claim(self, directory: Path, upload: str) -> int:
        """Take the completion lease (instance lock held); a crashed one is recovered."""

        lease_path = directory / _COMPLETING
        if lease_path.exists() or lease_path.is_symlink():
            if _completion_live(directory):
                raise CaptureUploadRefused(
                    "in_progress", f"Upload {upload} is already completing.", http_status=409
                )
            # A completion that died without cleaning up: start it over.
            _remove_tree(directory / UPLOAD_WORKSPACE_NAME)
            os.unlink(lease_path)
        lease = os.open(lease_path, os.O_RDWR | os.O_CREAT | os.O_EXCL | _FILE_FLAGS, 0o600)
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(lease)
            raise
        return lease

    def _assemble(self, directory: Path, record: CaptureUploadSessionRecord, target: Path) -> None:
        digest = hashlib.sha256()
        total = 0
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE_FLAGS, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                for part in range(record.part_count):
                    try:
                        content = _read(directory / _PARTS / str(part))
                    except OSError as exc:
                        if exc.errno in {errno.ELOOP, errno.ENOENT}:
                            raise CaptureUploadRefused(
                                "incomplete", f"Part {part} is no longer staged.", http_status=409
                            ) from None
                        raise
                    digest.update(content)
                    total += len(content)
                    handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(descriptor)
        if total != record.byte_length or f"sha256:{digest.hexdigest()}" != record.sha256:
            raise CaptureUploadRefused(
                "digest_mismatch",
                f"The {total} bytes received do not match the declared length and sha256; "
                "open a new upload and send the file again.",
            )


def check_upload_procedure(
    instance: PlaybillInstance, name: str, at: AcceptedCoordinate | None
) -> None:
    """Refuse, before anything is staged, a Procedure that cannot land an upload.

    It must read a ``workspace.file`` Source, and no Capture it lands that way
    may be ``never_materialize``: an import fires on the retained Capture body,
    so a contract that forbids keeping it could never import the upload.
    """

    from cruxible_core.service.procedures.procedure_runs import (
        _accepted_capture_contracts,
        _accepted_procedure,
        _resolve_coordinate,
    )

    coordinate = _resolve_coordinate(instance, at)
    accepted = _accepted_procedure(instance, name=name, coordinate=coordinate, bind_references=True)
    reads = tuple(
        node
        for node in accepted.procedure.definition.nodes
        if isinstance(node, SourceNode)
        and node.interface_digest in WORKSPACE_FILE_INTERFACE_DIGESTS
    )
    if not reads:
        raise CaptureUploadRefused(
            "procedure_reads_no_upload",
            f"Procedure {name} has no workspace.file Source to read the upload with; "
            "complete the upload with the Procedure that acquires this kind of file.",
        )
    pins = tuple(
        node.capture_contract for node in reads if isinstance(node.capture_contract, ArtifactPin)
    )
    contracts = _accepted_capture_contracts(instance, coordinate, pins)
    for pin in pins:
        contract = contracts.get(pin.artifact_digest)
        if (
            contract is not None
            and capture_retention_policy(contract).body_retention == "never_materialize"
        ):
            raise CaptureUploadRefused(
                "contract_not_materialized",
                f"Procedure {name} reads into CaptureContract {contract.identity.name}, which "
                "never keeps a body, so nothing could import the upload.",
            )


__all__ = [
    "UPLOAD_WORKSPACE_NAME",
    "CaptureUploadRefused",
    "CaptureUploadStore",
    "UploadLimits",
    "check_upload_procedure",
]
