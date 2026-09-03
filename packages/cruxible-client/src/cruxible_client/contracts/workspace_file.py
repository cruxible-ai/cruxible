"""Contracts for the daemon-authorized ``workspace.file`` source read."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_client.contracts.canonical import Sha256Value, normalize_canonical, typed_digest
from cruxible_client.contracts.projection import AcceptedCoordinate

_RELATIVE_COMPONENT_RE = re.compile(r"^[^/\\\x00]+$")
WORKSPACE_FILE_INTERFACE_DIGEST = (
    "sha256:372bc808d6bd77627bdda7bc67586300e2eb812bf0a4fb3769283a26cc021f88"
)


class _StrictWorkspaceFileModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkspaceFileSourceRequestV1(_StrictWorkspaceFileModel):
    """Logical Source request; no canonical host root is representable."""

    tag: Literal["playbill-workspace-file-source-request-v1"] = (
        "playbill-workspace-file-source-request-v1"
    )
    logical_source: str
    workspace_binding_digest: str
    relative_path: str
    coordinate_type: str
    coordinate: object
    selector_type: str
    selector: object
    replayability: Literal["exact", "attested_only"] = "exact"

    _canonical = field_validator("coordinate", "selector", mode="before")(normalize_canonical)

    @field_validator("workspace_binding_digest")
    @classmethod
    def _binding_digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("logical_source", "coordinate_type", "selector_type")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        if not value or any(ord(character) < 32 for character in value):
            raise ValueError("workspace file source identifiers must be nonempty and printable")
        return value

    @field_validator("relative_path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        parts = value.split("/")
        if (
            not value
            or value.startswith("/")
            or value.endswith("/")
            or any(part in {"", ".", ".."} for part in parts)
            or not all(_RELATIVE_COMPONENT_RE.fullmatch(part) for part in parts)
            or any(part != part.strip() for part in parts)
            or any(
                ord(character) < 32 or ord(character) == 127 or character == "\ufeff"
                for character in value
            )
        ):
            raise ValueError("workspace file path must be normalized relative POSIX")
        return value


class SourceReadReceiptV1(_StrictWorkspaceFileModel):
    """Daemon receipt independently attesting one bounded authorized file read."""

    tag: Literal["playbill-source-read-receipt-v1"] = "playbill-source-read-receipt-v1"
    run_id: str
    admission_binding_digest: str
    occurrence_path: str
    logical_source: str
    workspace_binding_digest: str
    relative_path: str = Field(
        description="The REAL on-disk component names the daemon read, kernel-confirmed."
    )
    requested_path: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description=(
            "The spelling the derived Source request asked for, when the daemon recorded one. "
            "Absent on receipts written before the real-name law; never a lookup key."
        ),
    )
    bytes_digest: str
    byte_length: int = Field(ge=0)
    policy_coordinate: AcceptedCoordinate
    resolved_max_bytes: int = Field(ge=1)
    derived_request_digest: str
    provider_input_digest: str
    read_at: datetime = Field(description="Reads EVALUATION INSTANT.")

    @field_validator(
        "admission_binding_digest",
        "workspace_binding_digest",
        "bytes_digest",
        "derived_request_digest",
        "provider_input_digest",
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        Sha256Value.from_tagged(value)
        return value

    @field_validator("read_at")
    @classmethod
    def _read_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("workspace file read instant must be timezone-aware")
        return value

    @field_validator("relative_path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        return WorkspaceFileSourceRequestV1._relative_path(value)

    @field_validator("requested_path")
    @classmethod
    def _requested_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return WorkspaceFileSourceRequestV1._relative_path(value)


def source_read_receipt_digest(receipt: SourceReadReceiptV1) -> str:
    return typed_digest(
        Sha256Value,
        "playbill-source-read-receipt-v1",
        {"receipt": receipt.model_dump(mode="json")},
    ).tagged


__all__ = [
    "SourceReadReceiptV1",
    "WORKSPACE_FILE_INTERFACE_DIGEST",
    "WorkspaceFileSourceRequestV1",
    "source_read_receipt_digest",
]
