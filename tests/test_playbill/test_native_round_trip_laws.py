"""PC-F3-S3: the §11.9.6 round-trip laws, frozen, in one place.

This module is the canonical home of the five laws. It quotes the spec block it
freezes, and every test in it is one law under the name of that law -- so a
reader can check the block against the specification by reading test names, and
a later batch that weakens a law has to delete a test that says what it deleted.

    All laws hold over an explicit `RenderContextV1` (accepted generation,
    evaluation time, scope/query digest, access profile, lens/renderer digest).
    Render never samples wall clock, and live freshness overlays never alter
    committed snapshot bytes -- deterministic rendering is a function of state
    and context, nothing else.

    compile(render(accepted_state, ctx)) == no-op
    render(parse(render(accepted_state, ctx)), ctx) == render(accepted_state, ctx)
    edit editable field -> compile -> accept -> render  preserves semantic payload
    edit derived field -> typed refusal
    re-render over dirty regions -> refuses without explicit stash/discard

The two preconditions in that paragraph are tested here too, because a law that
holds over a context nobody can reproduce is not a law.

What these tests never assert is a **spelling**. Every byte comparison is
against the render's own output within one context; no heading, bullet, marker
syntax, or literal Markdown is pinned anywhere in this file, and the one edit
that has to reach inside a derived region reaches it through the typed region
structure rather than by matching rendered text. The grammar is class-3 through
the dogfood, and freezing it here would freeze the thing §11.9 deliberately left
free.
"""

from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path

import pytest

from cruxible_client.contracts.claims import LiteralClaimObject
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.native import (
    NativeAcceptedStateV1,
    NativeClaimRecordV1,
    NativeCompileResultV1,
    NativeFileSourceV1,
    NativeRegionSegmentV1,
    NativeRenderManifestV1,
    NativeSyncRefusal,
    build_native_render,
    compile_native_tree,
    emit_native_file_source,
    native_boundary_from_manifest,
    native_render_from_tree,
    native_stash_body,
    parse_native_tree,
    plan_native_render,
    read_native_file_source,
    render_context_from_manifest,
    render_native_tree,
    resolve_native_invalidation,
)
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.service.playbill_claims import (
    DirectClaimAuthoringV1,
    service_propose_playbill_claims,
)
from tests.test_playbill._knowledge_loop_support import activate
from tests.test_playbill._native_support import (
    WI_42,
    native_context,
    native_state,
    seed_native_instance,
    seeded_render,
)

READY = b'\n"ready"\n'
DONE = b'\n"done"\n'
WI_42_ARTIFACT = "wi-42.yaml"

CLOCK_GUARDRAIL = (
    "tests/test_architecture/test_playbill_dp0_boundaries.py"
    "::test_pc_f3_native_render_adds_no_authority_and_reads_no_clock"
)


def _compiled(
    instance: PlaybillInstance,
    files: dict[str, bytes],
    manifest: NativeRenderManifestV1,
) -> NativeCompileResultV1:
    """Compile exactly as the CLI does: baseline at G, head now, one context."""

    baseline_at = PlaybillAcceptedCoordinate.model_validate(
        manifest.coordinate.model_dump(mode="json")
    )
    return compile_native_tree(
        files,
        manifest=manifest,
        baseline_state=native_state(
            instance,
            at=baseline_at,
            boundary=native_boundary_from_manifest(manifest),
        ),
        accepted_state_at_head=native_state(instance),
        ctx=render_context_from_manifest(manifest),
    )


def _edited(files: dict[str, bytes], path: str, old: bytes, new: bytes) -> dict[str, bytes]:
    edited = dict(files)
    assert old in edited[path], f"{old!r} is not in the rendered {path}"
    edited[path] = edited[path].replace(old, new, 1)
    return edited


def _wi_42_claim(state: NativeAcceptedStateV1) -> NativeClaimRecordV1:
    return next(
        item
        for item in state.claims
        if item.claim.statement.subject.artifact_path.endswith(WI_42_ARTIFACT)
    )


def _literal_value(record: NativeClaimRecordV1) -> object:
    value = record.claim.statement.object
    assert isinstance(value, LiteralClaimObject)
    return value.value


# -- preconditions: the context is the whole of the environment --------------


def test_precondition_render_never_samples_the_wall_clock(tmp_path: Path) -> None:
    """Every time in a render arrives in the context, and none is read from a clock.

    The observable half is here: one state under two contexts differing only in
    read time produces two different renders, and the same context twice
    produces the same bytes. The structural half -- that no module in the native
    package calls a clock at all -- is an AST guardrail rather than a behavioral
    test, because "it did not read the clock this time" is not the statement the
    law needs. This test names that guardrail and asserts it still exists, so the
    reference cannot rot into a note about a test somebody deleted.
    """

    state, ctx, render = seeded_render(tmp_path)

    assert render_native_tree(state, ctx) == render.files
    later = ctx.model_copy(update={"evaluation_time": ctx.evaluation_time + timedelta(hours=1)})
    assert render_native_tree(state, later) != render.files
    assert render.manifest.evaluation_time == ctx.evaluation_time

    path, _separator, name = CLOCK_GUARDRAIL.partition("::")
    guardrail = Path(__file__).resolve().parents[2] / path
    tree = ast.parse(guardrail.read_text(encoding="utf-8"), filename=str(guardrail))
    assert name in {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}, (
        f"the structural half of this law lives in {CLOCK_GUARDRAIL} and is gone"
    )


def test_precondition_live_freshness_never_alters_committed_snapshot_bytes(
    tmp_path: Path,
) -> None:
    """Resolving coverage over a rendered tree answers about it and changes nothing.

    The freshness overlay is computed *from* the committed bytes; a resolve that
    wrote back into them would make the render a function of when it was last
    asked about, which is exactly what the deterministic-render law forbids.
    """

    _state, _ctx, render = seeded_render(tmp_path)
    files = dict(render.files)
    before = {path: bytes(content) for path, content in files.items()}

    invalidation = resolve_native_invalidation(
        files,
        manifest=render.manifest,
        ctx=render_context_from_manifest(render.manifest),
    )

    assert invalidation.coverage.spans
    assert files == before
    assert render.files == before


# -- law 1 -------------------------------------------------------------------


def test_law_compile_of_a_render_is_a_no_op(tmp_path: Path) -> None:
    """`compile(render(accepted_state, ctx)) == no-op`.

    Zero members, zero drafts, zero refusals: a checkout nobody has edited
    proposes nothing, and says so rather than erroring.
    """

    instance, _owner = seed_native_instance(tmp_path)
    state = native_state(instance)
    render = build_native_render(state, native_context(state))

    result = _compiled(instance, dict(render.files), render.manifest)

    assert result.members == ()
    assert result.drafts == ()
    assert result.refusals == ()
    assert result.three_way == ()
    assert result.compilable is False


# -- law 2 -------------------------------------------------------------------


def test_law_render_of_a_parsed_render_reproduces_the_render(tmp_path: Path) -> None:
    """`render(parse(render(accepted_state, ctx)), ctx) == render(accepted_state, ctx)`.

    The literal law: read the rendered tree back, rebuild the whole render from
    what was read -- manifest included, with every per-file baseline recomputed
    from the bytes rather than copied out of the committed manifest -- and
    byte-compare. Nothing accepted is in scope for the reconstruction, so an
    equal result says the rendered tree describes itself totally and loses
    nothing on the way back.
    """

    _state, _ctx, render = seeded_render(tmp_path)

    recovered = native_render_from_tree(render.files)

    assert recovered.files == render.files
    assert recovered.manifest == render.manifest
    assert recovered == render
    # And the parse of that reconstruction is the same clean parse, so the fixed
    # point is a fixed point of the typed structure and not only of the bytes.
    parsed = parse_native_tree(recovered.files, manifest=recovered.manifest)
    assert parsed.refusals == ()
    assert parsed.dirty_region_ids == ()
    assert parsed.tampered_region_ids == ()
    assert {item.region_id for item in parsed.regions} == {
        region.region_id for file in render.manifest.files for region in file.regions
    }
    assert all(item.observed_digest == item.baseline_digest for item in parsed.regions)


# -- law 3 -------------------------------------------------------------------


def test_law_edit_compile_accept_render_preserves_the_semantic_payload(
    tmp_path: Path,
) -> None:
    """`edit editable field -> compile -> accept -> render  preserves semantic payload`.

    The whole loop, through the governed path: edit a rendered statement value,
    compile it into ordinary proposal input, submit and accept it as any other
    proposal, then re-render at the new generation. The edited payload has to
    survive as *accepted state* -- not merely as text that came back -- so this
    reads the accepted Claim as well as the fresh render, and the re-rendered
    tree has to compile to nothing against its own new baseline, which is what
    makes the round trip closed rather than merely successful.
    """

    instance, owner = seed_native_instance(tmp_path)
    state = native_state(instance)
    render = build_native_render(state, native_context(state))
    claim = _wi_42_claim(state)
    assert _literal_value(claim) == "ready"

    edited = _edited(dict(render.files), WI_42, READY, DONE)
    result = _compiled(instance, edited, render.manifest)
    assert result.compilable is True

    proposed = service_propose_playbill_claims(
        instance,
        authorings=tuple(DirectClaimAuthoringV1.model_validate(item) for item in result.authorings),
        actor_id="owner",
        proposal_name="native-round-trip",
        timestamp="2026-08-16T20:06:00.000000Z",
        base=PlaybillAcceptedCoordinate.model_validate(
            render.manifest.coordinate.model_dump(mode="json")
        ),
    )
    activate(instance, owner, proposed, sequence=4)

    after = native_state(instance)
    accepted = _wi_42_claim(after)
    assert _literal_value(accepted) == "done"
    assert accepted.claim.identity.name == claim.claim.identity.name

    rerendered = build_native_render(after, native_context(after))
    assert rerendered.manifest.coordinate != render.manifest.coordinate
    reparsed = parse_native_tree(rerendered.files, manifest=rerendered.manifest)
    assert reparsed.dirty_region_ids == ()
    assert reparsed.refusals == ()
    # The payload is in the tree, and compiling the fresh checkout proposes
    # nothing: the loop closed on the acceptance rather than on the text.
    assert DONE in rerendered.files[WI_42]
    settled = _compiled(instance, dict(rerendered.files), rerendered.manifest)
    assert settled.members == ()
    assert settled.refusals == ()


# -- law 4 -------------------------------------------------------------------


def _tamper_with_a_derived_region(
    files: dict[str, bytes],
    manifest: NativeRenderManifestV1,
) -> tuple[dict[str, bytes], str]:
    """Add a line inside one derived region, reaching it through typed structure.

    Deliberately not a text search: matching rendered prose would make this law
    a statement about a class-3 spelling. The region is chosen from the manifest
    by its typed editable flag and edited as a region body.
    """

    entry, baseline = next(
        (entry, region)
        for entry in manifest.files
        for region in entry.regions
        if not region.editable and region.byte_length
    )
    source = read_native_file_source(entry.path, files[entry.path])
    segments = tuple(
        NativeRegionSegmentV1(
            locator=item.locator,
            lines=(*item.lines, b"a line the lens never rendered"),
        )
        if isinstance(item, NativeRegionSegmentV1) and item.locator.region_id == baseline.region_id
        else item
        for item in source.segments
    )
    tampered = dict(files)
    tampered[entry.path] = emit_native_file_source(
        NativeFileSourceV1(path=entry.path, marker=source.marker, segments=segments)
    )
    assert tampered[entry.path] != files[entry.path]
    return tampered, baseline.region_id


def test_law_editing_a_derived_field_is_a_typed_refusal(tmp_path: Path) -> None:
    """`edit derived field -> typed refusal`.

    Typed at both gates and interpreted at neither: the parse reports the region
    as tampered and carries a regeneration instruction, and the compile refuses
    rather than reading the edited text as anything. Nothing tries to work out
    what the edit meant, because a derived field is a projection of something the
    ledger decided and nothing typed into a working file changes what that was.
    """

    instance, _owner = seed_native_instance(tmp_path)
    state = native_state(instance)
    render = build_native_render(state, native_context(state))

    tampered, region_id = _tamper_with_a_derived_region(dict(render.files), render.manifest)

    parsed = parse_native_tree(tampered, manifest=render.manifest)
    region = parsed.region(region_id)
    assert region is not None
    assert region.state == "tampered"
    assert region.editable is False
    assert parsed.dirty_region_ids == ()
    refusals = [item for item in parsed.refusals if item.region_id == region_id]
    assert [item.code for item in refusals] == ["derived_region_tampered"]
    assert refusals[0].severity == "refusal"
    assert refusals[0].instruction is not None

    result = _compiled(instance, tampered, render.manifest)
    assert [item.code for item in result.refusals] == ["derived_region_tampered"]
    assert [item.region_id for item in result.refusals] == [region_id]
    assert result.refusals[0].required_action == refusals[0].instruction
    assert result.members == ()
    assert result.compilable is False


# -- law 5 -------------------------------------------------------------------


def test_law_a_re_render_over_dirty_regions_refuses_without_stash_or_discard(
    tmp_path: Path,
) -> None:
    """`re-render over dirty regions -> refuses without explicit stash/discard`.

    The refusal is the default and both ways past it are acts. It names every
    dirty region, it names all three things an author may do about them --
    compile, stash, discard -- and it will not accept both dispositions at once,
    because "keep these and drop these" is not one answer.
    """

    _state, _ctx, render = seeded_render(tmp_path)
    dirty = _edited(dict(render.files), WI_42, READY, DONE)
    region_ids = parse_native_tree(dirty, manifest=render.manifest).dirty_region_ids
    assert region_ids

    with pytest.raises(NativeSyncRefusal) as refused:
        plan_native_render(dirty, manifest=render.manifest, render=render)
    message = str(refused.value)
    assert WI_42 in message
    assert "stash" in message
    assert "discard" in message
    assert "Compile" in message

    with pytest.raises(NativeSyncRefusal):
        plan_native_render(dirty, manifest=render.manifest, render=render, stash=True, discard=True)

    stashed = plan_native_render(dirty, manifest=render.manifest, render=render, stash=True)
    assert stashed.stashed_region_ids == region_ids
    assert stashed.discarded_region_ids == ()
    assert stashed.write_paths == (WI_42,)

    discarded = plan_native_render(dirty, manifest=render.manifest, render=render, discard=True)
    assert discarded.discarded_region_ids == region_ids
    assert discarded.stashed_region_ids == ()

    # The stash disposition is only a plan until the caller writes it, and what
    # it would write is exactly the bytes the refusal was protecting.
    body = native_stash_body(dirty, manifest=render.manifest)
    assert body is not None
    assert body.region_ids == region_ids
    assert DONE.strip(b"\n") in body.regions[0].body
