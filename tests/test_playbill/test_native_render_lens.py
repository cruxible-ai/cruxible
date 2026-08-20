"""PC-F3-S1: the render lens, the typed region grammar, and the round-trip laws.

The §11.9.6 laws that S1 must already satisfy are render determinism and
parse/render idempotency; the rest of this file pins the §11.9.2/§11.9.3
semantics the grammar exists to carry. What is deliberately *not* pinned is a
spelling: no test here asserts a heading, a bullet, or a marker syntax, because
those are class-3 through the dogfood. The tests read the typed region
structure, which is the contract.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from cruxible_core.playbill.coverage.contracts import CoverageManifestProfileV1
from cruxible_core.playbill.native import (
    NATIVE_REGION_EDITABLE,
    NATIVE_RENDER_MANIFEST_PATH,
    NativeSyncRefusal,
    build_native_render,
    extract_regions,
    native_invalidation_index,
    native_status,
    parse_native_tree,
    plan_native_render,
    render_native_tree,
    resolve_native_invalidation,
    verify_native_locator,
)
from cruxible_core.playbill.native.grammar import REGION_CLOSE, render_region_open
from cruxible_core.service.playbill_floor import PlaybillFloorCoverageManifestV1
from tests.test_playbill._native_support import (
    WI_42,
    WI_43,
    native_context,
    native_state,
    seed_native_instance,
    seeded_render,
)


def _edit_value(content: bytes, *, old: bytes, new: bytes) -> bytes:
    edited = content.replace(old, new, 1)
    assert edited != content
    return edited


# -- §11.9.6 round-trip laws ----------------------------------------------


def test_render_is_a_deterministic_function_of_state_and_context(tmp_path: Path) -> None:
    """Same accepted state, same context, byte-identical bytes. No clock anywhere."""

    state, ctx, render = seeded_render(tmp_path)

    again = render_native_tree(state, ctx)

    assert again == render.files
    assert build_native_render(state, ctx).manifest == render.manifest


def test_every_time_in_a_render_comes_from_the_context(tmp_path: Path) -> None:
    """Two contexts differing only in read time produce two different renders.

    That is the observable half of "render never samples wall clock"; the
    structural half -- that the package holds no clock call at all -- is an
    architecture guardrail.
    """

    state, ctx, render = seeded_render(tmp_path)

    body = render.files[WI_42].decode("utf-8")

    assert ctx.evaluation_time.isoformat() in body
    assert render.manifest.evaluation_time == ctx.evaluation_time
    later = ctx.model_copy(update={"evaluation_time": ctx.evaluation_time + timedelta(hours=1)})
    assert render_native_tree(state, later) != render.files


def test_parsing_a_fresh_render_reproduces_the_baseline_with_nothing_dirty(
    tmp_path: Path,
) -> None:
    """`render(parse(render(x))) == render(x)`, as a fixed point of the grammar."""

    _state, _ctx, render = seeded_render(tmp_path)

    parsed = parse_native_tree(render.files, manifest=render.manifest)

    assert parsed.dirty_region_ids == ()
    assert parsed.tampered_region_ids == ()
    assert parsed.refusals == ()
    assert {item.region_id for item in parsed.regions} == {
        region.region_id for file in render.manifest.files for region in file.regions
    }
    assert all(item.state == "clean" for item in parsed.regions)
    assert all(item.observed_digest == item.baseline_digest for item in parsed.regions)


def test_regions_are_typed_and_the_split_is_the_frozen_one(tmp_path: Path) -> None:
    _state, _ctx, render = seeded_render(tmp_path)

    parsed = parse_native_tree(render.files, manifest=render.manifest)

    assert {item.region_kind for item in parsed.regions} <= set(NATIVE_REGION_EDITABLE)
    assert all(item.editable == NATIVE_REGION_EDITABLE[item.region_kind] for item in parsed.regions)
    assert any(item.editable for item in parsed.regions)
    assert any(not item.editable for item in parsed.regions)


# -- §11.9.2 rendered honesty ---------------------------------------------


def test_governance_renders_generation_and_time_qualified_never_a_bare_badge(
    tmp_path: Path,
) -> None:
    """A verdict that could sit in Git indefinitely must carry when and at what."""

    _state, ctx, render = seeded_render(tmp_path)
    _marker, regions, _diagnostics = extract_regions(render.files[WI_42], path=WI_42)
    governance = [item for item in regions if item.locator.region_kind == "governance"]

    assert governance
    for region in governance:
        body = region.body.decode("utf-8")
        verdict_line = next(
            line for line in body.splitlines() if line.startswith("verdict at render:")
        )
        assert "supported" in verdict_line
        assert "evaluated at" in verdict_line
        assert f"generation {ctx.at.generation_root}" in verdict_line
        currency_line = next(
            line for line in body.splitlines() if line.startswith("currency at render:")
        )
        assert "evaluated at" in currency_line
        assert f"generation {ctx.at.generation_root}" in currency_line


def test_editing_an_editable_field_is_dirty_and_grants_nothing(tmp_path: Path) -> None:
    _state, _ctx, render = seeded_render(tmp_path)
    edited = dict(render.files)
    edited[WI_42] = _edit_value(render.files[WI_42], old=b'\n"ready"\n', new=b'\n"shipped"\n')

    parsed = parse_native_tree(edited, manifest=render.manifest)

    assert len(parsed.dirty_region_ids) == 1
    assert parsed.tampered_region_ids == ()
    dirty = parsed.region(parsed.dirty_region_ids[0])
    assert dirty is not None and dirty.editable and dirty.region_kind == "statement_value"
    notices = [item for item in parsed.files if item.path == WI_42][0].diagnostics
    assert [item.code for item in notices] == ["editable_region_dirty"]
    assert all(item.severity == "notice" for item in notices)


def test_a_dirty_field_invalidates_its_derived_neighbours_through_the_overlay(
    tmp_path: Path,
) -> None:
    """§11.9.2 through the PC-F2 channel: the resolver reports it, unextended.

    The rendered file is a working source, the render baseline is the accepted
    evidence about the bytes it held at G, and the unmodified coverage resolver
    supplies the drift. Nothing here re-derives a match, and every card it
    returns is structurally incapable of granting a governance fact.
    """

    _state, ctx, render = seeded_render(tmp_path)
    edited = dict(render.files)
    edited[WI_42] = _edit_value(render.files[WI_42], old=b'\n"ready"\n', new=b'\n"shipped"\n')

    invalidation = resolve_native_invalidation(edited, manifest=render.manifest, ctx=ctx)

    assert invalidation.coverage.summary.drifted >= 1
    assert invalidation.drifted_addresses
    assert invalidation.invalidated_region_ids
    parsed = parse_native_tree(edited, manifest=render.manifest)
    dirty = parsed.region(parsed.dirty_region_ids[0])
    assert dirty is not None
    invalidated = {
        region.region_kind
        for region in parsed.regions
        if region.region_id in invalidation.invalidated_region_ids
    }
    assert invalidated == {"governance", "provenance"}
    assert all(
        region.address == dirty.address
        for region in parsed.regions
        if region.region_id in invalidation.invalidated_region_ids
    )
    # No governance facts reach local material: the cards say so structurally.
    assert invalidation.grants_governance_facts is False
    assert all(
        card.grants_mutation_authority is False and card.resolves_equivalence is False
        for span in invalidation.coverage.spans
        for card in span.cards
    )


def test_an_untouched_claim_keeps_its_derived_display(tmp_path: Path) -> None:
    """Invalidation is local to what changed, not a blanket over the tree."""

    _state, ctx, render = seeded_render(tmp_path)
    edited = dict(render.files)
    edited[WI_42] = _edit_value(render.files[WI_42], old=b'\n"ready"\n', new=b'\n"shipped"\n')

    invalidation = resolve_native_invalidation(edited, manifest=render.manifest, ctx=ctx)

    assert invalidation.intact_region_ids
    assert set(invalidation.intact_region_ids).isdisjoint(invalidation.invalidated_region_ids)


def test_editing_a_derived_field_is_a_typed_refusal_with_a_regeneration_instruction(
    tmp_path: Path,
) -> None:
    """§11.9.3: tampering is never given a semantic reading."""

    _state, _ctx, render = seeded_render(tmp_path)
    tampered = dict(render.files)
    tampered[WI_42] = _edit_value(
        render.files[WI_42],
        old=b"- role: observation",
        new=b"- role: normative",
    )

    parsed = parse_native_tree(tampered, manifest=render.manifest)

    assert parsed.dirty_region_ids == ()
    assert len(parsed.tampered_region_ids) == 1
    refusals = [item for item in parsed.refusals if item.code == "derived_region_tampered"]
    assert len(refusals) == 1
    assert refusals[0].severity == "refusal"
    assert refusals[0].instruction is not None and "Re-render" in refusals[0].instruction
    tampered_region = parsed.region(parsed.tampered_region_ids[0])
    assert tampered_region is not None and not tampered_region.editable


# -- §11.9.3 locator semantics --------------------------------------------


def test_a_locator_verifies_against_accepted_state_and_a_forged_one_refuses(
    tmp_path: Path,
) -> None:
    state, _ctx, render = seeded_render(tmp_path)
    _marker, regions, _diagnostics = extract_regions(render.files[WI_42], path=WI_42)
    genuine = regions[0].locator

    verified = verify_native_locator(genuine, state=state, manifest=render.manifest, path=WI_42)
    forged = verify_native_locator(
        genuine.model_copy(update={"artifact_digest": "sha256:" + "0" * 64}),
        state=state,
        manifest=render.manifest,
        path=WI_42,
    )
    unknown = verify_native_locator(
        genuine.model_copy(update={"region_id": "sha256:" + "1" * 64}),
        state=state,
        manifest=render.manifest,
        path=WI_42,
    )
    stale = verify_native_locator(
        genuine.model_copy(update={"generation_root": "sha256:" + "2" * 64}),
        state=state,
        manifest=render.manifest,
        path=WI_42,
    )

    assert verified.verified and verified.reason_codes == ()
    assert not forged.verified and "artifact_digest_mismatch" in forged.reason_codes
    assert not unknown.verified and "region_not_in_baseline" in unknown.reason_codes
    assert not stale.verified and "generation_mismatch" in stale.reason_codes
    assert all(
        item.grants_mutation_authority is False for item in (verified, forged, unknown, stale)
    )


def test_a_locator_copied_into_another_file_refuses_as_ambiguity(tmp_path: Path) -> None:
    """§11.9.3: duplicated locators refuse, and neither occurrence is bound."""

    _state, _ctx, render = seeded_render(tmp_path)
    _marker, regions, _diagnostics = extract_regions(render.files[WI_42], path=WI_42)
    borrowed = regions[0]
    block = (
        render_region_open(borrowed.locator).encode("utf-8")
        + b"\n"
        + borrowed.body
        + REGION_CLOSE.encode("utf-8")
        + b"\n"
    )
    copied = dict(render.files)
    copied[WI_43] = render.files[WI_43] + b"\n" + block

    parsed = parse_native_tree(copied, manifest=render.manifest)

    duplicated = [item for item in parsed.refusals if item.code == "locator_duplicated"]
    assert duplicated and duplicated[0].region_id == borrowed.locator.region_id
    bound = [item for item in parsed.regions if item.region_id == borrowed.locator.region_id]
    assert len(bound) == 2
    assert all(item.state == "ambiguous" for item in bound)


def test_a_moved_file_preserves_region_identity_because_a_path_is_presentation(
    tmp_path: Path,
) -> None:
    """§11.9.3: paths are presentation coordinates over occurrence identity."""

    _state, _ctx, render = seeded_render(tmp_path)
    moved = {
        ("docs/renamed-wi-42.md" if path == WI_42 else path): content
        for path, content in render.files.items()
    }

    parsed = parse_native_tree(moved, manifest=render.manifest)

    relocated = [item for item in parsed.regions if item.path == "docs/renamed-wi-42.md"]
    assert relocated
    assert all(item.moved and item.moved_from_path == WI_42 for item in relocated)
    assert all(item.state == "clean" for item in relocated)
    original = {
        region.region_id
        for file in render.manifest.files
        if file.path == WI_42
        for region in file.regions
    }
    assert {item.region_id for item in relocated} == original


def test_removing_a_rendered_file_is_never_inferred_as_retirement(tmp_path: Path) -> None:
    _state, _ctx, render = seeded_render(tmp_path)
    without = {path: content for path, content in render.files.items() if path != WI_42}

    parsed = parse_native_tree(without, manifest=render.manifest)

    absent = [item for item in parsed.diagnostics if item.code == "rendered_file_absent"]
    assert [item.path for item in absent] == [WI_42]
    assert all(item.severity == "notice" for item in absent)
    assert parsed.refusals == ()


# -- §11.9.5 manifest and explicit sync -----------------------------------


def test_the_render_manifest_is_a_profile_inheriting_every_coverage_field(
    tmp_path: Path,
) -> None:
    """One manifest family, two profiles. No second schema, checked structurally."""

    _state, ctx, render = seeded_render(tmp_path)
    manifest = render.manifest

    assert isinstance(manifest, CoverageManifestProfileV1)
    family = set(CoverageManifestProfileV1.model_fields)
    assert family <= set(type(manifest).model_fields)
    assert family <= set(PlaybillFloorCoverageManifestV1.model_fields)
    assert manifest.format == "playbill-coverage-manifest-v1"
    # A render observes no working snapshot, exactly as an exported floor does.
    assert manifest.epoch is None
    assert manifest.watcher_health == "absent"
    # The render's own additions.
    assert manifest.lens.renderer_digest.startswith("sha256:")
    assert manifest.scope_digest == ctx.scope_digest
    assert manifest.evaluation_time == ctx.evaluation_time
    assert manifest.render_roots and manifest.orientation_entrypoints
    assert all(item.regions or item.disposition == "orientation" for item in manifest.files)
    assert NATIVE_RENDER_MANIFEST_PATH not in {item.path for item in manifest.files}


def test_the_manifest_baseline_matches_the_bytes_that_were_rendered(tmp_path: Path) -> None:
    _state, _ctx, render = seeded_render(tmp_path)

    for entry in render.manifest.files:
        content = render.files[entry.path]
        assert entry.byte_length == len(content)
        _marker, regions, diagnostics = extract_regions(content, path=entry.path)
        assert diagnostics == ()
        assert {item.locator.region_id for item in regions} == {
            item.region_id for item in entry.regions
        }
        for raw in regions:
            baseline = next(
                item for item in entry.regions if item.region_id == raw.locator.region_id
            )
            assert raw.body_digest == baseline.body_digest
            assert raw.locator.baseline_digest == baseline.body_digest


def test_a_rerender_over_a_dirty_region_refuses_without_an_explicit_discard(
    tmp_path: Path,
) -> None:
    """§11.9.5: a re-render never overwrites dirty regions without stash or discard."""

    _state, _ctx, render = seeded_render(tmp_path)
    edited = dict(render.files)
    edited[WI_42] = _edit_value(render.files[WI_42], old=b'\n"ready"\n', new=b'\n"shipped"\n')

    with pytest.raises(NativeSyncRefusal) as refusal:
        plan_native_render(edited, manifest=render.manifest, render=render)

    assert WI_42 in str(refusal.value)
    assert "statement_value" in str(refusal.value)

    plan = plan_native_render(edited, manifest=render.manifest, render=render, discard=True)
    assert plan.write_paths == (WI_42,)
    assert plan.stash_required is True
    assert len(plan.discarded_region_ids) == 1


def test_a_rerender_over_a_clean_tree_writes_nothing(tmp_path: Path) -> None:
    _state, _ctx, render = seeded_render(tmp_path)

    plan = plan_native_render(render.files, manifest=render.manifest, render=render)

    assert plan.write_paths == ()
    assert plan.delete_paths == ()
    assert plan.stash_required is False
    assert set(plan.unchanged_paths) == set(render.files)


def test_status_reports_per_file_state_region_counts_and_the_baseline_generation(
    tmp_path: Path,
) -> None:
    _state, _ctx, render = seeded_render(tmp_path)
    edited = dict(render.files)
    edited[WI_42] = _edit_value(render.files[WI_42], old=b'\n"ready"\n', new=b'\n"shipped"\n')

    status = native_status(edited, manifest=render.manifest)

    assert status.baseline_generation_root == render.manifest.coordinate.generation_root
    assert status.dirty is True and status.tampered is False
    rows = {item.path: item for item in status.files}
    assert rows[WI_42].state == "dirty"
    assert rows[WI_42].dirty_regions == 1
    assert rows[WI_43].state == "clean"
    assert rows[WI_43].dirty_regions == 0
    assert all(
        item.region_count == item.clean_regions + item.dirty_regions for item in rows.values()
    )
    assert status.missing_paths == () and status.untracked_paths == ()


# -- hard laws -------------------------------------------------------------


def test_rendering_never_mutates_accepted_state(tmp_path: Path) -> None:
    """A checkout is a read. The accepted coordinate is the same on both sides."""

    instance, _owner = seed_native_instance(tmp_path)
    before = instance.accepted_coordinate()
    state = native_state(instance)
    ctx = native_context(state)

    build_native_render(state, ctx)
    instance.refresh()

    assert instance.accepted_coordinate() == before


def test_the_invalidation_index_cites_only_rendered_editable_material(
    tmp_path: Path,
) -> None:
    """The disposable index is a projection of the baseline, never a Capture.

    It names no accepted Capture digest, because the render baseline is not
    evidence the ledger accepted: it is what the lens wrote. What it does carry
    is the Claim the region belongs to and the locator that dereferences it.
    """

    _state, _ctx, render = seeded_render(tmp_path)

    index = native_invalidation_index(render.manifest)

    assert index.at == render.manifest.coordinate
    assert index.citations
    assert all(item.capture_digests == () for item in index.citations)
    assert all(item.digest_kind == "exact_bytes" for item in index.citations)
    assert all(item.dereference_handle_digest is not None for item in index.citations)
    editable = {
        region.body_digest
        for file in render.manifest.files
        for region in file.regions
        if region.editable and region.byte_length
    }
    assert {item.commitment_digest for item in index.citations} == editable
