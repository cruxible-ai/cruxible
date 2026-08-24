"""The stash: "compile it or stash it" instead of "compile it or lose it".

§11.9.5 says a re-render never overwrites dirty regions without an explicit
stash or discard. S2 shipped the refusal and the discard; discard is the honest
minimum and a poor default, because the material at risk is exactly the material
a person has just written and has not yet decided how to propose. This module
carries those bytes somewhere instead of dropping them, and it changes nothing
about the refusal: a bare re-render still refuses, and stashing is as explicit an
act as discarding.

A local format, and nothing more
--------------------------------
A stash entry is **not** accepted state, not a proposal, and not a second
authority over anything. It is a rebuildable-cache-shaped record in the same
family as the replay checkpoint and the coverage manifest: a digest-committed
body, written atomically by the caller, superseded by rewriting rather than
migrated, and disposable. Deleting the stash directory loses exactly the local
edits somebody chose to stash and nothing else -- the same loss as deleting the
rendered tree, which §11.9 already makes the whole risk surface of a render.

Nothing here touches a filesystem. §11.9.5's explicit-sync law is structural in
this package rather than promised, so the stash module produces and consumes
bytes and the CLI writes them, exactly as the lens produces a tree and the CLI
writes that.

Restoring is by identity, never by position
-------------------------------------------
A stashed region is re-applied to whichever region carries its identity in the
tree *now*. Region identity is path-free (§11.9.3: paths are presentation
coordinates), so a restore lands correctly across a file move or rename, and a
restore whose region is gone -- or is present but no longer binds unambiguously
-- is **reported** rather than guessed at. There is no positional fallback,
because a byte offset from an older render is a coincidence and not a location.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping, Sequence
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_client.contracts.query.grammar import byte_sorted
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_core.playbill.native.grammar import (
    NativeDiagnosticV1,
    NativeLensV1,
    NativeRegionKind,
    NativeRenderError,
    body_commitment,
)
from cruxible_core.playbill.native.manifest import NativeRenderManifestV1
from cruxible_core.playbill.native.parse import NativeTreeParseV1, parse_native_tree
from cruxible_core.playbill.projection import AcceptedCoordinate

NATIVE_STASH_DIRECTORY: Final = ".playbill-stash"
"""Where a caller keeps stash entries, relative to the render root.

Under the render root rather than the instance's managed root, because the
material is the *repository's*: a render may be produced against a daemon the
CLI cannot write to at all, and edits to a checkout belong beside the checkout.
The dot prefix keeps it out of the rendered tree the manifest describes."""

NATIVE_STASH_FILE_PREFIX: Final = "stash-"
NATIVE_STASH_TAG: Final = "playbill-native-stash-v1"

_MINIMUM_STASH_PREFIX: Final = 8


class NativeStashDigest(Sha256Value):
    """The self-digest of one stash body.

    Declared here rather than beside the wire digest kinds, precisely so a later
    reader cannot mistake a disposable local record for accepted material.
    """

    kind = "native stash digest"


class NativeStashError(NativeRenderError):
    """A stash could not be captured, read, or resolved."""


class _StrictStashModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NativeStashRegionV1(_StrictStashModel):
    """One dirty region's bytes, with everything needed to put them back.

    ``path`` is the presentation coordinate the region sat at when it was
    stashed. It is recorded so a person reading a stash can see where the edit
    was, and it is deliberately *not* how a restore finds its target: identity
    is.
    """

    tag: Literal["playbill-native-stash-region-v1"] = "playbill-native-stash-region-v1"
    region_id: str
    region_kind: NativeRegionKind
    path: str
    address: SemanticAddress
    baseline_digest: str
    body_digest: str
    byte_length: int = Field(ge=0)
    body_base64: str

    @model_validator(mode="after")
    def _region_law(self) -> "NativeStashRegionV1":
        for value in (self.region_id, self.baseline_digest, self.body_digest):
            Sha256Value.from_tagged(value)
        try:
            body = base64.b64decode(self.body_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("a stashed region body must be exact base64") from exc
        if len(body) != self.byte_length:
            raise ValueError("a stashed region body must be the length it declares")
        if body_commitment(body) != self.body_digest:
            raise ValueError("a stashed region body must reproduce its own digest")
        return self

    @property
    def body(self) -> bytes:
        return base64.b64decode(self.body_base64, validate=True)

    @property
    def sort_key(self) -> bytes:
        return self.region_id.encode("ascii")


class NativeStashBodyV1(_StrictStashModel):
    """One stash: the dirty regions of one render, at the generation it was cut."""

    tag: Literal["playbill-native-stash-v1"] = "playbill-native-stash-v1"
    instance_id: str
    at: AcceptedCoordinate
    lens: NativeLensV1
    render_digest: str
    regions: tuple[NativeStashRegionV1, ...]

    @field_validator("regions")
    @classmethod
    def _regions(cls, value: tuple[NativeStashRegionV1, ...]) -> tuple[NativeStashRegionV1, ...]:
        if not value:
            raise ValueError("a stash holds at least one region; there is no empty stash")
        keys = tuple(item.sort_key for item in value)
        if keys != tuple(sorted(set(keys))):
            raise ValueError("stashed regions must be sorted and unique by region identity")
        return value

    @model_validator(mode="after")
    def _stash_law(self) -> "NativeStashBodyV1":
        Sha256Value.from_tagged(self.render_digest)
        return self

    @property
    def region_ids(self) -> tuple[str, ...]:
        return tuple(item.region_id for item in self.regions)


class NativeStashFileV1(_StrictStashModel):
    """The on-disk record: one digest-committed body plus non-committed metadata."""

    tag: Literal["playbill-native-stash-file-v1"] = "playbill-native-stash-file-v1"
    body: NativeStashBodyV1
    stash_digest: str
    written_at: str

    @field_validator("stash_digest")
    @classmethod
    def _stash_digest(cls, value: str) -> str:
        NativeStashDigest.from_tagged(value)
        return value

    @property
    def stash_id(self) -> str:
        return self.stash_digest


class NativeStashRestoreV1(_StrictStashModel):
    """What re-applying one stash would put back, and what it refuses to guess."""

    tag: Literal["playbill-native-stash-restore-v1"] = "playbill-native-stash-restore-v1"
    stash_digest: str
    restored_region_ids: tuple[str, ...] = ()
    unresolved_region_ids: tuple[str, ...] = ()
    write_paths: tuple[str, ...] = ()
    files: dict[str, bytes] = Field(default_factory=dict)
    diagnostics: tuple[NativeDiagnosticV1, ...] = ()

    @field_validator("restored_region_ids", "unresolved_region_ids", "write_paths")
    @classmethod
    def _sorted(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != byte_sorted(value):
            raise ValueError("stash restore lists must be sorted and unique")
        return value


def native_stash_digest(body: NativeStashBodyV1) -> NativeStashDigest:
    """Digest exactly the body, so the written record commits to what it holds."""

    return typed_digest(
        NativeStashDigest,
        NATIVE_STASH_TAG,
        {key: value for key, value in body.model_dump(mode="json").items() if key != "tag"},
    )


def render_native_stash(body: NativeStashBodyV1, *, written_at: str = "") -> bytes:
    """Serialize one stash entry the way every other local record is written.

    ``written_at`` sits outside the digest preimage, so stashing the same edits
    twice produces the same identity rather than two entries that differ only in
    when somebody typed the command.
    """

    record = NativeStashFileV1(
        body=body,
        stash_digest=native_stash_digest(body).tagged,
        written_at=written_at,
    )
    return canonical_bytes(record.model_dump(mode="json")) + b"\n"


def parse_native_stash(content: bytes) -> NativeStashFileV1:
    """Read one stash entry, refusing one whose digest does not reproduce."""

    try:
        record = NativeStashFileV1.model_validate_json(content)
    except ValueError as exc:
        raise NativeStashError(f"a native stash entry is malformed: {exc}") from exc
    if record.stash_digest != native_stash_digest(record.body).tagged:
        raise NativeStashError(
            "a native stash entry does not reproduce its own digest; it is not read"
        )
    return record


def native_stash_entry_path(stash_digest: str) -> str:
    """The relative path one stash entry occupies under the render root."""

    value = NativeStashDigest.from_tagged(stash_digest)
    return f"{NATIVE_STASH_DIRECTORY}/{NATIVE_STASH_FILE_PREFIX}{value.value}.json"


def native_stash_body(
    files: Mapping[str, bytes],
    *,
    manifest: NativeRenderManifestV1,
    parsed: NativeTreeParseV1 | None = None,
) -> NativeStashBodyV1 | None:
    """Capture every dirty region's bytes, or return nothing when none is dirty.

    Only ``dirty`` regions are captured, and that is the whole selection rule. A
    tampered derived region has nothing to preserve -- it regenerates by
    definition -- and an ambiguous or unbaselined region binds nothing, so
    keeping its bytes would be keeping bytes nobody could put back.
    """

    tree = parsed or parse_native_tree(files, manifest=manifest)
    regions: list[NativeStashRegionV1] = []
    for region in tree.regions:
        if region.state != "dirty" or region.baseline_digest is None:
            continue
        content = files.get(region.path)
        if content is None:  # pragma: no cover - the parse read this file to find the region
            raise NativeStashError(f"dirty region {region.region_id} names an unreadable file")
        body = content[region.line_overlay.start_byte : region.line_overlay.end_byte]
        regions.append(
            NativeStashRegionV1(
                region_id=region.region_id,
                region_kind=region.region_kind,
                path=region.path,
                address=region.address,
                baseline_digest=region.baseline_digest,
                body_digest=body_commitment(body),
                byte_length=len(body),
                body_base64=base64.b64encode(body).decode("ascii"),
            )
        )
    if not regions:
        return None
    return NativeStashBodyV1(
        instance_id=manifest.instance_id,
        at=manifest.coordinate,
        lens=manifest.lens,
        render_digest=manifest.render_digest,
        regions=tuple(sorted(regions, key=lambda item: item.sort_key)),
    )


def resolve_native_stash(
    stashes: Sequence[NativeStashFileV1],
    stash_id: str,
) -> NativeStashFileV1:
    """Find one stash by its full digest or by an unambiguous hexadecimal prefix.

    An ambiguous prefix refuses and names the entries it matched. A local
    convenience may shorten an identifier; it may not choose between two records
    on the operator's behalf.
    """

    wanted = stash_id.strip()
    if wanted.startswith(f"{NativeStashDigest.algorithm}:"):
        wanted = wanted.split(":", 1)[1]
    wanted = wanted.lower()
    if len(wanted) < _MINIMUM_STASH_PREFIX:
        raise NativeStashError(
            f"a stash identifier needs at least {_MINIMUM_STASH_PREFIX} hexadecimal characters"
        )
    matched = [
        item
        for item in stashes
        if NativeStashDigest.from_tagged(item.stash_digest).value.startswith(wanted)
    ]
    if not matched:
        raise NativeStashError(f"no stash entry matches {stash_id}")
    if len(matched) > 1:
        named = ", ".join(sorted(item.stash_digest for item in matched))
        raise NativeStashError(f"{stash_id} matches more than one stash entry: {named}")
    return matched[0]


def restore_native_stash(
    files: Mapping[str, bytes],
    *,
    manifest: NativeRenderManifestV1,
    stash: NativeStashFileV1,
    parsed: NativeTreeParseV1 | None = None,
) -> NativeStashRestoreV1:
    """Re-apply stashed region bodies onto the tree as it is now, by identity.

    Splices run from the end of each file backwards, so an earlier restore in
    the same file cannot move the window a later one was measured against.
    """

    tree = parsed or parse_native_tree(files, manifest=manifest)
    present = {item.region_id: item for item in tree.regions}
    diagnostics: list[NativeDiagnosticV1] = []
    edits: dict[str, list[tuple[int, int, bytes]]] = {}
    restored: list[str] = []
    unresolved: list[str] = []

    for entry in stash.body.regions:
        region = present.get(entry.region_id)
        if region is None or region.path not in files:
            unresolved.append(entry.region_id)
            diagnostics.append(
                NativeDiagnosticV1(
                    code="stash_region_absent",
                    severity="notice",
                    path=entry.path,
                    region_id=entry.region_id,
                    message=(
                        "this stashed field is not in the current render; the edit is kept in "
                        "the stash rather than placed somewhere it might not belong"
                    ),
                )
            )
            continue
        if region.state not in {"clean", "dirty"}:
            unresolved.append(entry.region_id)
            diagnostics.append(
                NativeDiagnosticV1(
                    code="stash_region_not_bound",
                    severity="refusal",
                    path=region.path,
                    region_id=entry.region_id,
                    message=(
                        f"this stashed field is {region.state} in the current tree and binds "
                        "nothing; repair the region before restoring onto it"
                    ),
                )
            )
            continue
        edits.setdefault(region.path, []).append(
            (region.line_overlay.start_byte, region.line_overlay.end_byte, entry.body)
        )
        restored.append(entry.region_id)

    written: dict[str, bytes] = {}
    for path, spans in edits.items():
        content = files[path]
        for start, end, body in sorted(spans, key=lambda item: item[0], reverse=True):
            content = content[:start] + body + content[end:]
        if content != files[path]:
            written[path] = content

    return NativeStashRestoreV1(
        stash_digest=stash.stash_digest,
        restored_region_ids=byte_sorted(tuple(restored)),
        unresolved_region_ids=byte_sorted(tuple(unresolved)),
        write_paths=byte_sorted(tuple(written)),
        files=written,
        diagnostics=tuple(diagnostics),
    )


__all__ = [
    "NATIVE_STASH_DIRECTORY",
    "NATIVE_STASH_FILE_PREFIX",
    "NATIVE_STASH_TAG",
    "NativeStashBodyV1",
    "NativeStashDigest",
    "NativeStashError",
    "NativeStashFileV1",
    "NativeStashRegionV1",
    "NativeStashRestoreV1",
    "native_stash_body",
    "native_stash_digest",
    "native_stash_entry_path",
    "parse_native_stash",
    "render_native_stash",
    "resolve_native_stash",
    "restore_native_stash",
]
