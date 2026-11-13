"""Core's builtin workspace reads: ``workspace.file`` (one path) and ``workspace.files`` (a glob).

A Procedure's call on a slot naming the base kit's ``workspace.file`` or
``workspace.files`` capability writes the request that capability declares
(:class:`WorkspaceFileRequest`, :class:`WorkspaceFilesRequest`) and nothing
else: the path or glob, the logical source, and optionally the workspace and
the revision. Core derives the rest when it reads: the coordinate and selector
types from the CaptureKind the read lands under (:func:`workspace_read_shape`),
the coordinate from the revision, a whole-file selector, and the workspace root.

The daemon reads each file itself and hands core's builtin operation the bytes
it read (``workspace.file.handoff@1``). That handoff is core's internal shape:
it is the builtin's own carried Contract and never a Contract a Procedure
sees. The builtin's results are carried copies of the base kit's result
Contracts, byte for byte: ``{capture}`` for one file, ``{captures}`` for a glob.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cruxible_client.contracts.canonical import Sha256Value, normalize_canonical, typed_digest
from cruxible_client.contracts.projection import AcceptedCoordinate

if TYPE_CHECKING:
    from cruxible_client.contracts.captures import CaptureContractAny

_RELATIVE_COMPONENT_RE = re.compile(r"^[^/\\\x00]+$")
_DIGEST_PATTERN = '"^sha256:[0-9a-f]{64}$"'
#: The base kit's ``logical_source`` pattern: a logical, locator-free source name.
_LOGICAL_SOURCE_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,255}$")

#: Why the daemon's workspace reader refuses a path: the class of the refusal.
WorkspaceFilePathClass = Literal[
    "cloud_no_mounts",
    "binding",
    "path_grammar",
    "git_metadata",
    "cruxible_control",
    "client_custody",
    "managed_root",
    "symlink",
    "hardlink",
    "non_regular",
    "unreadable",
    "changed_during_read",
    "missing",
    "size_budget",
    # The authored request names no workspace and several roots are authorized.
    "workspace_unnamed",
    # The authored request names a workspace no authorized root (or several) is called.
    "workspace_unknown",
    # A glob read matches more files than its cap (D-R41).
    "glob_over_cap",
]

#: The builtin's two operations, named as the capabilities they serve (ruling O2):
#: one path and one Capture, or a glob and a sorted, bounded list of Captures.
#: Core recognizes each by its exact signature, never by a provider's name for it.
WORKSPACE_FILE_OPERATION: Final = "workspace.file"
WORKSPACE_FILES_OPERATION: Final = "workspace.files"
WorkspaceReadOperation = Literal["workspace.file", "workspace.files"]

#: The bytes the daemon read for one file, as it hands them to the builtin.
WORKSPACE_FILE_HANDOFF_CONTRACT: dict[str, object] = {
    "name": "workspace.file.handoff",
    "version": 1,
    "description": (
        "The bytes the daemon read for one workspace file, handed to core's builtin. "
        "Core builds it; a Procedure never writes or reads it."
    ),
    "fields": {
        "logical_source": "string{min_len: 1}",
        "commitment_digest": f"string{{pattern: {_DIGEST_PATTERN}}}",
        "content_encoding": "enum[base64]",
        "bytes": "string",
        "byte_length": "int{0..}",
        "bytes_digest": f"string{{pattern: {_DIGEST_PATTERN}}}",
    },
}
#: The base kit's ``workspace.file.result@1``, byte for byte: the builtin's copy.
WORKSPACE_FILE_RESULT_CONTRACT: dict[str, object] = {
    "name": "workspace.file.result",
    "version": 1,
    "description": "The exact bytes the daemon read, as one Capture.",
    "fields": {"capture": {"type": {"kind": "capture", "capture_kind": "*"}}},
}
#: The base kit's ``workspace.files.result@1``, byte for byte: the builtin's copy.
WORKSPACE_FILES_RESULT_CONTRACT: dict[str, object] = {
    "name": "workspace.files.result",
    "version": 1,
    "description": (
        "The exact bytes the daemon read, one Capture per matched file, sorted by the "
        "files' names. A glob that matches nothing returns an empty list."
    ),
    "fields": {
        "captures": {
            "type": {
                "kind": "list",
                "item": {"kind": "capture", "capture_kind": "*"},
                "max_items": 1000,
            }
        }
    },
}
#: Detail of a ``provider_declined`` refusal: the built-in's digest mismatch
#: guard (``declared``/``computed``); both optional, since a plain decline
#: carries no detail.
WORKSPACE_FILE_REFUSAL_PROVIDER_DECLINED_CONTRACT: dict[str, object] = {
    "name": "workspace.file.refusal.provider_declined",
    "version": 1,
    "description": "Detail of a provider_declined refusal: the declared and computed digests.",
    "fields": {
        "declared": "string?",
        "computed": "string?",
    },
}
#: Detail of an ``invalid_parameter`` refusal: the built-in's several shape
#: guards share this one code. ``declared`` here is the fixed list of fields
#: the handoff declares (the "unknown fields" guard); the unrelated
#: "content_encoding must be base64" guard also names its own detail field
#: ``declared`` (the encoding string the caller sent), a str-vs-list collision
#: this Contract cannot represent both sides of. That one path refuses
#: ``contract_output_refused`` instead of a clean ``invalid_parameter`` node
#: refusal: typed and legitimate, just less specific. Core builds the handoff,
#: so neither guard fires on a read core made.
WORKSPACE_FILE_REFUSAL_INVALID_PARAMETER_CONTRACT: dict[str, object] = {
    "name": "workspace.file.refusal.invalid_parameter",
    "version": 1,
    "description": "Detail of an invalid_parameter refusal: the offending field(s).",
    "fields": {
        "field": "string?",
        "unknown": "list<string>{max_items: 16}?",
        "missing": "list<string>{max_items: 16}?",
        "supported": "list<string>{max_items: 16}?",
        "content_encoding": "string?",
        "declared": "list<string>{max_items: 16}?",
    },
}
#: Detail of a ``mismatched_lengths`` refusal: the declared vs. decoded byte length.
WORKSPACE_FILE_REFUSAL_MISMATCHED_LENGTHS_CONTRACT: dict[str, object] = {
    "name": "workspace.file.refusal.mismatched_lengths",
    "version": 1,
    "description": "Detail of a mismatched_lengths refusal: declared vs decoded byte length.",
    "fields": {
        "declared": "int{0..}?",
        "decoded": "int{0..}?",
    },
}


def workspace_file_refusal_contracts() -> tuple[object, object, object]:
    """The three refusal-detail Contracts the builtin's typed refusals pin:
    ``provider_declined``, ``invalid_parameter`` and ``mismatched_lengths``.
    """

    from cruxible_client.contracts.shapes import validate_contract

    return (
        validate_contract(WORKSPACE_FILE_REFUSAL_PROVIDER_DECLINED_CONTRACT),
        validate_contract(WORKSPACE_FILE_REFUSAL_INVALID_PARAMETER_CONTRACT),
        validate_contract(WORKSPACE_FILE_REFUSAL_MISMATCHED_LENGTHS_CONTRACT),
    )


def workspace_file_contracts() -> tuple[object, object, object]:
    """The builtin's carried operation Contracts: the handoff, then the one-file and
    the glob result (the base kit's result Contracts, byte for byte)."""

    from cruxible_client.contracts.shapes import validate_contract

    return (
        validate_contract(WORKSPACE_FILE_HANDOFF_CONTRACT),
        validate_contract(WORKSPACE_FILE_RESULT_CONTRACT),
        validate_contract(WORKSPACE_FILES_RESULT_CONTRACT),
    )


def workspace_read_signature_digest(operation: WorkspaceReadOperation) -> str:
    """The exact signature digest of one of the builtin's operations: the handoff
    in, that operation's result out, effect pure."""

    from cruxible_client.contracts.provider_operations import (
        OperationSignature,
        operation_signature_digest,
    )
    from cruxible_client.contracts.shapes import Contract, contract_digest

    handoff, file_result, files_result = workspace_file_contracts()
    result = file_result if operation == WORKSPACE_FILE_OPERATION else files_result
    assert isinstance(handoff, Contract) and isinstance(result, Contract)
    return operation_signature_digest(
        OperationSignature.model_validate(
            {
                "in": {
                    "name": handoff.name,
                    "version": handoff.version,
                    "digest": contract_digest(handoff).tagged,
                },
                "out": {
                    "name": result.name,
                    "version": result.version,
                    "digest": contract_digest(result).tagged,
                },
                "effect": "pure",
            }
        )
    )


def workspace_file_signature_digest() -> str:
    """The exact signature digest of the builtin's one-file ``workspace.file`` operation."""

    return workspace_read_signature_digest(WORKSPACE_FILE_OPERATION)


_SIGNATURES: dict[str, WorkspaceReadOperation] = {}


def builtin_workspace_operation(digest: str | None) -> WorkspaceReadOperation | None:
    """Which of the builtin's workspace reads an operation signature digest is, if either."""

    if digest is None:
        return None
    if not _SIGNATURES:
        for operation in (WORKSPACE_FILE_OPERATION, WORKSPACE_FILES_OPERATION):
            _SIGNATURES[workspace_read_signature_digest(operation)] = operation
    return _SIGNATURES.get(digest)


def is_workspace_file_signature(digest: str | None) -> bool:
    """Whether an operation signature digest is one of the builtin's workspace reads."""

    return builtin_workspace_operation(digest) is not None


class _StrictWorkspaceFileModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _relative_path_value(value: str) -> str:
    parts = value.split("/")
    if (
        not value
        or value.startswith("/")
        or value.endswith("/")
        or any(part in {"", ".", ".."} for part in parts)
        or not all(_RELATIVE_COMPONENT_RE.fullmatch(part) for part in parts)
        or any(part != part.strip() for part in parts)
        or any(
            ord(character) < 32 or ord(character) == 127 or character == "﻿" for character in value
        )
    ):
        raise ValueError("workspace file path must be normalized relative POSIX")
    return value


def _logical_source_value(value: str) -> str:
    if not _LOGICAL_SOURCE_RE.fullmatch(value):
        raise ValueError(
            "logical_source is a logical, locator-free name: lowercase, starting with a "
            "letter, then letters, digits, '_', '.' or '-' (at most 256 characters)"
        )
    return value


# v1 made its author name the root by ``workspace_binding_digest``, a daemon
# transport detail. Kept byte-for-byte (schema included) so Source requests
# retained in run history still read and verify.
class WorkspaceFileSourceRequest(_StrictWorkspaceFileModel):
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
        return _relative_path_value(value)


_WORKSPACE_NAME_RE = re.compile(r"^[^/\\\x00]+$")


def _workspace_name(value: str) -> str:
    if (
        not _WORKSPACE_NAME_RE.fullmatch(value)
        or value in {".", ".."}
        or value != value.strip()
        or len(value) > 255
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("a workspace name is one printable path component")
    return value


#: The most files one glob read may match (D-R41): past its cap a glob refuses.
MAX_WORKSPACE_GLOB_FILES = 1000
_GLOB_CHARACTERS = frozenset("*?[")


def _glob_value(value: str) -> str:
    _relative_path_value(value)
    parts = value.split("/")
    if any(part == "**" for part in parts):
        raise ValueError("a workspace glob matches within one directory per component")
    if not any(character in _GLOB_CHARACTERS for character in value):
        raise ValueError("a workspace glob names a pattern; read one file by relative_path")
    return value


class WorkspaceFileRequest(_StrictWorkspaceFileModel):
    """What a Procedure writes for a ``workspace.file`` read: the base kit's
    ``workspace.file.request@1``, and every field core's builtin reads.

    ``workspace`` names an authorized root logically (the last component of its
    path) or is omitted: the instance's attached workspace, else its only
    authorized root. ``revision`` is the working tree, the only one core reads.
    """

    logical_source: str
    relative_path: str = Field(max_length=1024)
    revision: Literal["working"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    workspace: str | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("logical_source")
    @classmethod
    def _logical_source(cls, value: str) -> str:
        return _logical_source_value(value)

    @field_validator("relative_path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        return _relative_path_value(value)

    @field_validator("workspace")
    @classmethod
    def _workspace(cls, value: str | None) -> str | None:
        return None if value is None else _workspace_name(value)


class WorkspaceFilesRequest(_StrictWorkspaceFileModel):
    """What a Procedure writes for a ``workspace.files`` read (D-R41): the base kit's
    ``workspace.files.request@1``, and every field core's builtin reads.

    ``glob`` is a relative POSIX path whose components may use ``*``, ``?`` and
    ``[...]`` (a ``**`` component is not a pattern). The daemon lists the
    matches under the resolved workspace, sorted by their real names, and reads
    each as one ordinary file read: the call's one list field holds one Capture
    per file, in that order. More matches than ``max_files`` refuses the read
    before any file is read. A file that fails to read is dropped and the list
    is incomplete; a file the glob no longer matches is simply not read, and
    nothing about it retires.
    """

    logical_source: str
    glob: str = Field(max_length=1024)
    max_files: int = Field(default=100, ge=1, le=MAX_WORKSPACE_GLOB_FILES)
    revision: Literal["working"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    workspace: str | None = Field(default=None, exclude_if=lambda value: value is None)

    @field_validator("logical_source")
    @classmethod
    def _logical_source(cls, value: str) -> str:
        return _logical_source_value(value)

    @field_validator("glob")
    @classmethod
    def _glob(cls, value: str) -> str:
        return _glob_value(value)

    @field_validator("workspace")
    @classmethod
    def _workspace(cls, value: str | None) -> str | None:
        return None if value is None else _workspace_name(value)


#: Every request field core's builtin reads, per operation: a capability of the
#: same name whose request declares any other field is refused at authoring.
WORKSPACE_READ_FIELDS: Mapping[WorkspaceReadOperation, frozenset[str]] = {
    WORKSPACE_FILE_OPERATION: frozenset(WorkspaceFileRequest.model_fields),
    WORKSPACE_FILES_OPERATION: frozenset(WorkspaceFilesRequest.model_fields),
}


def workspace_read_operation(capability_name: str) -> WorkspaceReadOperation | None:
    """The builtin read that serves a capability of this name, if one does."""

    if capability_name == WORKSPACE_FILE_OPERATION:
        return WORKSPACE_FILE_OPERATION
    if capability_name == WORKSPACE_FILES_OPERATION:
        return WORKSPACE_FILES_OPERATION
    return None


class WorkspaceReadShapeRefused(ValueError):
    """A workspace read cannot land under this CaptureKind: core cannot derive its source."""


class WorkspaceReadShape(_StrictWorkspaceFileModel):
    """What core derives for a workspace read from the CaptureKind it lands under.

    The coordinate type and selector type are the kind's one coordinate and one
    selector schema; replayability follows the kind's replay. The coordinate
    names the revision read and the selector the whole file.
    """

    coordinate_type: str
    selector_type: str
    replayability: Literal["exact", "attested_only"]

    def coordinate(self, revision: str | None) -> dict[str, object]:
        return {"revision": revision or "working"}

    @staticmethod
    def selector() -> dict[str, object]:
        return {"whole": True}


def workspace_read_shape(contract: CaptureContractAny) -> WorkspaceReadShape:
    """The source shape core derives for a workspace read landing under ``contract``.

    Refuses a kind that declares several coordinate or selector schemas (or
    none): a file read has one revision and reads the whole file, so the author
    has nothing to choose between them with.
    """

    from cruxible_client.contracts.captures import capture_components

    components = capture_components(contract)
    coordinates = components.coordinate_schema_pins
    selectors = components.selector_schema_pins
    if len(coordinates) != 1 or len(selectors) != 1:
        raise WorkspaceReadShapeRefused(
            f"CaptureKind {contract.identity.name} declares {len(coordinates)} coordinate and "
            f"{len(selectors)} selector schemas; a workspace read lands only under a kind with "
            "exactly one of each (an http_response kind), since core derives the read's "
            "coordinate and selector from it"
        )
    return WorkspaceReadShape(
        coordinate_type=coordinates[0].target.name,
        selector_type=selectors[0].target.name,
        replayability=(
            "exact" if getattr(contract, "replay", "exact") == "exact" else "attested_only"
        ),
    )


#: The codes of a call core's builtin could not serve as authored, without their
#: prefix: authoring raises them under ``cruxible.authoring.``, the Procedure
#: acceptance law under ``cruxible.procedure.``.
WORKSPACE_REQUEST_FIELD_UNREAD: Final = "workspace_request_field_unread"
WORKSPACE_RESULT_FIELD_UNSERVED: Final = "workspace_result_field_unserved"
WORKSPACE_READ_KIND_UNSUPPORTED: Final = "workspace_read_kind_unsupported"


class WorkspaceReadProblem(_StrictWorkspaceFileModel):
    """Why core's builtin cannot serve one workspace read call as it is declared.

    ``code`` is unprefixed; ``element`` is where, relative to the call:
    ``input`` (its request), ``captures`` (its result) or ``captures.<field>``
    (the kind it lands under).
    """

    code: Literal[
        "workspace_request_field_unread",
        "workspace_result_field_unserved",
        "workspace_read_kind_unsupported",
    ]
    message: str
    element: str


def _type_form(field: object) -> str | None:
    from cruxible_client.contracts.shapes import CaptureType, ListType

    typed = getattr(field, "type", None)
    if isinstance(typed, CaptureType):
        return "capture"
    if isinstance(typed, ListType) and isinstance(typed.item, CaptureType):
        return "list<capture>"
    return None


def workspace_read_problem(
    operation: WorkspaceReadOperation,
    *,
    capability: str,
    request: object | None,
    result: object | None,
    capture_kind: CaptureContractAny | None,
    capture_field: str | None,
) -> WorkspaceReadProblem | None:
    """The first reason core's builtin cannot serve a call on capability ``capability``.

    The builtin is bound by name, whatever version of the capability a slot
    pins, and what it reads and returns is core's code (section 7.4): the
    request may declare only fields the builtin reads, the result must be
    field for field what the builtin returns (``capture`` for one path,
    ``captures`` for a glob), and the Capture must land under a kind core can
    derive the read's coordinate and selector from. ``request`` and ``result``
    are the capability's Contracts (``None`` when not resolved: not checked).
    Authoring and the Procedure acceptance law both ask this.
    """

    from cruxible_client.contracts.shapes import Contract, validate_contract

    if isinstance(request, Contract):
        read = WORKSPACE_READ_FIELDS[operation]
        unread = sorted(set(request.fields) - read, key=lambda name: name.encode("utf-8"))
        if unread:
            return WorkspaceReadProblem(
                code=WORKSPACE_REQUEST_FIELD_UNREAD,
                message=(
                    f"{capability}'s request {request.name}@{request.version} declares "
                    f"{', '.join(repr(name) for name in unread)}, which core's builtin "
                    f"{operation} read does not read; it reads {', '.join(sorted(read))}. "
                    "Remove the field in a new version of the capability's request."
                ),
                element="input",
            )
    if isinstance(result, Contract):
        served = validate_contract(
            WORKSPACE_FILE_RESULT_CONTRACT
            if operation == WORKSPACE_FILE_OPERATION
            else WORKSPACE_FILES_RESULT_CONTRACT
        )
        unserved = sorted(
            (
                name
                for name in set(result.fields) | set(served.fields)
                if name not in result.fields
                or name not in served.fields
                or _type_form(result.fields[name]) != _type_form(served.fields[name])
            ),
            key=lambda name: name.encode("utf-8"),
        )
        if unserved:
            (expected,) = served.fields
            return WorkspaceReadProblem(
                code=WORKSPACE_RESULT_FIELD_UNSERVED,
                message=(
                    f"{capability}'s result {result.name}@{result.version} declares "
                    f"{sorted(result.fields)}; core's builtin {operation} read returns "
                    f"exactly {expected!r} ({_type_form(served.fields[expected])}), so "
                    f"{', '.join(repr(name) for name in unserved)} cannot be served. "
                    f"Name the result field {expected!r} in a new version of the capability."
                ),
                element="captures",
            )
    if capture_kind is not None:
        try:
            workspace_read_shape(capture_kind)
        except WorkspaceReadShapeRefused as exc:
            return WorkspaceReadProblem(
                code=WORKSPACE_READ_KIND_UNSUPPORTED,
                message=str(exc),
                element="captures" if capture_field is None else f"captures.{capture_field}",
            )
    return None


class WorkspaceFileSourceRequestV2(_StrictWorkspaceFileModel):
    """One file read as core performs it, before its workspace root is resolved.

    Built by core from the authored :class:`WorkspaceFileRequest` and the
    :class:`WorkspaceReadShape` of the kind the read lands under; never authored
    and never stored. The daemon resolves the root and records its binding
    digest on the executed request, the read receipt and the Capture.
    """

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
        return _relative_path_value(value)

    @field_validator("workspace")
    @classmethod
    def _workspace(cls, value: str | None) -> str | None:
        return None if value is None else _workspace_name(value)

    @classmethod
    def reading(
        cls, authored: WorkspaceFileRequest, shape: WorkspaceReadShape
    ) -> WorkspaceFileSourceRequestV2:
        return cls(
            logical_source=authored.logical_source,
            workspace=authored.workspace,
            relative_path=authored.relative_path,
            coordinate_type=shape.coordinate_type,
            coordinate=shape.coordinate(authored.revision),
            selector_type=shape.selector_type,
            selector=shape.selector(),
            replayability=shape.replayability,
        )


class WorkspaceFileGlobRequestV1(_StrictWorkspaceFileModel):
    """One glob read as core performs it (D-R41), before its root is resolved.

    Built by core from the authored :class:`WorkspaceFilesRequest` and the
    kind's :class:`WorkspaceReadShape`; never authored and never stored. Each
    match is read as the one-file request :meth:`member` returns.
    """

    logical_source: str
    workspace: str | None = Field(default=None, exclude_if=lambda value: value is None)
    glob: str
    max_files: int = Field(default=100, ge=1, le=MAX_WORKSPACE_GLOB_FILES)
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

    @field_validator("glob")
    @classmethod
    def _glob(cls, value: str) -> str:
        return _glob_value(value)

    @field_validator("workspace")
    @classmethod
    def _workspace(cls, value: str | None) -> str | None:
        return None if value is None else _workspace_name(value)

    @classmethod
    def reading(
        cls, authored: WorkspaceFilesRequest, shape: WorkspaceReadShape
    ) -> WorkspaceFileGlobRequestV1:
        return cls(
            logical_source=authored.logical_source,
            workspace=authored.workspace,
            glob=authored.glob,
            max_files=authored.max_files,
            coordinate_type=shape.coordinate_type,
            coordinate=shape.coordinate(authored.revision),
            selector_type=shape.selector_type,
            selector=shape.selector(),
            replayability=shape.replayability,
        )

    def member(self, relative_path: str) -> WorkspaceFileSourceRequestV2:
        """The ordinary one-file request the daemon reads for one match."""

        return WorkspaceFileSourceRequestV2(
            logical_source=self.logical_source,
            workspace=self.workspace,
            relative_path=relative_path,
            coordinate_type=self.coordinate_type,
            coordinate=self.coordinate,
            selector_type=self.selector_type,
            selector=self.selector,
            replayability=self.replayability,
        )


def is_workspace_glob(value: object) -> bool:
    """Whether an authored workspace read input reads a glob (it names ``glob``)."""

    return isinstance(value, dict) and "glob" in value


def parse_authored_workspace_glob(
    value: object, shape: WorkspaceReadShape
) -> WorkspaceFileGlobRequestV1:
    """Read an authored ``workspace.files`` request as the glob core performs."""

    if isinstance(value, dict) and "workspace_binding_digest" in value:
        raise WorkspaceBindingDigestAuthored()
    return WorkspaceFileGlobRequestV1.reading(WorkspaceFilesRequest.model_validate(value), shape)


class ExecutedWorkspaceFileSourceRequestV2(_StrictWorkspaceFileModel):
    """What the daemon executed for one file read: the root it resolved.

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
        return _relative_path_value(value)

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


def parse_authored_workspace_request(
    value: object, shape: WorkspaceReadShape
) -> WorkspaceFileSourceRequestV2:
    """Read an authored ``workspace.file`` request as the read core performs."""

    if isinstance(value, dict) and "workspace_binding_digest" in value:
        raise WorkspaceBindingDigestAuthored()
    return WorkspaceFileSourceRequestV2.reading(WorkspaceFileRequest.model_validate(value), shape)


class WorkspaceBindingDigestAuthored(ValueError):
    """An authored request carries the binding digest the daemon resolves."""

    def __init__(self) -> None:
        super().__init__(
            "workspace_binding_digest is resolved by the daemon, never authored: remove it; "
            "name the workspace with `workspace` only when several are authorized"
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
    "MAX_WORKSPACE_GLOB_FILES",
    "SourceReadReceipt",
    "WORKSPACE_FILES_OPERATION",
    "WORKSPACE_FILES_RESULT_CONTRACT",
    "WORKSPACE_FILE_HANDOFF_CONTRACT",
    "WORKSPACE_FILE_OPERATION",
    "WORKSPACE_FILE_REFUSAL_INVALID_PARAMETER_CONTRACT",
    "WORKSPACE_FILE_REFUSAL_MISMATCHED_LENGTHS_CONTRACT",
    "WORKSPACE_FILE_REFUSAL_PROVIDER_DECLINED_CONTRACT",
    "WORKSPACE_FILE_RESULT_CONTRACT",
    "WORKSPACE_READ_FIELDS",
    "WORKSPACE_READ_KIND_UNSUPPORTED",
    "WORKSPACE_REQUEST_FIELD_UNREAD",
    "WORKSPACE_RESULT_FIELD_UNSERVED",
    "WorkspaceReadProblem",
    "WorkspaceBindingDigestAuthored",
    "WorkspaceFileGlobRequestV1",
    "WorkspaceFilePathClass",
    "WorkspaceFileRequest",
    "WorkspaceFileSourceRequest",
    "WorkspaceFileSourceRequestV2",
    "WorkspaceFilesRequest",
    "WorkspaceReadOperation",
    "WorkspaceReadShape",
    "WorkspaceReadShapeRefused",
    "builtin_workspace_operation",
    "is_workspace_file_signature",
    "is_workspace_glob",
    "parse_authored_workspace_glob",
    "parse_authored_workspace_request",
    "parse_executed_workspace_request",
    "source_read_receipt_digest",
    "workspace_file_contracts",
    "workspace_file_refusal_contracts",
    "workspace_file_signature_digest",
    "workspace_read_operation",
    "workspace_read_problem",
    "workspace_read_shape",
    "workspace_read_signature_digest",
]
