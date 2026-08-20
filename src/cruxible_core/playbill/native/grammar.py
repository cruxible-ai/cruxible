"""The typed region grammar and the human-invisible locator channel.

Everything in this module is **class-3 experimental** (§11.9 status note): the
serialized spellings below are shaped by dogfooding and are expected to change,
so they are versioned and digested rather than frozen as wire tags. What does
not change with them is the semantics this module makes structural:

*typed regions* -- :data:`NATIVE_REGION_EDITABLE` is the whole editable/derived
split, in one map. A locator cannot be constructed whose ``editable`` flag
disagrees with its kind, so "a derived field claims to be editable" is
unrepresentable rather than merely refused later.

*locators are untrusted* -- §11.6.5. A marker in a file is a *locator*, never a
signature: it says where to look, and everything it asserts is re-checked
against accepted state and the render baseline before it means anything. This
module only reads markers out of bytes; :mod:`.verify` decides whether one is
true. A copied or hand-authored marker parses perfectly and grants nothing.

*identity is path-free* -- a region's identity is
``(semantic address, region kind, ordinal, lens)`` digested, and a filesystem
path appears nowhere in it. Moving or renaming a rendered file therefore
preserves every region identity in it, exactly as §11.9.3 requires: paths are
presentation coordinates over §11.6.1 source-occurrence identity.

The channel itself is an HTML comment, which is invisible in every Markdown
renderer while staying plain text in the file. The payload is canonical JSON so
that reading a marker is exact rather than a parse of prose, and emission
refuses any payload that could close the comment early -- a locator that could
be truncated into surrounding prose is not a channel, it is a hazard.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from cruxible_core.playbill.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_core.playbill.errors import PlaybillError
from cruxible_core.playbill.semantic import SemanticAddress

# -- the versioned lens ----------------------------------------------------

NATIVE_LENS_ID: Final = "playbill-native-markdown"
"""The one default in-repo lens (§11.9.1). Additional lenses are cloud-side
additive presentation and never enter canonical identity."""

NATIVE_LENS_VERSION: Final = 1
"""Bumped whenever a spelling below changes. Not a compatibility promise: the
grammar is class-3, and the version exists so a render can say which grammar
produced it rather than so old spellings can be preserved."""

NATIVE_GRAMMAR_CLASS: Final = "experimental"

REGION_OPEN_PREFIX: Final = "<!--playbill:region "
REGION_CLOSE: Final = "<!--playbill:/region-->"
FILE_MARKER_PREFIX: Final = "<!--playbill:file "
MARKER_SUFFIX: Final = "-->"

REGION_IDENTITY_DIGEST_DOMAIN: Final = "playbill-native-region-identity-v1"
RENDERER_DIGEST_DOMAIN: Final = "playbill-native-renderer-v1"

NativeRegionKind = Literal[
    "statement_value",
    "statement_qualifier",
    "governance",
    "provenance",
    "coverage",
    "structure",
]

NATIVE_REGION_KINDS: Final[tuple[str, ...]] = (
    "coverage",
    "governance",
    "provenance",
    "statement_qualifier",
    "statement_value",
    "structure",
)

NATIVE_REGION_EDITABLE: Final[Mapping[str, bool]] = {
    "coverage": False,
    "governance": False,
    "provenance": False,
    "statement_qualifier": True,
    "statement_value": True,
    "structure": False,
}
"""§11.9.2's typed split, as one map rather than a convention.

Editable fields carry statement content and values, and are free-form *inside*
the field. Derived fields carry verdict, currency, provenance, and coverage:
they regenerate from accepted state and refuse edits with a typed diagnostic,
because a derived field is a projection of something the ledger decided and
nothing typed into a working file can change what that was.
"""

DERIVED_REGENERATION_INSTRUCTION: Final = (
    "This region is derived from accepted state and regenerates. Re-render to "
    "restore it; the edited text is not interpreted as a proposal."
)


class NativeRenderError(PlaybillError):
    """A native render, parse, or locator operation could not proceed."""


class _StrictNativeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NativeLensV1(_StrictNativeModel):
    """Which lens produced a render, and the digest of its exact spellings.

    ``renderer_digest`` is computed from the spelling table rather than typed in
    by hand, so a spelling change that nobody remembered to version still shows
    up as a different renderer digest in every manifest that used it.
    """

    tag: Literal["playbill-native-lens-v1"] = "playbill-native-lens-v1"
    lens_id: str = NATIVE_LENS_ID
    lens_version: int = Field(default=NATIVE_LENS_VERSION, ge=1)
    grammar_class: Literal["experimental", "stable"] = NATIVE_GRAMMAR_CLASS
    renderer_digest: str

    @model_validator(mode="after")
    def _digest(self) -> "NativeLensV1":
        Sha256Value.from_tagged(self.renderer_digest)
        return self


def native_renderer_digest() -> str:
    """Digest exactly the spellings and the typed split this lens renders with."""

    return typed_digest(
        Sha256Value,
        RENDERER_DIGEST_DOMAIN,
        {
            "editable": dict(sorted(NATIVE_REGION_EDITABLE.items())),
            "file_marker_prefix": FILE_MARKER_PREFIX,
            "kinds": list(NATIVE_REGION_KINDS),
            "lens_id": NATIVE_LENS_ID,
            "lens_version": NATIVE_LENS_VERSION,
            "marker_suffix": MARKER_SUFFIX,
            "region_close": REGION_CLOSE,
            "region_open_prefix": REGION_OPEN_PREFIX,
        },
    ).tagged


def default_native_lens() -> NativeLensV1:
    """The versioned default lens, with its spellings already digested."""

    return NativeLensV1(renderer_digest=native_renderer_digest())


# -- region identity and locators -----------------------------------------


def body_commitment(body: bytes) -> str:
    """Commit to one region body, in the same shape coverage commits to bytes."""

    return Sha256Value(hashlib.sha256(body).hexdigest()).tagged


def region_identity_digest(
    *,
    address: SemanticAddress,
    region_kind: str,
    ordinal: int = 0,
    lens: NativeLensV1,
) -> str:
    """Digest the four values that identify one region, none of them a path.

    A rendered file's location is presentation. Two renders that place the same
    Claim in different files produce the same region identity, which is what
    makes §11.9.3's "file moves/renames preserve stable region identity" a
    property of the identity rather than a bookkeeping step somebody has to
    remember to perform.
    """

    if region_kind not in NATIVE_REGION_EDITABLE:
        raise NativeRenderError(f"unknown native region kind: {region_kind}")
    if ordinal < 0:
        raise NativeRenderError("native region ordinal must be non-negative")
    return typed_digest(
        Sha256Value,
        REGION_IDENTITY_DIGEST_DOMAIN,
        {
            "address": address.model_dump(mode="json"),
            "lens_id": lens.lens_id,
            "lens_version": lens.lens_version,
            "ordinal": ordinal,
            "region_kind": region_kind,
        },
    ).tagged


class NativeLocatorV1(_StrictNativeModel):
    """The human-invisible binding between one rendered region and accepted state.

    It carries exactly what §11.6.5 permits a locator to carry -- an address, a
    digest, and a coordinate -- and it is believed about none of them. The
    ``baseline_digest`` is the region body as rendered, which is what makes a
    dirty region detectable from the file alone; the render manifest holds the
    authoritative copy, so an edited marker disagrees with the baseline and is
    caught rather than believed.
    """

    tag: Literal["playbill-native-locator-v1"] = "playbill-native-locator-v1"
    lens_id: str
    lens_version: int = Field(ge=1)
    region_id: str
    region_kind: NativeRegionKind
    editable: bool
    address: SemanticAddress
    artifact_digest: str
    generation_root: str
    baseline_digest: str

    @model_validator(mode="after")
    def _locator_law(self) -> "NativeLocatorV1":
        for value in (self.region_id, self.artifact_digest, self.generation_root):
            Sha256Value.from_tagged(value)
        Sha256Value.from_tagged(self.baseline_digest)
        if self.editable != NATIVE_REGION_EDITABLE[self.region_kind]:
            raise ValueError("a locator may not disagree with the typed editable/derived split")
        return self

    @property
    def sort_key(self) -> bytes:
        return self.region_id.encode("ascii")


# -- the marker channel ----------------------------------------------------


class NativeFileMarkerV1(_StrictNativeModel):
    """The per-file header marker: which lens, which generation, which read time.

    ``disposition`` is §11.9.4's native/foreign guard, projected redundantly out
    to the file itself. Only ``native_editable`` files carry compilable regions;
    ``orientation`` files are signposts with no governed material in them at all.
    """

    tag: Literal["playbill-native-file-marker-v1"] = "playbill-native-file-marker-v1"
    lens_id: str
    lens_version: int = Field(ge=1)
    path: str
    disposition: Literal["native_editable", "foreign_observed", "orientation"]
    generation_root: str
    evaluation_time: str
    scope_digest: str

    @model_validator(mode="after")
    def _marker_law(self) -> "NativeFileMarkerV1":
        Sha256Value.from_tagged(self.generation_root)
        Sha256Value.from_tagged(self.scope_digest)
        return self


class NativeDiagnosticV1(_StrictNativeModel):
    """One typed thing the grammar has to say about a parsed file.

    A ``refusal`` means the region cannot be read as semantic material at all --
    a tampered derived region, a duplicated locator, an unterminated region. A
    ``notice`` is an observation that changes nothing about admissibility: a
    dirty editable region is a notice, because editing is exactly what an
    editable region is for.
    """

    tag: Literal["playbill-native-diagnostic-v1"] = "playbill-native-diagnostic-v1"
    code: str
    severity: Literal["refusal", "notice"]
    path: str | None = None
    region_id: str | None = None
    message: str
    instruction: str | None = None


def _reject_comment_escape(payload: str, *, label: str) -> str:
    if MARKER_SUFFIX in payload or "\n" in payload or "\r" in payload:
        raise NativeRenderError(
            f"{label} would close its own comment channel; refusing to render a truncatable locator"
        )
    return payload


def render_region_open(locator: NativeLocatorV1) -> str:
    """Emit the opening marker of one region as a single invisible line."""

    payload = canonical_bytes(locator.model_dump(mode="json")).decode("utf-8")
    _reject_comment_escape(payload, label="region locator")
    return REGION_OPEN_PREFIX + payload + MARKER_SUFFIX


def render_file_marker(marker: NativeFileMarkerV1) -> str:
    """Emit the file header marker as a single invisible line."""

    payload = canonical_bytes(marker.model_dump(mode="json")).decode("utf-8")
    _reject_comment_escape(payload, label="file marker")
    return FILE_MARKER_PREFIX + payload + MARKER_SUFFIX


def parse_region_open(line: str) -> NativeLocatorV1 | None:
    """Read one opening marker, or return nothing when the line is ordinary text."""

    stripped = line.strip()
    if not stripped.startswith(REGION_OPEN_PREFIX) or not stripped.endswith(MARKER_SUFFIX):
        return None
    payload = stripped[len(REGION_OPEN_PREFIX) : -len(MARKER_SUFFIX)]
    try:
        return NativeLocatorV1.model_validate_json(payload)
    except ValidationError as exc:
        raise NativeRenderError(f"native region locator is malformed: {exc.error_count()} error(s)")


def parse_file_marker(line: str) -> NativeFileMarkerV1 | None:
    """Read the file header marker, or return nothing when the line is ordinary text."""

    stripped = line.strip()
    if not stripped.startswith(FILE_MARKER_PREFIX) or not stripped.endswith(MARKER_SUFFIX):
        return None
    payload = stripped[len(FILE_MARKER_PREFIX) : -len(MARKER_SUFFIX)]
    try:
        return NativeFileMarkerV1.model_validate_json(payload)
    except ValidationError as exc:
        raise NativeRenderError(f"native file marker is malformed: {exc.error_count()} error(s)")


@dataclass(frozen=True)
class NativeRawRegionV1:
    """One region as it currently sits in a file, before anything is believed.

    The body stays out of every model: a region record is a map of where content
    sits, not a second copy of the content, exactly as the coverage overlay
    keeps working bytes out of its records.
    """

    locator: NativeLocatorV1
    body: bytes
    body_digest: str
    start_byte: int
    end_byte: int
    start_line: int
    end_line: int


def extract_regions(
    content: bytes,
    *,
    path: str | None = None,
) -> tuple[
    NativeFileMarkerV1 | None,
    tuple[NativeRawRegionV1, ...],
    tuple[NativeDiagnosticV1, ...],
]:
    """Read the marker channel out of one file's bytes, believing none of it.

    Regions do not nest in this grammar: a region is a leaf field, and an
    opening marker inside an open region is a structural refusal rather than a
    tree to interpret. An unterminated region is likewise refused rather than
    read to end-of-file, because a region whose end the writer never wrote has
    no body anybody can be held to.
    """

    marker: NativeFileMarkerV1 | None = None
    regions: list[NativeRawRegionV1] = []
    diagnostics: list[NativeDiagnosticV1] = []

    offset = 0
    line_number = 0
    open_locator: NativeLocatorV1 | None = None
    body_start = 0
    body_line = 0
    for raw in content.split(b"\n"):
        line_number += 1
        line_start = offset
        offset += len(raw) + 1
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        stripped = text.strip()

        if marker is None and open_locator is None and stripped.startswith(FILE_MARKER_PREFIX):
            try:
                marker = parse_file_marker(text)
            except NativeRenderError as exc:
                diagnostics.append(
                    NativeDiagnosticV1(
                        code="file_marker_malformed",
                        severity="refusal",
                        path=path,
                        message=str(exc),
                    )
                )
            continue

        if stripped == REGION_CLOSE:
            if open_locator is None:
                diagnostics.append(
                    NativeDiagnosticV1(
                        code="region_close_without_open",
                        severity="refusal",
                        path=path,
                        message=f"line {line_number} closes a region that was never opened",
                    )
                )
                continue
            body = content[body_start:line_start]
            regions.append(
                NativeRawRegionV1(
                    locator=open_locator,
                    body=body,
                    body_digest=body_commitment(body),
                    start_byte=body_start,
                    end_byte=body_start + len(body),
                    start_line=body_line,
                    end_line=max(line_number - 1, body_line),
                )
            )
            open_locator = None
            continue

        if stripped.startswith(REGION_OPEN_PREFIX):
            if open_locator is not None:
                diagnostics.append(
                    NativeDiagnosticV1(
                        code="region_nested",
                        severity="refusal",
                        path=path,
                        region_id=open_locator.region_id,
                        message=f"line {line_number} opens a region inside an open region",
                    )
                )
                open_locator = None
                continue
            try:
                open_locator = parse_region_open(text)
            except NativeRenderError as exc:
                diagnostics.append(
                    NativeDiagnosticV1(
                        code="locator_malformed",
                        severity="refusal",
                        path=path,
                        message=str(exc),
                    )
                )
                continue
            body_start = offset
            body_line = line_number + 1

    if open_locator is not None:
        diagnostics.append(
            NativeDiagnosticV1(
                code="region_unterminated",
                severity="refusal",
                path=path,
                region_id=open_locator.region_id,
                message="a region was opened and never closed",
            )
        )

    seen: dict[str, int] = {}
    for region in regions:
        seen[region.locator.region_id] = seen.get(region.locator.region_id, 0) + 1
    for region_id, count in sorted(seen.items()):
        if count > 1:
            diagnostics.append(
                NativeDiagnosticV1(
                    code="locator_duplicated",
                    severity="refusal",
                    path=path,
                    region_id=region_id,
                    message=(
                        f"{count} regions carry one locator; duplicated locators refuse as "
                        "ambiguity and none of them is bound"
                    ),
                )
            )

    return marker, tuple(regions), tuple(diagnostics)


__all__ = [
    "DERIVED_REGENERATION_INSTRUCTION",
    "FILE_MARKER_PREFIX",
    "MARKER_SUFFIX",
    "NATIVE_GRAMMAR_CLASS",
    "NATIVE_LENS_ID",
    "NATIVE_LENS_VERSION",
    "NATIVE_REGION_EDITABLE",
    "NATIVE_REGION_KINDS",
    "REGION_CLOSE",
    "REGION_IDENTITY_DIGEST_DOMAIN",
    "REGION_OPEN_PREFIX",
    "RENDERER_DIGEST_DOMAIN",
    "NativeDiagnosticV1",
    "NativeFileMarkerV1",
    "NativeLensV1",
    "NativeLocatorV1",
    "NativeRawRegionV1",
    "NativeRegionKind",
    "NativeRenderError",
    "body_commitment",
    "default_native_lens",
    "extract_regions",
    "native_renderer_digest",
    "parse_file_marker",
    "parse_region_open",
    "region_identity_digest",
    "render_file_marker",
    "render_region_open",
]
