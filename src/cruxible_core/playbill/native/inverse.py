"""The inverse of the lens on its own output: a render describes itself totally.

§11.9.6's second law -- ``render(parse(render(x)), ctx) == render(x)`` -- is a
statement that the rendered tree loses nothing. Reading it back and emitting it
again has to reproduce the same bytes, or a compile that subtracts a parsed
baseline is subtracting something other than what was written.

What this module proves, exactly
--------------------------------
:func:`native_render_from_tree` reconstructs a whole
:class:`~cruxible_core.playbill.native.lens.NativeRenderV1` -- the manifest and
every file -- from the rendered bytes **alone**, with no accepted state in
scope. Two things follow when the reconstruction equals the render:

* the decomposition is **total**. Every byte of every rendered file is either a
  marker line, a region body line, or prose, and nothing falls between those
  categories. A byte the parser could not account for would be a byte a compile
  could silently drop.
* the marker channel is a **faithful serialization**. Markers are not copied
  across: they are parsed into
  :class:`~cruxible_core.playbill.native.grammar.NativeLocatorV1` and
  :class:`~cruxible_core.playbill.native.grammar.NativeFileMarkerV1` values and
  re-emitted from those values, so the canonical-JSON payload has to round-trip
  exactly. And every per-file baseline in the manifest -- content digest, byte
  length, disposition, region identity, kind, address, artifact digest, body
  digest -- is recomputed from the tree rather than read out of the manifest, so
  a manifest that described bytes other than the ones written cannot reproduce.

What it deliberately does not do is re-derive Claims from prose. Prose is
carried through verbatim, because reversing the *semantic* rendering would be a
second lens coupled to spellings that §11.9 keeps class-3 through the dogfood --
and the semantic direction is already covered by the other four laws: compile of
a clean render is a no-op, and an edit survives compile, acceptance, and
re-render with its payload intact.

Totality is claimed for **clean** trees. A tree whose region structure is broken
-- a nested region, a region opened and never closed, a close with no open --
raises rather than guessing at a reconstruction; that tree is the compiler's
business (it refuses there, with a typed diagnostic), not this law's.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from cruxible_core.playbill.canonical import file_digest
from cruxible_core.playbill.native.context import RenderContextV1
from cruxible_core.playbill.native.grammar import (
    FILE_MARKER_PREFIX,
    REGION_CLOSE,
    REGION_OPEN_PREFIX,
    NativeFileMarkerV1,
    NativeLocatorV1,
    NativeRenderError,
    body_commitment,
    parse_file_marker,
    parse_region_open,
    render_file_marker,
    render_region_open,
)
from cruxible_core.playbill.native.lens import NativeRenderV1, native_source_identity
from cruxible_core.playbill.native.manifest import (
    NATIVE_RENDER_MANIFEST_PATH,
    NativeRegionBaselineV1,
    NativeRenderFileV1,
    NativeRenderManifestV1,
    native_render_digest,
    parse_native_manifest,
    render_native_manifest_bytes,
)
from cruxible_core.playbill.native.sync import render_context_from_manifest
from cruxible_core.playbill.query.grammar import byte_sorted

_CLOSE_BYTES: Final = REGION_CLOSE.encode("utf-8")


@dataclass(frozen=True)
class NativeProseSegmentV1:
    """A run of consecutive lines the lens did not put inside a region.

    Lines are kept as bytes rather than text: prose is carried through
    unchanged, and decoding it would make the round trip a statement about this
    module's encoder instead of about the render.
    """

    lines: tuple[bytes, ...]


@dataclass(frozen=True)
class NativeRegionSegmentV1:
    """One region, as its parsed locator plus the body lines it wrapped."""

    locator: NativeLocatorV1
    lines: tuple[bytes, ...]

    @property
    def body(self) -> bytes:
        """The region body in the same framing the render committed to."""

        return b"\n".join((*self.lines, b"")) if self.lines else b""


NativeSegmentV1 = NativeProseSegmentV1 | NativeRegionSegmentV1


@dataclass(frozen=True)
class NativeFileSourceV1:
    """One rendered file decomposed into everything it is made of, in order."""

    path: str
    marker: NativeFileMarkerV1 | None
    segments: tuple[NativeSegmentV1, ...]

    @property
    def regions(self) -> tuple[NativeRegionSegmentV1, ...]:
        return tuple(item for item in self.segments if isinstance(item, NativeRegionSegmentV1))


def _decoded(line: bytes) -> str | None:
    try:
        return line.decode("utf-8")
    except UnicodeDecodeError:
        return None


def read_native_file_source(path: str, content: bytes) -> NativeFileSourceV1:
    """Decompose one rendered file into markers, regions, and prose, losing nothing.

    Refuses a broken region structure rather than reconstructing over it: a
    nested region, an unterminated region, or a close with no open is not a
    tree this law holds over, and pretending otherwise would produce bytes that
    silently differ from what an author wrote.
    """

    marker: NativeFileMarkerV1 | None = None
    segments: list[NativeSegmentV1] = []
    prose: list[bytes] = []
    open_locator: NativeLocatorV1 | None = None
    body: list[bytes] = []

    def flush_prose() -> None:
        if prose:
            segments.append(NativeProseSegmentV1(lines=tuple(prose)))
            prose.clear()

    for number, line in enumerate(content.split(b"\n"), start=1):
        text = _decoded(line)
        stripped = "" if text is None else text.strip()

        if open_locator is not None:
            if stripped == REGION_CLOSE:
                segments.append(NativeRegionSegmentV1(locator=open_locator, lines=tuple(body)))
                open_locator = None
                body.clear()
            elif stripped.startswith(REGION_OPEN_PREFIX):
                raise NativeRenderError(
                    f"{path} line {number} opens a region inside an open region; "
                    "regions are leaf fields and do not nest"
                )
            else:
                body.append(line)
            continue

        if stripped == REGION_CLOSE:
            raise NativeRenderError(f"{path} line {number} closes a region that was never opened")
        if stripped.startswith(REGION_OPEN_PREFIX) and text is not None:
            flush_prose()
            open_locator = parse_region_open(text)
            if open_locator is None:  # pragma: no cover - the prefix matched above
                raise NativeRenderError(f"{path} line {number} is not a readable region locator")
            continue
        if marker is None and stripped.startswith(FILE_MARKER_PREFIX) and text is not None:
            marker = parse_file_marker(text)
            if marker is not None:
                continue
        prose.append(line)

    if open_locator is not None:
        raise NativeRenderError(f"{path} opens a region that is never closed")
    flush_prose()
    return NativeFileSourceV1(path=path, marker=marker, segments=tuple(segments))


def emit_native_file_source(source: NativeFileSourceV1) -> bytes:
    """Re-emit one decomposed file, rebuilding every marker from its parsed value."""

    lines: list[bytes] = []
    if source.marker is not None:
        lines.append(render_file_marker(source.marker).encode("utf-8"))
    for segment in source.segments:
        if isinstance(segment, NativeProseSegmentV1):
            lines.extend(segment.lines)
            continue
        lines.append(render_region_open(segment.locator).encode("utf-8"))
        lines.extend(segment.lines)
        lines.append(_CLOSE_BYTES)
    return b"\n".join(lines)


def _render_file_entry(
    source: NativeFileSourceV1,
    content: bytes,
    *,
    ctx: RenderContextV1,
) -> NativeRenderFileV1:
    """Rebuild one manifest entry from the file's own markers and bodies."""

    marker = source.marker
    if marker is None:
        raise NativeRenderError(
            f"{source.path} carries no file marker; a rendered file always declares its lens"
        )
    if marker.path != source.path:
        raise NativeRenderError(
            f"{source.path} carries a marker for {marker.path}; a moved file is a working "
            "tree state, not a render this law reconstructs"
        )
    baselines: list[NativeRegionBaselineV1] = []
    for region in source.regions:
        digest = body_commitment(region.body)
        if digest != region.locator.baseline_digest:
            raise NativeRenderError(
                f"region {region.locator.region_id} in {source.path} no longer holds the bytes "
                "its own locator commits to; that is an edited tree, not a fresh render"
            )
        baselines.append(
            NativeRegionBaselineV1(
                region_id=region.locator.region_id,
                region_kind=region.locator.region_kind,
                editable=region.locator.editable,
                address=region.locator.address,
                artifact_digest=region.locator.artifact_digest,
                body_digest=digest,
                byte_length=len(region.body),
            )
        )
    return NativeRenderFileV1(
        path=source.path,
        content_digest=file_digest(content).tagged,
        byte_length=len(content),
        disposition=marker.disposition,
        source=native_source_identity(source.path, ctx=ctx),
        regions=tuple(sorted(baselines, key=lambda item: item.sort_key)),
    )


def native_render_from_tree(files: Mapping[str, bytes]) -> NativeRenderV1:
    """Recover a whole render from the bytes it wrote, with no accepted state.

    The manifest's *context* fields -- instance, generation, boundary, lens,
    scope, evaluation time, roots, entrypoints -- are read back out of the
    committed manifest, which is itself part of the render and is re-serialized
    here rather than copied. Everything the manifest says about *bytes* is
    recomputed from the files, so the two halves have to agree for the
    reconstruction to reproduce.
    """

    committed = files.get(NATIVE_RENDER_MANIFEST_PATH)
    if committed is None:
        raise NativeRenderError(
            f"a rendered tree carries its baseline in {NATIVE_RENDER_MANIFEST_PATH}; "
            "this one has none"
        )
    manifest = parse_native_manifest(committed)
    ctx = render_context_from_manifest(manifest)

    rebuilt: dict[str, bytes] = {}
    entries: list[NativeRenderFileV1] = []
    for path in byte_sorted(tuple(item for item in files if item.endswith(".md"))):
        source = read_native_file_source(path, files[path])
        content = emit_native_file_source(source)
        rebuilt[path] = content
        entries.append(_render_file_entry(source, content, ctx=ctx))

    inventory = tuple(sorted(entries, key=lambda item: item.path.encode("utf-8")))
    recovered = NativeRenderManifestV1.model_validate(
        {
            **manifest.model_dump(mode="json"),
            "files": [item.model_dump(mode="json") for item in inventory],
            "render_digest": native_render_digest(inventory),
        }
    )
    return NativeRenderV1(
        manifest=recovered,
        files={NATIVE_RENDER_MANIFEST_PATH: render_native_manifest_bytes(recovered), **rebuilt},
    )


__all__ = [
    "NativeFileSourceV1",
    "NativeProseSegmentV1",
    "NativeRegionSegmentV1",
    "NativeSegmentV1",
    "emit_native_file_source",
    "native_render_from_tree",
    "read_native_file_source",
]
