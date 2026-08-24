"""The default in-repo render lens: accepted state as a browsable working tree.

§11.9.1 promotes the committed render of Playbill-native knowledge from a lookup
index to the **primary greppable discovery surface**. That is a demand about
prose quality, not only about determinism: an agent or a person who greps this
tree should find a readable document about a Subject, not a serialized record
with a heading on top. So Claims are grouped where they are actually about
something -- under the Subject they are made of, by predicate -- and the
locators that bind each region to accepted state sit in an invisible channel
that no Markdown renderer shows.

Rendering adds no authority
---------------------------
This module holds no write path. It reads a
:class:`~cruxible_core.playbill.native.state.NativeAcceptedStateV1` value and a
:class:`~cruxible_core.playbill.native.context.RenderContextV1` and returns
bytes. It does not write the bytes either: §11.9.5's explicit-sync law makes
writing the caller's separate act, so a daemon that can compute a render still
structurally cannot commit one into a user's repository.

Determinism, concretely
-----------------------
Every collection is byte-sorted, every time comes from the context, and no
identifier is allocated. Two calls with the same state and context produce
byte-identical files, which is the §11.9.6 render-determinism law; and because
region bodies are emitted exactly as the parser slices them back out,
``render(parse(render(x)))`` reproduces ``render(x)``.

The spellings below -- headings, bullet shapes, the "verdict at render" line --
are class-3 experimental and expected to move with dogfood. The **laws** they
serve are not: a governance fact is never rendered as an unqualified badge, a
derived region always says it regenerates, and an editable region is free-form
only inside itself.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from cruxible_client.contracts.canonical import Sha256Value, canonical_bytes, typed_digest
from cruxible_client.contracts.claims import (
    ClaimObject,
    ExactContentClaimObject,
    LiteralClaimObject,
    SubjectClaimObject,
)
from cruxible_client.contracts.query.grammar import byte_sorted
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_core.playbill.coverage.contracts import LogicalSourceIdentityV1
from cruxible_core.playbill.native.context import RenderContextV1
from cruxible_core.playbill.native.grammar import (
    DERIVED_REGENERATION_INSTRUCTION,
    DRAFT_MARKER_PREFIX,
    FILE_MARKER_PREFIX,
    NATIVE_REGION_EDITABLE,
    REGION_CLOSE,
    REGION_OPEN_PREFIX,
    NativeFileMarkerV1,
    NativeLocatorV1,
    NativeRenderError,
    body_commitment,
    region_identity_digest,
    render_file_marker,
    render_region_open,
)
from cruxible_core.playbill.native.manifest import (
    NATIVE_RENDER_MANIFEST_PATH,
    NativeFileDisposition,
    NativeRegionBaselineV1,
    NativeRenderFileV1,
    NativeRenderManifestV1,
    native_render_digest,
    render_native_manifest_bytes,
)
from cruxible_core.playbill.native.state import (
    NativeAcceptedStateV1,
    NativeArtifactRecordV1,
    NativeClaimRecordV1,
)

README_PATH: Final = "README.md"
NATIVE_SOURCE_DIGEST_DOMAIN: Final = "playbill-native-render-source-v1"

NONE_TEXT: Final = "(none)"
UNBOUNDED_TEXT: Final = "(unbounded)"
DERIVED_NOTE: Final = "_Derived from accepted state: regenerated on re-render, never edited._"
EDITABLE_NOTE: Final = (
    "_Editable: free-form inside this field; compile proposes, editing does not._"
)


def native_source_identity(path: str, *, ctx: RenderContextV1) -> LogicalSourceIdentityV1:
    """Declare the logical source one rendered file is observed under.

    Coverage binds a working path to a logical source **by declaration**
    (§11.6.1); it never infers one from a filename. The render is the side that
    knows both, so it declares the binding here and records it in the manifest.
    The identity is a digest rather than the path itself because an external
    source identity is required to be logical and locator-free -- naming the
    checkout path inside it would put a locator in accepted-shaped material.
    """

    key = typed_digest(
        Sha256Value,
        NATIVE_SOURCE_DIGEST_DOMAIN,
        {"instance_id": ctx.instance_id, "path": path, "scope_digest": ctx.scope_digest},
    ).value
    return LogicalSourceIdentityV1(plane="external", identity=f"native.render.{key}")


def _render_path(ledger_path: str) -> str:
    """Map one ledger artifact path to its presentation coordinate in the tree."""

    stem = ledger_path
    for suffix in (".yaml", ".yml", ".json"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    if not stem or ".." in stem.split("/") or stem.startswith("/"):
        raise NativeRenderError(f"accepted artifact path is not renderable: {ledger_path}")
    return f"{stem}.md"


def _reject_marker_collision(lines: Sequence[str], *, path: str) -> None:
    for line in lines:
        stripped = line.strip()
        if (
            stripped == REGION_CLOSE
            or stripped.startswith(REGION_OPEN_PREFIX)
            or stripped.startswith(FILE_MARKER_PREFIX)
            or stripped.startswith(DRAFT_MARKER_PREFIX)
        ):
            raise NativeRenderError(
                f"rendered content in {path} would collide with the marker channel; "
                "refusing to emit a region whose body could be read as a marker"
            )


@dataclass
class _FileBuilder:
    """Accumulate one rendered file's bytes and its region baselines together.

    Bodies are committed to as they are emitted, so a baseline digest can never
    describe bytes other than the ones written -- there is no second pass in
    which the two could diverge.
    """

    path: str
    disposition: NativeFileDisposition
    ctx: RenderContextV1
    parts: list[str] = field(default_factory=list)
    regions: list[NativeRegionBaselineV1] = field(default_factory=list)

    def __post_init__(self) -> None:
        marker = NativeFileMarkerV1(
            lens_id=self.ctx.lens.lens_id,
            lens_version=self.ctx.lens.lens_version,
            path=self.path,
            disposition=self.disposition,
            generation_root=self.ctx.at.generation_root,
            evaluation_time=self.ctx.evaluation_time_text,
            scope_digest=self.ctx.scope_digest,
        )
        self.parts.append(render_file_marker(marker) + "\n")

    def text(self, *lines: str) -> None:
        _reject_marker_collision(lines, path=self.path)
        self.parts.append("\n".join(lines) + "\n")

    def region(
        self,
        *,
        kind: str,
        address: SemanticAddress,
        artifact_digest: str,
        lines: Sequence[str],
        ordinal: int = 0,
    ) -> None:
        _reject_marker_collision(lines, path=self.path)
        body = ("\n".join(lines) + "\n") if lines else ""
        body_bytes = body.encode("utf-8")
        digest = body_commitment(body_bytes)
        region_id = region_identity_digest(
            address=address,
            region_kind=kind,
            ordinal=ordinal,
            lens=self.ctx.lens,
        )
        locator = NativeLocatorV1(
            lens_id=self.ctx.lens.lens_id,
            lens_version=self.ctx.lens.lens_version,
            region_id=region_id,
            region_kind=kind,  # type: ignore[arg-type]
            editable=NATIVE_REGION_EDITABLE[kind],
            address=address,
            artifact_digest=artifact_digest,
            generation_root=self.ctx.at.generation_root,
            baseline_digest=digest,
        )
        self.parts.append(render_region_open(locator) + "\n" + body + REGION_CLOSE + "\n")
        self.regions.append(
            NativeRegionBaselineV1(
                region_id=region_id,
                region_kind=kind,  # type: ignore[arg-type]
                editable=locator.editable,
                address=address,
                artifact_digest=artifact_digest,
                body_digest=digest,
                byte_length=len(body_bytes),
            )
        )

    def finish(self) -> tuple[bytes, NativeRenderFileV1]:
        content = "".join(self.parts).encode("utf-8")
        return content, NativeRenderFileV1(
            path=self.path,
            content_digest=Sha256Value(hashlib.sha256(content).hexdigest()).tagged,
            byte_length=len(content),
            disposition=self.disposition,
            source=native_source_identity(self.path, ctx=self.ctx),
            regions=tuple(sorted(self.regions, key=lambda item: item.sort_key)),
        )


# -- region bodies ---------------------------------------------------------


def _object_lines(value: ClaimObject) -> list[str]:
    if isinstance(value, LiteralClaimObject):
        return [canonical_bytes(value.value).decode("utf-8")]
    if isinstance(value, SubjectClaimObject):
        return [value.address.artifact_path]
    if isinstance(value, ExactContentClaimObject):
        if value.span is None:
            return [value.content_digest]
        return [f"{value.content_digest} bytes {value.span.start_byte}-{value.span.end_byte}"]
    raise NativeRenderError("unknown Claim object kind in the native lens")


def _governance_lines(record: NativeClaimRecordV1, *, ctx: RenderContextV1) -> list[str]:
    """Render governance generation- and time-qualified, never as a badge.

    §11.9.2 forbids an unqualified badge that can sit in Git indefinitely, so
    every line here carries the instant the verdict was evaluated at *and* the
    generation it was accepted in. Those are two different times from the
    render's own read time, and all three are stated rather than collapsed.
    """

    generation = ctx.at.generation_root
    if record.verdict is None:
        return [
            f"verdict at render: not projected · read at {ctx.evaluation_time_text} "
            f"· generation {generation}",
            "This accepted read projected no verdict; that is an absence, not a refusal.",
        ]
    evaluated = record.verdict.evaluation_time.isoformat()
    lines = [
        f"verdict at render: {record.verdict.verdict} · evaluated at {evaluated} "
        f"· generation {generation}",
        f"currency at render: {record.verdict.currency} · evaluated at {evaluated} "
        f"· generation {generation}",
        f"read at: {ctx.evaluation_time_text}",
    ]
    if record.verdict.basis_kinds:
        lines.append("evidence basis: " + ", ".join(record.verdict.basis_kinds))
    if record.verdict.refusal_codes:
        lines.append("refusal codes: " + ", ".join(record.verdict.refusal_codes))
    return lines


def _provenance_lines(record: NativeClaimRecordV1) -> list[str]:
    claim = record.claim
    effective_from = (
        UNBOUNDED_TEXT
        if claim.statement.effective_from is None
        else claim.statement.effective_from.isoformat()
    )
    effective_until = (
        UNBOUNDED_TEXT
        if claim.statement.effective_until is None
        else claim.statement.effective_until.isoformat()
    )
    return [
        f"- claim: `{claim.identity.qualified}`",
        f"- ledger artifact: `{record.path}`",
        f"- artifact digest: `{record.artifact_digest}`",
        f"- role: {claim.statement.role}",
        f"- lifecycle: {claim.lifecycle.state}",
        f"- effective from: {effective_from}",
        f"- effective until: {effective_until}",
        "- captures: "
        + (", ".join(f"`{item}`" for item in claim.backing.capture_digests) or NONE_TEXT),
        "- attestations: "
        + (", ".join(f"`{item}`" for item in claim.backing.attestation_digests) or NONE_TEXT),
        "- input claims: "
        + (", ".join(f"`{item}`" for item in claim.backing.input_claim_digests) or NONE_TEXT),
        "- pins: "
        + (", ".join(f"{pin.role}={pin.target.qualified}" for pin in claim.pins) or NONE_TEXT),
    ]


def _coverage_lines(state: NativeAcceptedStateV1, *, ctx: RenderContextV1) -> list[str]:
    boundary = state.boundary
    return [
        f"- coverage boundary: {boundary.completeness}",
        f"- evidence index: `{boundary.index_digest}`",
        f"- access profile: `{boundary.access_profile_id}`",
        f"- declared sources: {len(boundary.scope)}",
        "- truncation: " + (", ".join(boundary.truncation_reason_codes) or NONE_TEXT),
        f"- generation: `{ctx.at.generation_root}`",
        f"- read at: {ctx.evaluation_time_text}",
        "",
        "A `none` is factual only inside a complete boundary; this line says which",
        "boundary the answer was computed over, never that anything is globally",
        "ungoverned.",
    ]


def _structure_lines(record: NativeArtifactRecordV1, *, extra: Sequence[str] = ()) -> list[str]:
    return [
        f"- identity: `{record.identity}`",
        f"- ledger artifact: `{record.path}`",
        f"- artifact digest: `{record.artifact_digest}`",
        *extra,
    ]


# -- whole files -----------------------------------------------------------


def _preamble(builder: _FileBuilder) -> None:
    builder.text(
        "",
        "The ledger is the semantic object store; this directory is its editable",
        "working tree. Editing a field here is a local draft and changes nothing",
        "accepted until it is compiled into a proposal and that proposal is accepted.",
        "Deleting this file loses uncompiled local edits and nothing else.",
        "",
    )


def _claim_sections(
    builder: _FileBuilder,
    records: Sequence[NativeClaimRecordV1],
    *,
    ctx: RenderContextV1,
) -> None:
    by_predicate: dict[str, list[NativeClaimRecordV1]] = {}
    for record in records:
        by_predicate.setdefault(record.claim.statement.predicate, []).append(record)
    for predicate in sorted(by_predicate, key=lambda item: item.encode("utf-8")):
        builder.text(f"## {predicate}", "")
        for record in sorted(by_predicate[predicate], key=lambda item: item.path.encode("utf-8")):
            address = record.address
            digest = record.artifact_digest
            builder.text(f"### Claim `{record.claim.identity.name}`", "", EDITABLE_NOTE, "")
            builder.text("#### Value", "")
            builder.region(
                kind="statement_value",
                address=address,
                artifact_digest=digest,
                lines=_object_lines(record.claim.statement.object),
            )
            builder.text("", "#### Qualifier", "")
            builder.region(
                kind="statement_qualifier",
                address=address,
                artifact_digest=digest,
                lines=[record.claim.statement.qualifier or NONE_TEXT],
            )
            builder.text("", "#### Governance", "", DERIVED_NOTE, "")
            builder.region(
                kind="governance",
                address=address,
                artifact_digest=digest,
                lines=_governance_lines(record, ctx=ctx),
            )
            builder.text("", "#### Provenance", "", DERIVED_NOTE, "")
            builder.region(
                kind="provenance",
                address=address,
                artifact_digest=digest,
                lines=_provenance_lines(record),
            )
            builder.text("")


def _coverage_section(
    builder: _FileBuilder,
    state: NativeAcceptedStateV1,
    *,
    ctx: RenderContextV1,
    address: SemanticAddress,
    artifact_digest: str,
) -> None:
    builder.text("## Rendered at", "", DERIVED_NOTE, "")
    builder.region(
        kind="coverage",
        address=address,
        artifact_digest=artifact_digest,
        lines=_coverage_lines(state, ctx=ctx),
    )
    builder.text("")


def _subject_file(
    state: NativeAcceptedStateV1,
    ctx: RenderContextV1,
    record: NativeArtifactRecordV1,
) -> tuple[bytes, NativeRenderFileV1]:
    builder = _FileBuilder(path=_render_path(record.path), disposition="native_editable", ctx=ctx)
    name = record.identity.removeprefix("Subject:")
    builder.text(f"# Subject — {name}")
    _preamble(builder)
    builder.text("## Identity", "", DERIVED_NOTE, "")
    builder.region(
        kind="structure",
        address=record.address,
        artifact_digest=record.artifact_digest,
        lines=_structure_lines(record),
    )
    builder.text("")
    _coverage_section(
        builder,
        state,
        ctx=ctx,
        address=record.address,
        artifact_digest=record.artifact_digest,
    )
    claims = state.claims_for_subject(record.path)
    if claims:
        _claim_sections(builder, claims, ctx=ctx)
    else:
        builder.text(
            "## Claims", "", "No accepted Claim names this Subject at this generation.", ""
        )
    return builder.finish()


def _claim_file(
    state: NativeAcceptedStateV1,
    ctx: RenderContextV1,
    record: NativeClaimRecordV1,
) -> tuple[bytes, NativeRenderFileV1]:
    """Render one Claim whose Subject is not itself rendered in this scope.

    Grouping Claims under their Subject is the natural human shape, so this file
    exists only for the Claims that have no rendered Subject to sit under. It is
    a fallback that keeps the tree total, not a second home for anything.
    """

    builder = _FileBuilder(path=_render_path(record.path), disposition="native_editable", ctx=ctx)
    builder.text(f"# Claim — {record.claim.identity.name}")
    _preamble(builder)
    builder.text(
        "This Claim's Subject is not rendered in this scope, so it is rendered on",
        f"its own page. Its Subject address is `{record.claim.statement.subject.artifact_path}`.",
        "",
    )
    _coverage_section(
        builder,
        state,
        ctx=ctx,
        address=record.address,
        artifact_digest=record.artifact_digest,
    )
    _claim_sections(builder, (record,), ctx=ctx)
    return builder.finish()


def _artifact_file(
    state: NativeAcceptedStateV1,
    ctx: RenderContextV1,
    record: NativeArtifactRecordV1,
    *,
    title: str,
    extra: Sequence[str],
    body: Sequence[str],
) -> tuple[bytes, NativeRenderFileV1]:
    builder = _FileBuilder(path=_render_path(record.path), disposition="native_editable", ctx=ctx)
    builder.text(f"# {title}")
    _preamble(builder)
    if body:
        builder.text(*body, "")
    builder.text("## Contract", "", DERIVED_NOTE, "")
    builder.region(
        kind="structure",
        address=record.address,
        artifact_digest=record.artifact_digest,
        lines=_structure_lines(record, extra=extra),
    )
    builder.text("")
    _coverage_section(
        builder,
        state,
        ctx=ctx,
        address=record.address,
        artifact_digest=record.artifact_digest,
    )
    return builder.finish()


def _claim_type_extra(record: NativeArtifactRecordV1) -> list[str]:
    envelope = record.envelope
    roles = envelope.get("permitted_roles") or ()
    kinds = envelope.get("allowed_subject_kinds") or ()
    return [
        f"- predicate: `{envelope.get('predicate', '')}`",
        f"- object kind: {envelope.get('object_kind', NONE_TEXT)}",
        f"- cardinality: {envelope.get('cardinality', NONE_TEXT)}",
        "- allowed subject kinds: " + (", ".join(str(item) for item in kinds) or NONE_TEXT),
        "- permitted roles: " + (", ".join(str(item) for item in roles) or NONE_TEXT),
    ]


def _query_extra(record: NativeArtifactRecordV1) -> list[str]:
    envelope = record.envelope
    entry = envelope.get("entry") or {}
    subject_kinds = entry.get("subject_kinds") if isinstance(entry, dict) else ()
    return [
        f"- result shape: {envelope.get('result_shape', NONE_TEXT)}",
        f"- result cardinality: {envelope.get('result_cardinality', NONE_TEXT)}",
        "- entry subject kinds: "
        + (", ".join(str(item) for item in (subject_kinds or ())) or NONE_TEXT),
    ]


def _document_extra(record: NativeArtifactRecordV1) -> list[str]:
    envelope = record.envelope
    return [
        f"- document kind: {envelope.get('document_kind', NONE_TEXT)}",
        f"- title: {envelope.get('title', NONE_TEXT)}",
        f"- media type: {envelope.get('media_type', NONE_TEXT)}",
        f"- body digest: `{envelope.get('body_digest', NONE_TEXT)}`",
    ]


def _readme(
    state: NativeAcceptedStateV1,
    ctx: RenderContextV1,
    *,
    roots: Sequence[str],
    entrypoints: Sequence[str],
) -> tuple[bytes, NativeRenderFileV1]:
    """The orientation floor: render roots, entrypoints, and the boundary.

    §11.9.5 asks the orientation-floor content to carry the working-set coverage
    map plus the render roots and named entrypoints. This file carries no
    regions at all: it is a signpost, not a projection of governed material, and
    its ``orientation`` disposition says so in the marker channel.
    """

    builder = _FileBuilder(path=README_PATH, disposition="orientation", ctx=ctx)
    builder.text("# Playbill native knowledge")
    _preamble(builder)
    builder.text(
        "## Render roots",
        "",
        *(f"- `{item}`" for item in roots),
        "",
        "## Orientation entrypoints",
        "",
        *(f"- `{item}`" for item in entrypoints),
        "",
        "## Coverage boundary",
        "",
        f"- generation: `{ctx.at.generation_root}`",
        f"- read at: {ctx.evaluation_time_text}",
        f"- boundary: {state.boundary.completeness}",
        f"- evidence index: `{state.boundary.index_digest}`",
        f"- access profile: `{state.boundary.access_profile_id}`",
        f"- declared sources: {len(state.boundary.scope)}",
        "",
        "## What is here",
        "",
        f"- Subjects: {len(state.subjects)}",
        f"- Claims: {len(state.claims)}",
        f"- ClaimTypes: {len(state.claim_types)}",
        f"- QueryDefinitions: {len(state.query_definitions)}",
        f"- Documents: {len(state.documents)}",
        "",
        "## Editing",
        "",
        "Fields marked editable are yours to change; derived blocks regenerate and",
        f"refuse edits. {DERIVED_REGENERATION_INSTRUCTION}",
        "",
        f"The baseline for every region is in `{NATIVE_RENDER_MANIFEST_PATH}`.",
        "",
    )
    return builder.finish()


class NativeRenderV1(BaseModel):
    """One render: the file map the caller may write, and the manifest of it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tag: Literal["playbill-native-render-v1"] = "playbill-native-render-v1"
    manifest: NativeRenderManifestV1
    files: dict[str, bytes]


def build_native_render(
    state: NativeAcceptedStateV1,
    ctx: RenderContextV1,
) -> NativeRenderV1:
    """Render accepted state as an editable working tree, deterministically.

    Refuses when the context does not describe the state it was handed: a render
    is a checkout of exactly one accepted generation for exactly one instance,
    and a context that names another is a caller error rather than something to
    reconcile silently.
    """

    if ctx.instance_id != state.instance_id:
        raise NativeRenderError("a render context and its accepted state must name one instance")
    if ctx.at != state.at:
        raise NativeRenderError("a render context and its accepted state must name one generation")

    rendered: dict[str, bytes] = {}
    entries: list[NativeRenderFileV1] = []

    def emit(result: tuple[bytes, NativeRenderFileV1]) -> None:
        content, entry = result
        if entry.path in rendered:
            raise NativeRenderError(f"two accepted artifacts render to one path: {entry.path}")
        rendered[entry.path] = content
        entries.append(entry)

    for subject in state.subjects:
        emit(_subject_file(state, ctx, subject))
    for claim_type in state.claim_types:
        emit(
            _artifact_file(
                state,
                ctx,
                claim_type,
                title=f"ClaimType — {claim_type.identity.removeprefix('ClaimType:')}",
                extra=_claim_type_extra(claim_type),
                body=("The contract every Claim of this predicate is admitted under.",),
            )
        )
    for query in state.query_definitions:
        emit(
            _artifact_file(
                state,
                ctx,
                query,
                title=f"QueryDefinition — {query.identity.removeprefix('QueryDefinition:')}",
                extra=_query_extra(query),
                body=("A named, accepted read over this instance's Claims.",),
            )
        )
    for document in state.documents:
        emit(
            _artifact_file(
                state,
                ctx,
                document,
                title=f"Document — {document.identity.removeprefix('document:')}",
                extra=_document_extra(document),
                body=("An accepted Document envelope; its body stays outside the ledger.",),
            )
        )
    rendered_subjects = state.subject_paths
    for claim in state.claims:
        if claim.claim.statement.subject.artifact_path in rendered_subjects:
            continue
        emit(_claim_file(state, ctx, claim))

    roots = byte_sorted(
        tuple({entry.path.split("/", 1)[0] + "/" for entry in entries if "/" in entry.path})
    )
    entrypoints = byte_sorted(
        (README_PATH, *(_render_path(item.path) for item in state.query_definitions))
    )
    emit(_readme(state, ctx, roots=roots, entrypoints=entrypoints))

    inventory = tuple(sorted(entries, key=lambda item: item.path.encode("utf-8")))
    manifest = NativeRenderManifestV1(
        instance_id=state.instance_id,
        coordinate=state.at,
        index_digest=state.boundary.index_digest,
        access_profile_id=state.boundary.access_profile_id,
        completeness=state.boundary.completeness,
        truncation_reason_codes=state.boundary.truncation_reason_codes,
        scope=state.boundary.scope,
        lens=ctx.lens,
        evaluation_time=ctx.evaluation_time,
        scope_kind=ctx.scope,
        scope_digest=ctx.scope_digest,
        scope_query_name=ctx.scope_query_name,
        render_roots=roots,
        orientation_entrypoints=entrypoints,
        files=inventory,
        render_digest=native_render_digest(inventory),
    )
    ordered = {
        path: rendered[path] for path in sorted(rendered, key=lambda item: item.encode("utf-8"))
    }
    return NativeRenderV1(
        manifest=manifest,
        files={NATIVE_RENDER_MANIFEST_PATH: render_native_manifest_bytes(manifest), **ordered},
    )


def render_native_tree(
    state: NativeAcceptedStateV1,
    ctx: RenderContextV1,
) -> dict[str, bytes]:
    """Return the rendered working tree as a deterministic path-to-bytes map."""

    return build_native_render(state, ctx).files


__all__ = [
    "DERIVED_NOTE",
    "EDITABLE_NOTE",
    "NATIVE_SOURCE_DIGEST_DOMAIN",
    "README_PATH",
    "NativeRenderV1",
    "build_native_render",
    "native_source_identity",
    "render_native_tree",
]
