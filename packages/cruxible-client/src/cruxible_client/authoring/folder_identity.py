"""The folder's default principal: who an agent started in this folder acts as.

`cruxible init` (and `cruxible workspace attach`, when it knows who acts)
records ``.cruxible/identity.json`` beside the folder's binding
(``.cruxible/coverage.json``). On a local machine the person and the agent
started in their project folder cannot be told apart, so the MCP server and the
CLI in that folder act as the principal the record names when nothing else
names one. Environment variables still win, so a team can give each agent
session its own principal.

The record names only a principal ID, the instance it belongs to and the
absolute path of that principal's key directory (outside the project). It never
holds a private key, a token or any other secret: committed by accident, it
reveals a principal ID, an instance ID and one local path. It is kept out of
Git by the same ``info/exclude`` rule as the binding.

It is read only at the workspace root the shared resolver chooses
(`cruxible_client.contracts.workspace_root`), never beyond it, and is used
only over a local daemon socket with daemon auth off. A record that cannot be
used (malformed, for another instance, a missing key directory, a key whose
public half the instance does not hold for that principal, a principal the
instance does not know) refuses with a typed error and its repair; it never
reads as "no principal".
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from cruxible_client._safe_files import read_regular_file
from cruxible_client.authoring.signing import _load_private_key, assert_outside_roots
from cruxible_client.contracts.errors import SigningKeyError
from cruxible_client.contracts.principals import is_canonical_principal_id
from cruxible_client.contracts.types import PrincipalRecord
from cruxible_client.contracts.workspace_layout import WORKSPACE_DIRECTORY, WorkspaceError

#: The record's path under the workspace root.
FOLDER_IDENTITY_PATH = f"{WORKSPACE_DIRECTORY}/identity.json"
FOLDER_IDENTITY_TAG: Literal["cruxible-folder-identity-v1"] = "cruxible-folder-identity-v1"
_MAX_RECORD_BYTES = 4096

FolderIdentityRefusalCode = Literal[
    "cruxible.identity.folder_record_invalid",
    "cruxible.identity.folder_key_unavailable",
    "cruxible.identity.folder_key_mismatch",
    "cruxible.identity.folder_principal_unknown",
]

#: Where the MCP server's (and the CLI's) identity came from.
IdentitySource = Literal["environment", "folder", "none"]


class FolderIdentity(BaseModel):
    """``.cruxible/identity.json``: the folder's default principal and its key directory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["cruxible-folder-identity-v1"] = FOLDER_IDENTITY_TAG
    instance_id: str
    principal_id: str
    #: Absolute path of the principal's key directory (``<principal_id>.ed25519``
    #: and ``cruxible.env``); never a key or a token.
    key_dir: str

    @field_validator("instance_id")
    @classmethod
    def _instance(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("instance_id must be nonempty")
        return value

    @field_validator("principal_id")
    @classmethod
    def _principal(cls, value: str) -> str:
        if not is_canonical_principal_id(value):
            raise ValueError("principal_id is not a canonical principal ID")
        return value

    @field_validator("key_dir")
    @classmethod
    def _key_dir(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("key_dir must be an absolute path")
        return value

    @property
    def private_key_path(self) -> Path:
        return Path(self.key_dir) / f"{self.principal_id}.ed25519"


class FolderIdentityRefused(WorkspaceError):
    """The folder's identity record cannot be used; the message leads with the code."""

    def __init__(self, code: FolderIdentityRefusalCode, *, path: Path, detail: str) -> None:
        self.error_code = code
        self.path = str(path)
        self.detail = detail
        super().__init__(
            f"{code}: {path}: {detail}. Repair: rerun `cruxible init` in this folder (or "
            "`cruxible workspace attach` as the principal it should act as), delete "
            f"{path} to act as no principal, or set CRUXIBLE_PRINCIPAL_ID, "
            "CRUXIBLE_PRINCIPAL_KEY and CRUXIBLE_MCP_KEY_DIR, which take precedence"
        )


def folder_identity_path(root: Path) -> Path:
    return root / FOLDER_IDENTITY_PATH


def _owner_uid() -> int | None:
    getuid = getattr(os, "getuid", None)
    return None if getuid is None else int(getuid())


def read_folder_identity(root: Path) -> FolderIdentity | None:
    """The record at ``root`` (a resolved workspace root), None when there is none.

    A symlink, a file another user owns, or content that is not exactly the
    record refuses (``cruxible.identity.folder_record_invalid``).
    """

    path = folder_identity_path(root)
    try:
        status = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise FolderIdentityRefused(
            "cruxible.identity.folder_record_invalid", path=path, detail=f"unreadable ({exc})"
        ) from exc
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISREG(status.st_mode):
        raise FolderIdentityRefused(
            "cruxible.identity.folder_record_invalid",
            path=path,
            detail="not a regular file (a symbolic link is never followed)",
        )
    owner = _owner_uid()
    if owner is not None and status.st_uid != owner:
        raise FolderIdentityRefused(
            "cruxible.identity.folder_record_invalid",
            path=path,
            detail=f"owned by another user (uid {status.st_uid}, not {owner})",
        )
    try:
        raw = read_regular_file(path, max_bytes=_MAX_RECORD_BYTES + 1)
        if len(raw) > _MAX_RECORD_BYTES:
            raise ValueError("larger than a record")
        payload = json.loads(raw.decode("utf-8"))
        return FolderIdentity.model_validate(payload)
    except (OSError, UnicodeDecodeError, ValueError, ValidationError) as exc:
        detail = (
            "; ".join(error["msg"] for error in exc.errors())
            if isinstance(exc, ValidationError)
            else str(exc)
        )
        raise FolderIdentityRefused(
            "cruxible.identity.folder_record_invalid",
            path=path,
            detail=f"not a folder identity record ({detail})",
        ) from exc


WriteStatus = Literal["written", "unchanged", "kept"]


def write_folder_identity(
    root: Path,
    *,
    instance_id: str,
    principal_id: str,
    key_dir: Path,
    replace: bool = False,
) -> tuple[Path, WriteStatus]:
    """Record the folder's default principal; never a key or a token.

    A record for another instance is stale and is replaced. One for this
    instance naming another principal is kept (``kept``) unless ``replace``:
    the folder's owner does not change behind its back.
    """

    from cruxible_client.authoring.workspace import (
        _ensure_workspace_config_ignored,
        _write_workspace_local,
    )

    path = folder_identity_path(root)
    desired = FolderIdentity(
        instance_id=instance_id,
        principal_id=principal_id,
        key_dir=str(key_dir.expanduser().resolve()),
    )
    try:
        existing = read_folder_identity(root)
    except FolderIdentityRefused:
        existing = None
    if existing == desired:
        return path, "unchanged"
    if (
        existing is not None
        and existing.instance_id == instance_id
        and existing.principal_id != principal_id
        and not replace
    ):
        return path, "kept"
    content = json.dumps(desired.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    _ensure_workspace_config_ignored(root)
    _write_workspace_local(root, FOLDER_IDENTITY_PATH, content.encode("utf-8"), durable=True)
    return path, "written"


def read_bound_folder_identity(root: Path, *, instance_id: str) -> FolderIdentity | None:
    """The record at a bound workspace ``root``; it must name the binding's instance."""

    record = read_folder_identity(root)
    if record is not None and record.instance_id != instance_id:
        raise FolderIdentityRefused(
            "cruxible.identity.folder_record_invalid",
            path=folder_identity_path(root),
            detail=(
                f"it names instance {record.instance_id}, but this folder's binding selects "
                f"{instance_id}"
            ),
        )
    return record


def folder_key_custody(identity: FolderIdentity, *, path: Path) -> tuple[int, ...]:
    """Check the key's current custody with one ``lstat`` each of it and its directory.

    Both must be real (no symbolic link), owned by this user and closed to group
    and world, and the key readable by this process. Cheap enough to run before
    every use; the returned stamp changes whenever the key file or its custody
    does, so a cached public-key match is never reused across such a change.
    """

    directory = Path(identity.key_dir)
    key = identity.private_key_path

    def refuse(detail: str) -> FolderIdentityRefused:
        return FolderIdentityRefused(
            "cruxible.identity.folder_key_unavailable", path=path, detail=detail
        )

    try:
        held = directory.lstat()
    except OSError:
        raise refuse(f"key directory {directory} is missing or not a real directory") from None
    if not stat.S_ISDIR(held.st_mode):
        raise refuse(f"key directory {directory} is missing or not a real directory")
    try:
        status = key.lstat()
    except OSError:
        raise refuse(f"key directory {directory} holds no {key.name}") from None
    if not stat.S_ISREG(status.st_mode):
        raise refuse(f"key directory {directory} holds no {key.name}")
    owner = _owner_uid()
    for item, item_status in ((directory, held), (key, status)):
        if owner is not None and item_status.st_uid != owner:
            raise refuse(f"{item} is owned by another user (uid {item_status.st_uid})")
        if stat.S_IMODE(item_status.st_mode) & 0o077:
            raise refuse(f"{item} permissions must exclude group/world access")
    if not os.access(key, os.R_OK):
        raise refuse(f"key {key} is not readable by this process")
    return (
        held.st_dev,
        held.st_ino,
        held.st_mode,
        held.st_uid,
        status.st_dev,
        status.st_ino,
        status.st_mode,
        status.st_uid,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def require_folder_key(identity: FolderIdentity, *, path: Path) -> Path:
    """The record's private key path, after the custody checks that need no daemon."""

    folder_key_custody(identity, path=path)
    return identity.private_key_path


@dataclass(frozen=True)
class VerifiedFolderIdentity:
    """A record whose principal the instance holds, with the matching local key."""

    identity: FolderIdentity
    record_path: Path
    principal: PrincipalRecord

    @property
    def principal_id(self) -> str:
        return self.identity.principal_id

    @property
    def key_dir(self) -> Path:
        return Path(self.identity.key_dir)

    @property
    def private_key_path(self) -> Path:
        return self.identity.private_key_path


def verify_folder_identity(
    identity: FolderIdentity,
    *,
    path: Path,
    principals: Sequence[PrincipalRecord],
    forbidden_roots: Sequence[Path],
) -> VerifiedFolderIdentity:
    """Check the record against the instance's principal registry and its key's custody.

    The principal must be an active ordinary principal there, and the key file
    must pass the signing custody checks (regular file, owner-only, outside
    every workspace) and hold that principal's public key.
    """

    key = require_folder_key(identity, path=path)
    principal = next(
        (item for item in principals if item.principal_id == identity.principal_id), None
    )
    if principal is None or principal.status != "active" or principal.kind != "ordinary":
        standing = "absent" if principal is None else f"{principal.status} {principal.kind}"
        raise FolderIdentityRefused(
            "cruxible.identity.folder_principal_unknown",
            path=path,
            detail=(
                f"instance {identity.instance_id} holds no active ordinary principal "
                f"{identity.principal_id!r} ({standing})"
            ),
        )
    try:
        assert_outside_roots(key, forbidden_roots)
        public_key = _load_private_key(key).public_key().public_bytes_raw().hex()
    except (OSError, SigningKeyError) as exc:
        raise FolderIdentityRefused(
            "cruxible.identity.folder_key_unavailable",
            path=path,
            detail=f"key {key} cannot be used ({exc})",
        ) from exc
    if public_key != principal.public_key:
        raise FolderIdentityRefused(
            "cruxible.identity.folder_key_mismatch",
            path=path,
            detail=(
                f"key {key} is not the key instance {identity.instance_id} registers for "
                f"{identity.principal_id!r}"
            ),
        )
    return VerifiedFolderIdentity(identity=identity, record_path=path, principal=principal)


def check_folder_identity(
    identity: FolderIdentity,
    *,
    path: Path,
    client: Any,
    forbidden_roots: Sequence[Path],
) -> VerifiedFolderIdentity:
    """The one check the MCP server and the CLI run before acting as the folder's principal.

    The key's custody first (no daemon read), then the daemon's principal
    registry for the record's instance through ``client``, then
    `verify_folder_identity`. A registry that cannot be read refuses
    (``cruxible.identity.folder_principal_unknown``); an unreachable daemon
    propagates as itself.
    """

    from cruxible_client.authoring.attestations import principal_records
    from cruxible_client.errors import CoreError, ServerUnreachableError

    folder_key_custody(identity, path=path)
    try:
        principals = principal_records(client, identity.instance_id)
    except ServerUnreachableError:
        raise
    except CoreError as exc:
        raise FolderIdentityRefused(
            "cruxible.identity.folder_principal_unknown",
            path=path,
            detail=f"the daemon's principal registry could not be read ({exc})",
        ) from exc
    return verify_folder_identity(
        identity, path=path, principals=principals, forbidden_roots=forbidden_roots
    )


__all__ = [
    "FOLDER_IDENTITY_PATH",
    "FOLDER_IDENTITY_TAG",
    "FolderIdentity",
    "FolderIdentityRefusalCode",
    "FolderIdentityRefused",
    "IdentitySource",
    "VerifiedFolderIdentity",
    "check_folder_identity",
    "folder_identity_path",
    "folder_key_custody",
    "read_bound_folder_identity",
    "read_folder_identity",
    "require_folder_key",
    "verify_folder_identity",
    "write_folder_identity",
]
