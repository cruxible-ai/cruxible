"""Capture uploads: a scan or log sent to the daemon in parts, then read as a Capture.

An upload lands evidence that lives nowhere the daemon can read, such as a hosted
instance with no workspace mounts. The client opens a session naming the file's
exact length, sha256 and name; sends its bytes as raw parts of at most
``UPLOAD_PART_SIZE`` bytes, each its own request so every request stays under a
hosted edge's 1 MB body cap; then completes it by naming the acquire Procedure
that reads it. The daemon assembles the parts, checks them against the declared
sha256, stages the file in a daemon-owned ``uploads`` workspace, and runs the
Procedure exactly as a direct run, with the staged name in the input field the
Procedure reads its ``workspace.file`` path from. The Capture, its receipt, its
replay and the import Line that fires on it are the ordinary ones.

The staged file never outlives the completing request, and a session no one
completes expires. A session belongs to the principal that opened it.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_client.contracts import ProcedureRunState
from cruxible_client.contracts.canonical import Sha256Value
from cruxible_client.contracts.projection import AcceptedCoordinate

#: The most bytes one part carries: 512 KiB, so a part request stays under a
#: 1 MB edge body cap with room for its headers.
UPLOAD_PART_SIZE = 524_288

_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_UPLOAD_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class _StrictUploadModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def upload_filename(value: str) -> str:
    """One plain file name: letters, digits, ``.``, ``_`` and ``-``, never hidden."""

    if not _FILENAME_RE.fullmatch(value) or ".." in value:
        raise ValueError(
            "an upload's filename is one plain name (letters, digits, '.', '_', '-'; "
            "at most 128 characters, starting with a letter or digit)"
        )
    return value


def upload_id(value: str) -> str:
    if not _UPLOAD_ID_RE.fullmatch(value):
        raise ValueError("an upload id is 32 lowercase hex digits")
    return value


def upload_part_count(byte_length: int) -> int:
    return -(-byte_length // UPLOAD_PART_SIZE)


class CaptureUploadOpenRequest(_StrictUploadModel):
    """Open an upload session for one file of exactly these bytes."""

    byte_length: int = Field(ge=1, description="The file's exact length in bytes.")
    sha256: str = Field(description="The file's sha256, tagged (sha256:<hex>).")
    filename: str = Field(
        description=(
            "The name the file is staged under, and the path the acquire "
            "Procedure's workspace.file read receives."
        )
    )

    @field_validator("sha256")
    @classmethod
    def _sha256(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("filename")
    @classmethod
    def _filename(cls, value: str) -> str:
        return upload_filename(value)


class CaptureUploadSession(_StrictUploadModel):
    """An open upload: what it will hold, and which parts the daemon holds so far."""

    tag: Literal["cruxible-capture-upload-session-v1"] = "cruxible-capture-upload-session-v1"
    upload_id: str
    filename: str
    byte_length: int = Field(ge=1)
    sha256: str
    part_size: int = Field(ge=1)
    part_count: int = Field(ge=1)
    #: Part numbers (0-based) the daemon holds, in order.
    parts_received: tuple[int, ...] = ()
    expires_at: datetime

    @field_validator("upload_id")
    @classmethod
    def _upload_id(cls, value: str) -> str:
        return upload_id(value)

    @property
    def missing_parts(self) -> tuple[int, ...]:
        held = set(self.parts_received)
        return tuple(part for part in range(self.part_count) if part not in held)


class CaptureUploadCompleteRequest(_StrictUploadModel):
    """Read the uploaded file as a Capture by running the acquire Procedure on it.

    ``input`` is the Procedure's run input apart from the path: the daemon sets
    ``input[input_path_field]`` to the staged file name, so the Procedure's
    ``workspace.file`` read (``relative_path: $input.relative_path`` by default)
    finds the upload in its ``uploads`` workspace.
    """

    procedure: str = Field(description="The acquire Procedure that reads the upload.")
    input: dict[str, object] = Field(default_factory=dict)
    input_path_field: str = "relative_path"
    at: AcceptedCoordinate | None = None

    @field_validator("procedure")
    @classmethod
    def _procedure(cls, value: str) -> str:
        if not value or any(ord(character) < 33 for character in value):
            raise ValueError("a Procedure name is nonempty and has no spaces")
        return value

    @field_validator("input_path_field")
    @classmethod
    def _field(cls, value: str) -> str:
        if not _FIELD_RE.fullmatch(value):
            raise ValueError("input_path_field is one lowercase input field name")
        return value


class CaptureUploadCompleteResult(_StrictUploadModel):
    """The acquire run the upload completed into; the staged file is gone."""

    tag: Literal["cruxible-capture-upload-complete-result-v1"] = (
        "cruxible-capture-upload-complete-result-v1"
    )
    upload_id: str
    filename: str
    sha256: str
    byte_length: int
    run: ProcedureRunState


__all__ = [
    "UPLOAD_PART_SIZE",
    "CaptureUploadCompleteRequest",
    "CaptureUploadCompleteResult",
    "CaptureUploadOpenRequest",
    "CaptureUploadSession",
    "upload_filename",
    "upload_id",
    "upload_part_count",
]
