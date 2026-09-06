"""The ledger's publication to a private remote, and what it says when it lags.

The ledger is Git, so review is Git -- but only for a reviewer who can reach the
ledger. A daemon-owned bare repository under the instance root is reachable by
the daemon and nobody else, so every ref the review flow projects -- the
candidate commits, the three note refs, the open-proposal branches -- is
invisible to the person the projection exists for. The mirror is the one step
that closes that: one remote, coalesced publication after ledger writes, and an explicit
wait barrier for a reviewer who needs remote visibility.

Three properties keep it from becoming a second source of record.

* It is a PUBLICATION of already-accepted state, never a condition of it. A push
  that fails -- no network, no credential, a remote that moved -- is a typed
  warning row in ``playbill next``, never a refusal of the write that preceded
  it. The ledger on disk is the record; the remote is a copy.
* The URL is OPERATIONAL configuration, not accepted state. It lives on the
  instance descriptor beside the terminal decommission record, is absent from
  the canonical bytes when unset, and never enters a generation, a candidate, or
  any digest.
* The CREDENTIAL is never configuration at all. A remote URL carrying userinfo
  is refused by the grammar, so a token cannot be written into a descriptor that
  inspection reads back. The daemon reads its token from its own environment and
  hands it to Git through Git's environment-config protocol, so it appears in no
  command line, no config file and no error message.
"""

from __future__ import annotations

import base64
import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from cruxible_client.contracts.ledger_mirror import (
    LEDGER_MIRROR_CREDENTIAL_ENV,
    validate_mirror_url,
)

MIRROR_STATE_FILE: Final = "ledger-mirror.json"
"""Operational, rebuildable, and deliberately outside ``StorageLayout``.

The storage layout is part of the frozen ``playbill-instance-v1`` descriptor
preimage; a local record of whether the last push succeeded may never widen an
accepted format. Deleting this file loses nothing but the memory of the last
attempt, and the next publication rewrites it.
"""


def mirror_credential_environment(url: str) -> dict[str, str]:
    """Build the push-only environment: the token as a header, and nothing else.

    The token reaches Git through ``GIT_CONFIG_COUNT``/``GIT_CONFIG_KEY_0``/
    ``GIT_CONFIG_VALUE_0`` -- Git's own environment-config protocol -- rather
    than through ``-c http.extraHeader=...`` on the command line, because an
    argument vector is readable by every process on the host and an environment
    is not. It is set only for HTTPS remotes: an SSH or local remote has no
    header to carry it, and setting one anyway would offer the daemon's token to
    a host that never asked for it.
    """

    environment: dict[str, str] = {}
    for name in ("HOME", "SSH_AUTH_SOCK", "GIT_SSH_COMMAND"):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    token = os.environ.get(LEDGER_MIRROR_CREDENTIAL_ENV)
    if token and url.startswith("https://"):
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
        environment.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
            }
        )
    return environment


class LedgerMirrorStateV1(BaseModel):
    """What the last publication attempt did, for the one row that reports it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-ledger-mirror-state-v1"] = "playbill-ledger-mirror-state-v1"
    url: str
    status: Literal["current", "behind", "pending", "publishing"]
    attempted_at: str
    published_main_oid: str | None = None
    published_refs: dict[str, str] = Field(default_factory=dict)
    attempted_refs: dict[str, str] = Field(default_factory=dict)
    requested_sequence: int = Field(default=0, ge=0)
    attempted_sequence: int = Field(default=0, ge=0)
    published_sequence: int = Field(default=0, ge=0)
    wait_sequence: int | None = Field(default=None, ge=0)
    detail: str | None = Field(default=None, max_length=1_000)

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        return validate_mirror_url(value)


def read_mirror_state(root: Path) -> LedgerMirrorStateV1 | None:
    """Read the last attempt, treating an unreadable record as no record.

    A rebuildable operational file may not refuse a read the way accepted state
    does, so a truncated or hand-edited record reports nothing rather than
    raising: the next publication overwrites it, and until then the queue simply
    does not claim the mirror is current.
    """

    path = root / MIRROR_STATE_FILE
    if path.is_symlink() or not path.is_file():
        return None
    try:
        return LedgerMirrorStateV1.model_validate(json.loads(path.read_bytes()))
    except (OSError, ValueError, ValidationError):
        return None


def write_mirror_state(root: Path, state: LedgerMirrorStateV1) -> None:
    """Replace the last-attempt record atomically, or leave the previous one."""

    path = root / MIRROR_STATE_FILE
    payload = json.dumps(state.model_dump(mode="json"), sort_keys=True).encode() + b"\n"
    descriptor, temporary = tempfile.mkstemp(prefix=".ledger-mirror-", dir=root)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def mirror_lock(root: Path, *, publication: bool = False, review: bool = False) -> Iterator[None]:
    """Separate short state transactions from the long cross-process push lock."""

    name = (
        "ledger-review-projection.lock"
        if review
        else "ledger-mirror-publication.lock"
        if publication
        else "ledger-mirror-state.lock"
    )
    descriptor = os.open(root / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


__all__ = [
    "MIRROR_STATE_FILE",
    "LedgerMirrorStateV1",
    "mirror_credential_environment",
    "mirror_lock",
    "read_mirror_state",
    "write_mirror_state",
]
