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

# Package-owned v2 operation schema. Both revisions use the same host-owned
# file-read protocol: the provider structures supplied bytes, never opens paths.
WORKSPACE_FILE_INTERFACE_V2_DIGEST = (
    "sha256:faa92552bd6032d3280753881ce991501013b2eaa2005e0f974820f01248d866"
)
WORKSPACE_FILE_INTERFACE_DIGESTS = frozenset(
    (WORKSPACE_FILE_INTERFACE_DIGEST, WORKSPACE_FILE_INTERFACE_V2_DIGEST)
)


class _StrictWorkspaceFileModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WorkspaceFileSourceRequest(_StrictWorkspaceFileModel):
    """Logical Source request v1; no canonical host root is representable.

    v1 made its author name the root by ``workspace_binding_digest``, a daemon
    transport detail. Kept exactly so Source requests retained in run history
    still read and verify; a Procedure now authors
    ``WorkspaceFileSourceRequestV2`` and the daemon executes it as an
    ``ExecutedWorkspaceFileSourceRequestV2``.
    """

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


_WORKSPACE_NAME_RE = re.compile(r"^[^/\\\x00]+$")


def _workspace_name(value: str) -> str:
    if (
        not _WORKSPACE_NAME_RE.fullmatch(value)
        or value in {".", ".."}
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("a workspace name is one printable path component")
    return value


class WorkspaceFileSourceRequestV2(_StrictWorkspaceFileModel):
    """The authored ``workspace.file`` Source request: intent only, no transport.

    ``workspace`` names an authorized root logically -- the last component of
    its path -- or is omitted: the instance's attached workspace, else its only
    authorized root. The daemon resolves the root and records its binding
    digest on the executed request, the read receipt and the Capture; an author
    never supplies it.
    """

    tag: Literal["playbill-workspace-file-source-request-v2"] = (
        "playbill-workspace-file-source-request-v2"
    )
    logical_source: str
    workspace: str | None = Field(default=None, exclude_if=lambda value: value is None)
    relative_path: str
    coordinate_type: str
    coordinate: object
    selector_type: str
    selector: object
    replayability: Literal["exact", "attested_only"] = "exact"

    _canonical = field_validator("coordinate", "selector", mode="before")(normalize_canonical)

    @field_validator("logical_source", "coordinate_type", "selector_type")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        return WorkspaceFileSourceRequest._nonempty(value)

    @field_validator("relative_path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        return WorkspaceFileSourceRequest._relative_path(value)

    @field_validator("workspace")
    @classmethod
    def _workspace(cls, value: str | None) -> str | None:
        return None if value is None else _workspace_name(value)


class ExecutedWorkspaceFileSourceRequestV2(_StrictWorkspaceFileModel):
    """What the daemon executed for one authored v2 request: the root it resolved.

    ``workspace`` is the logical name of the root the daemon read under (the
    authored name, or the one it defaulted to), and ``workspace_binding_digest``
    that root's binding to this instance. This, not the authored request, is the
    derived Source request a read receipt names, so provenance is unchanged: a
    moved instance records a different binding.
    """

    tag: Literal["playbill-workspace-file-executed-source-request-v2"] = (
        "playbill-workspace-file-executed-source-request-v2"
    )
    logical_source: str
    workspace: str
    workspace_binding_digest: str
    relative_path: str
    coordinate_type: str
    coordinate: object
    selector_type: str
    selector: object
    replayability: Literal["exact", "attested_only"] = "exact"

    _canonical = field_validator("coordinate", "selector", mode="before")(normalize_canonical)

    @field_validator("logical_source", "coordinate_type", "selector_type")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        return WorkspaceFileSourceRequest._nonempty(value)

    @field_validator("relative_path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        return WorkspaceFileSourceRequest._relative_path(value)

    @field_validator("workspace")
    @classmethod
    def _workspace(cls, value: str) -> str:
        return _workspace_name(value)

    @field_validator("workspace_binding_digest")
    @classmethod
    def _binding_digest(cls, value: str) -> str:
        return WorkspaceFileSourceRequest._binding_digest(value)

    @classmethod
    def executing(
        cls, authored: WorkspaceFileSourceRequestV2, *, workspace: str, binding_digest: str
    ) -> "ExecutedWorkspaceFileSourceRequestV2":
        return cls(
            logical_source=authored.logical_source,
            workspace=workspace,
            workspace_binding_digest=binding_digest,
            relative_path=authored.relative_path,
            coordinate_type=authored.coordinate_type,
            coordinate=authored.coordinate,
            selector_type=authored.selector_type,
            selector=authored.selector,
            replayability=authored.replayability,
        )


#: A derived (executed) workspace Source request as run history retains it.
ExecutedWorkspaceFileSourceRequest = (
    WorkspaceFileSourceRequest | ExecutedWorkspaceFileSourceRequestV2
)

_EXECUTED_V2_TAG = "playbill-workspace-file-executed-source-request-v2"


def parse_executed_workspace_request(value: object) -> ExecutedWorkspaceFileSourceRequest:
    """Read a retained derived request: the executed v2 shape, or a v1 request."""

    if isinstance(value, dict) and value.get("tag") == _EXECUTED_V2_TAG:
        return ExecutedWorkspaceFileSourceRequestV2.model_validate(value)
    return WorkspaceFileSourceRequest.model_validate(value)


def parse_authored_workspace_request(value: object) -> WorkspaceFileSourceRequestV2:
    """Read an authored request; the v1 binding digest is the daemon's, never the author's."""

    if isinstance(value, dict) and (
        "workspace_binding_digest" in value
        or value.get("tag") == "playbill-workspace-file-source-request-v1"
    ):
        raise WorkspaceBindingDigestAuthored()
    return WorkspaceFileSourceRequestV2.model_validate(value)


class WorkspaceBindingDigestAuthored(ValueError):
    """An authored request carries the binding digest the daemon now resolves."""

    def __init__(self) -> None:
        super().__init__(
            "workspace_binding_digest is resolved by the daemon, never authored: remove it "
            "(and any v1 tag); name the workspace with `workspace` only when several are "
            "authorized"
        )


class SourceReadReceipt(_StrictWorkspaceFileModel):
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
        return WorkspaceFileSourceRequest._relative_path(value)

    @field_validator("requested_path")
    @classmethod
    def _requested_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return WorkspaceFileSourceRequest._relative_path(value)


def source_read_receipt_digest(receipt: SourceReadReceipt) -> str:
    return typed_digest(
        Sha256Value,
        "playbill-source-read-receipt-v1",
        {"receipt": receipt.model_dump(mode="json")},
    ).tagged


__all__ = [
    "ExecutedWorkspaceFileSourceRequest",
    "ExecutedWorkspaceFileSourceRequestV2",
    "SourceReadReceipt",
    "WorkspaceBindingDigestAuthored",
    "WORKSPACE_FILE_INTERFACE_DIGEST",
    "WORKSPACE_FILE_INTERFACE_V2_DIGEST",
    "WORKSPACE_FILE_INTERFACE_DIGESTS",
    "WorkspaceFileSourceRequest",
    "WorkspaceFileSourceRequestV2",
    "parse_authored_workspace_request",
    "parse_executed_workspace_request",
    "source_read_receipt_digest",
]
