"""The native knowledge surface: the in-repo render lens over accepted state.

Governing principle (§11.9): **the ledger is the semantic object store; the
in-repo knowledge directory is its editable working tree.** Render is a
checkout, an edit is `local_only`, a compile proposes, an acceptance merges, and
a re-render pulls. Nothing under this package writes accepted state, and nothing
accepted references render output: delete the rendered directory and the only
loss is uncompiled local edits.

What is frozen here and what is not
-----------------------------------
The **semantic categories and laws** are the contract -- the editable/derived
region split, generation- and time-qualified governance rendering, deletion is
never inferred, tampering with a derived region refuses, locators are untrusted
until verified, and the round-trip laws. The **serialized spellings** -- the
Markdown shape, the marker syntax, the section headings -- are deliberately
class-3 experimental through the dogfood, so the lens is versioned
(:data:`grammar.NATIVE_LENS_VERSION`) and digested
(:func:`grammar.native_renderer_digest`) rather than pinned to frozen wire tags.
A grammar change moves the version and the renderer digest; it does not break a
frozen contract, because the spellings never became one.
"""

from cruxible_core.playbill.native.compile import (
    NativeCompileDraftV1,
    NativeCompileError,
    NativeCompileMemberV1,
    NativeCompileRefusalV1,
    NativeCompileResultV1,
    NativeDraftCandidateV1,
    NativeDraftDispositionV1,
    NativeReviewCurrencyV1,
    NativeThreeWayV1,
    compile_native_tree,
    native_review_currency,
)
from cruxible_core.playbill.native.context import (
    NATIVE_WHOLE_SCOPE,
    RenderContextV1,
    native_scope_digest,
    whole_scope_context,
)
from cruxible_core.playbill.native.grammar import (
    NATIVE_DRAFT_DISPOSITIONS,
    NATIVE_GRAMMAR_CLASS,
    NATIVE_LENS_ID,
    NATIVE_LENS_VERSION,
    NATIVE_REGION_EDITABLE,
    NATIVE_REGION_KINDS,
    NativeDiagnosticV1,
    NativeDraftMarkerV1,
    NativeFileMarkerV1,
    NativeLensV1,
    NativeLocatorV1,
    NativeRegionKind,
    NativeRenderError,
    body_commitment,
    default_native_lens,
    extract_prose,
    extract_regions,
    native_renderer_digest,
    region_identity_digest,
    render_draft_marker,
)
from cruxible_core.playbill.native.inverse import (
    NativeFileSourceV1,
    NativeProseSegmentV1,
    NativeRegionSegmentV1,
    emit_native_file_source,
    native_render_from_tree,
    read_native_file_source,
)
from cruxible_core.playbill.native.lens import (
    NativeRenderV1,
    build_native_render,
    render_native_tree,
)
from cruxible_core.playbill.native.manifest import (
    NATIVE_RENDER_MANIFEST_PATH,
    NativeRegionBaselineV1,
    NativeRenderFileV1,
    NativeRenderManifestV1,
    native_render_digest,
)
from cruxible_core.playbill.native.parse import (
    NativeFileParseV1,
    NativeParsedRegionV1,
    NativeTreeParseV1,
    parse_native_file,
    parse_native_tree,
)
from cruxible_core.playbill.native.stash import (
    NATIVE_STASH_DIRECTORY,
    NATIVE_STASH_FILE_PREFIX,
    NativeStashBodyV1,
    NativeStashError,
    NativeStashFileV1,
    NativeStashRegionV1,
    NativeStashRestoreV1,
    native_stash_body,
    native_stash_digest,
    native_stash_entry_path,
    parse_native_stash,
    render_native_stash,
    resolve_native_stash,
    restore_native_stash,
)
from cruxible_core.playbill.native.state import (
    NativeAcceptedStateV1,
    NativeArtifactRecordV1,
    NativeClaimRecordV1,
    NativeCoverageBoundaryV1,
    artifact_record_from_projection,
    build_native_state,
    claim_from_projection,
    claim_record_from_projection,
    native_boundary_from_floor,
    native_boundary_from_manifest,
)
from cruxible_core.playbill.native.sync import (
    NativeFileStatusV1,
    NativeRenderPlanV1,
    NativeSyncRefusal,
    NativeTreeStatusV1,
    native_status,
    plan_native_render,
    render_context_from_manifest,
)
from cruxible_core.playbill.native.verify import (
    NATIVE_CARD_BUDGET,
    NativeInvalidationV1,
    NativeLocatorVerdictV1,
    locator_handle_digest,
    native_baseline_snapshot,
    native_freshness_manifest,
    native_invalidation_index,
    native_invalidation_observations,
    native_invalidation_overlay,
    native_invalidation_spans,
    resolve_native_invalidation,
    verify_native_locator,
)

__all__ = [
    "NATIVE_CARD_BUDGET",
    "NATIVE_DRAFT_DISPOSITIONS",
    "NATIVE_GRAMMAR_CLASS",
    "NATIVE_LENS_ID",
    "NATIVE_LENS_VERSION",
    "NATIVE_REGION_EDITABLE",
    "NATIVE_REGION_KINDS",
    "NATIVE_RENDER_MANIFEST_PATH",
    "NATIVE_STASH_DIRECTORY",
    "NATIVE_STASH_FILE_PREFIX",
    "NATIVE_WHOLE_SCOPE",
    "NativeAcceptedStateV1",
    "NativeArtifactRecordV1",
    "NativeClaimRecordV1",
    "NativeCompileDraftV1",
    "NativeCompileError",
    "NativeCompileMemberV1",
    "NativeCompileRefusalV1",
    "NativeCompileResultV1",
    "NativeCoverageBoundaryV1",
    "NativeDiagnosticV1",
    "NativeDraftCandidateV1",
    "NativeDraftDispositionV1",
    "NativeDraftMarkerV1",
    "NativeFileMarkerV1",
    "NativeFileParseV1",
    "NativeFileSourceV1",
    "NativeFileStatusV1",
    "NativeInvalidationV1",
    "NativeLensV1",
    "NativeLocatorV1",
    "NativeLocatorVerdictV1",
    "NativeParsedRegionV1",
    "NativeProseSegmentV1",
    "NativeRegionBaselineV1",
    "NativeRegionKind",
    "NativeRegionSegmentV1",
    "NativeRenderError",
    "NativeRenderFileV1",
    "NativeRenderManifestV1",
    "NativeRenderPlanV1",
    "NativeRenderV1",
    "NativeReviewCurrencyV1",
    "NativeStashBodyV1",
    "NativeStashError",
    "NativeStashFileV1",
    "NativeStashRegionV1",
    "NativeStashRestoreV1",
    "NativeSyncRefusal",
    "NativeThreeWayV1",
    "NativeTreeParseV1",
    "NativeTreeStatusV1",
    "RenderContextV1",
    "artifact_record_from_projection",
    "body_commitment",
    "build_native_render",
    "build_native_state",
    "claim_from_projection",
    "claim_record_from_projection",
    "compile_native_tree",
    "default_native_lens",
    "emit_native_file_source",
    "extract_prose",
    "extract_regions",
    "locator_handle_digest",
    "native_baseline_snapshot",
    "native_boundary_from_floor",
    "native_boundary_from_manifest",
    "native_freshness_manifest",
    "native_invalidation_index",
    "native_invalidation_observations",
    "native_invalidation_overlay",
    "native_invalidation_spans",
    "native_render_digest",
    "native_render_from_tree",
    "native_renderer_digest",
    "native_review_currency",
    "native_scope_digest",
    "native_stash_body",
    "native_stash_digest",
    "native_stash_entry_path",
    "native_status",
    "parse_native_file",
    "parse_native_stash",
    "parse_native_tree",
    "plan_native_render",
    "read_native_file_source",
    "region_identity_digest",
    "render_context_from_manifest",
    "render_draft_marker",
    "render_native_stash",
    "render_native_tree",
    "resolve_native_invalidation",
    "resolve_native_stash",
    "restore_native_stash",
    "verify_native_locator",
    "whole_scope_context",
]
