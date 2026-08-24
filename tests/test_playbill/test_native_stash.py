"""PC-F3-S3: the stash, which turns "compile it or lose it" into a real choice.

The §11.9.5 refusal is frozen in the round-trip block. What is under test here is
the third answer to it: keeping the bytes. A stash entry has to be a disposable
local record in the same family as the replay checkpoint and the coverage
manifest -- digest-committed, superseded by rewriting, never accepted state --
and restoring one has to land by region identity rather than by position, since
identity is what survives a file move and a byte offset is what does not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.query.grammar import byte_sorted
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_core.playbill.coverage.contracts import CoverageLineOverlayV1
from cruxible_core.playbill.native import (
    NativeRenderManifestV1,
    NativeStashError,
    build_native_render,
    native_stash_body,
    native_stash_digest,
    native_stash_entry_path,
    parse_native_stash,
    parse_native_tree,
    plan_native_render,
    render_native_stash,
    resolve_native_stash,
    restore_native_stash,
)
from cruxible_core.playbill.native.parse import (
    NativeFileParseV1,
    NativeParsedRegionV1,
    NativeTreeParseV1,
)
from cruxible_core.playbill.native.stash import (
    NATIVE_STASH_DIRECTORY,
    NativeStashFileV1,
)
from tests.test_playbill._native_support import (
    WI_42,
    WI_43,
    native_context,
    native_state,
    seed_native_instance,
    seeded_render,
)

READY = b'\n"ready"\n'
BLOCKED = b'\n"blocked"\n'
DONE = b'\n"done"\n'
SHIPPED = b'\n"shipped"\n'


def _edit(files: dict[str, bytes], path: str, old: bytes, new: bytes) -> dict[str, bytes]:
    edited = dict(files)
    assert old in edited[path], f"{old!r} is not in the rendered {path}"
    edited[path] = edited[path].replace(old, new, 1)
    return edited


def _stashed(files: dict[str, bytes], manifest: NativeRenderManifestV1) -> NativeStashFileV1:
    body = native_stash_body(files, manifest=manifest)
    assert body is not None
    return parse_native_stash(render_native_stash(body, written_at="2026-08-16T21:00:00+00:00"))


# -- what a stash captures -------------------------------------------------


def test_a_clean_tree_has_nothing_to_stash(tmp_path: Path) -> None:
    """There is no empty stash: nothing dirty means no entry at all."""

    _state, _ctx, render = seeded_render(tmp_path)

    assert native_stash_body(dict(render.files), manifest=render.manifest) is None


def test_a_stash_captures_exactly_the_dirty_regions_bytes(tmp_path: Path) -> None:
    """The bytes a re-render would have lost are the bytes the entry holds.

    Only dirty regions: a tampered derived region regenerates by definition and
    has nothing to preserve, so keeping its bytes would be keeping bytes nobody
    could put back.
    """

    _state, _ctx, render = seeded_render(tmp_path)
    edited = _edit(dict(render.files), WI_42, READY, DONE)
    edited = _edit(edited, WI_43, BLOCKED, SHIPPED)
    parsed = parse_native_tree(edited, manifest=render.manifest)

    body = native_stash_body(edited, manifest=render.manifest)

    assert body is not None
    assert body.region_ids == parsed.dirty_region_ids
    assert {item.path for item in body.regions} == {WI_42, WI_43}
    assert all(item.region_kind == "statement_value" for item in body.regions)
    assert body.at == render.manifest.coordinate
    assert body.render_digest == render.manifest.render_digest
    for region in body.regions:
        baseline = render.manifest.baseline_for(region.region_id)
        assert baseline is not None
        assert region.baseline_digest == baseline[1].body_digest
        assert region.body_digest != region.baseline_digest


def test_a_parse_reports_region_identities_in_canonical_order_not_file_order(
    tmp_path: Path,
) -> None:
    """Two routes to "the dirty regions" may not disagree about their order.

    The digest-committed stash body orders its regions by region identity, and
    identity is path-free by §11.9.3. A parse that reported the same identities
    in *presentation* order -- byte-sorted path, then position in the file --
    therefore agreed with the stash only when the two orderings happened to
    coincide, which for content-addressed identities is a coin flip per render.
    That is a digest-committed local format whose field order depends on where
    the lens placed a Claim, and this format family exists to refuse exactly
    that.

    Constructed rather than rendered, because the failure has to be *forced*:
    the file that sorts first here deliberately holds the identity that sorts
    last, so this case fails before the fix on every run rather than half of
    them.
    """

    address = SemanticAddress.whole_artifact("claims/00/CLM-{}.yaml".format("0" * 32))
    high, low = f"sha256:{'f' * 64}", f"sha256:{'0' * 64}"

    def _region(region_id: str, path: str) -> NativeParsedRegionV1:
        return NativeParsedRegionV1(
            path=path,
            region_id=region_id,
            region_kind="statement_value",
            editable=True,
            address=address,
            state="dirty",
            baseline_digest=f"sha256:{'1' * 64}",
            observed_digest=f"sha256:{'2' * 64}",
            byte_length=3,
            line_overlay=CoverageLineOverlayV1(start_byte=0, end_byte=3, start_line=1, end_line=1),
        )

    parsed = NativeTreeParseV1(
        files=(
            NativeFileParseV1(path="a.md", tracked=True, regions=(_region(high, "a.md"),)),
            NativeFileParseV1(path="b.md", tracked=True, regions=(_region(low, "b.md"),)),
        )
    )

    # Presentation order is preserved where presentation is the question.
    assert [item.path for item in parsed.regions] == ["a.md", "b.md"]
    # Identity lists are canonical, and canonical is byte order over identities.
    assert parsed.dirty_region_ids == (low, high)
    assert parsed.dirty_region_ids == byte_sorted(parsed.dirty_region_ids)


def test_a_stash_body_and_its_parse_agree_on_region_order_whatever_the_paths(
    tmp_path: Path,
) -> None:
    """The regression the constructed case above generalizes, on a real render.

    Same two edits as the capture test, asserted as an ordering *law* rather
    than as an incidental equality: the stash body's committed order, the
    parse's identity list, and the canonical ordering are one order.
    """

    _state, _ctx, render = seeded_render(tmp_path)
    edited = _edit(dict(render.files), WI_42, READY, DONE)
    edited = _edit(edited, WI_43, BLOCKED, SHIPPED)
    parsed = parse_native_tree(edited, manifest=render.manifest)

    body = native_stash_body(edited, manifest=render.manifest)

    assert body is not None
    assert body.region_ids == byte_sorted(body.region_ids)
    assert parsed.dirty_region_ids == byte_sorted(parsed.dirty_region_ids)
    assert body.region_ids == parsed.dirty_region_ids
    # And the render plan, which is the third route to the same list.
    plan = plan_native_render(edited, manifest=render.manifest, render=render, stash=True)
    assert plan.stashed_region_ids == body.region_ids


def test_a_stash_entry_commits_to_its_own_body_and_refuses_when_it_does_not(
    tmp_path: Path,
) -> None:
    """Local, but not believed because it is written down.

    Two properties in one: the same edits stashed twice produce one identity
    regardless of when somebody typed the command, and a record whose digest no
    longer reproduces is refused rather than restored.
    """

    _state, _ctx, render = seeded_render(tmp_path)
    edited = _edit(dict(render.files), WI_42, READY, DONE)
    body = native_stash_body(edited, manifest=render.manifest)
    assert body is not None

    early = parse_native_stash(render_native_stash(body, written_at="2026-01-01T00:00:00+00:00"))
    late = parse_native_stash(render_native_stash(body, written_at="2026-12-31T00:00:00+00:00"))
    assert early.stash_digest == late.stash_digest == native_stash_digest(body).tagged
    assert early.written_at != late.written_at
    assert native_stash_entry_path(early.stash_digest).startswith(f"{NATIVE_STASH_DIRECTORY}/")

    forged = render_native_stash(body).replace(
        body.regions[0].body_base64.encode("ascii"),
        b"dGFtcGVyZWQK",
    )
    with pytest.raises(NativeStashError):
        parse_native_stash(forged)


# -- restoring by identity -------------------------------------------------


def test_restoring_puts_the_stashed_edit_back_byte_for_byte(tmp_path: Path) -> None:
    """Stash, re-render over the edit, restore: the tree is the edited tree again."""

    _state, _ctx, render = seeded_render(tmp_path)
    edited = _edit(dict(render.files), WI_42, READY, DONE)
    entry = _stashed(edited, render.manifest)

    restored = restore_native_stash(dict(render.files), manifest=render.manifest, stash=entry)

    assert restored.restored_region_ids == entry.body.region_ids
    assert restored.unresolved_region_ids == ()
    assert restored.write_paths == (WI_42,)
    assert restored.files[WI_42] == edited[WI_42]


def test_restoring_lands_after_the_file_it_was_edited_in_moved(tmp_path: Path) -> None:
    """Region identity carries no path, so a move is not a lost stash.

    §11.9.3 makes paths presentation coordinates over source-occurrence
    identity. A restore that matched on the recorded path would fail here; one
    that matches on identity puts the edit exactly where the field went.
    """

    _state, _ctx, render = seeded_render(tmp_path)
    edited = _edit(dict(render.files), WI_42, READY, DONE)
    entry = _stashed(edited, render.manifest)
    assert entry.body.regions[0].path == WI_42

    moved = dict(render.files)
    relocated = "subjects/project.work_item/renamed.md"
    moved[relocated] = moved.pop(WI_42)

    restored = restore_native_stash(moved, manifest=render.manifest, stash=entry)

    assert restored.restored_region_ids == entry.body.region_ids
    assert restored.write_paths == (relocated,)
    assert DONE in restored.files[relocated]


def test_restoring_a_region_the_render_no_longer_has_reports_it(tmp_path: Path) -> None:
    """A stash whose field is gone says so; it never guesses at a position."""

    _state, _ctx, render = seeded_render(tmp_path)
    edited = _edit(dict(render.files), WI_42, READY, DONE)
    entry = _stashed(edited, render.manifest)

    without = {path: content for path, content in render.files.items() if path != WI_42}
    restored = restore_native_stash(without, manifest=render.manifest, stash=entry)

    assert restored.restored_region_ids == ()
    assert restored.unresolved_region_ids == entry.body.region_ids
    assert restored.write_paths == ()
    assert restored.files == {}
    assert [item.code for item in restored.diagnostics] == ["stash_region_absent"]


def test_restoring_onto_a_region_that_binds_nothing_refuses_rather_than_splicing(
    tmp_path: Path,
) -> None:
    """A duplicated locator binds neither occurrence, so neither is a target."""

    _state, _ctx, render = seeded_render(tmp_path)
    edited = _edit(dict(render.files), WI_42, READY, DONE)
    entry = _stashed(edited, render.manifest)

    ambiguous = dict(render.files)
    ambiguous["subjects/project.work_item/wi-42-copy.md"] = render.files[WI_42]

    restored = restore_native_stash(ambiguous, manifest=render.manifest, stash=entry)

    assert restored.restored_region_ids == ()
    assert restored.unresolved_region_ids == entry.body.region_ids
    assert [item.code for item in restored.diagnostics] == ["stash_region_not_bound"]
    assert restored.diagnostics[0].severity == "refusal"


def test_two_stashed_regions_in_one_file_both_land(tmp_path: Path) -> None:
    """Splices run backwards through a file, so one restore cannot move another."""

    instance, _owner = seed_native_instance(tmp_path)
    state = native_state(instance)
    render = build_native_render(state, native_context(state))
    edited = _edit(dict(render.files), WI_42, READY, DONE)
    edited = _edit(edited, WI_42, b"\n(none)\n", b"\nas of the standup\n")
    entry = _stashed(edited, render.manifest)
    assert len(entry.body.regions) == 2

    restored = restore_native_stash(dict(render.files), manifest=render.manifest, stash=entry)

    assert restored.restored_region_ids == entry.body.region_ids
    assert restored.files[WI_42] == edited[WI_42]


# -- identifiers -----------------------------------------------------------


def test_a_stash_identifier_may_be_abbreviated_but_never_ambiguously(
    tmp_path: Path,
) -> None:
    """A local convenience may shorten an identifier; it may not choose for you."""

    _state, _ctx, render = seeded_render(tmp_path)
    first = _stashed(_edit(dict(render.files), WI_42, READY, DONE), render.manifest)
    second = _stashed(_edit(dict(render.files), WI_43, BLOCKED, SHIPPED), render.manifest)
    entries = [first, second]

    assert resolve_native_stash(entries, first.stash_digest) is first
    assert resolve_native_stash(entries, first.stash_digest.split(":", 1)[1]) is first
    assert resolve_native_stash(entries, first.stash_digest[7:19]) is first

    with pytest.raises(NativeStashError):
        resolve_native_stash(entries, "sha256:" + "0" * 64)
    with pytest.raises(NativeStashError):
        resolve_native_stash(entries, "abc")
    with pytest.raises(NativeStashError, match="more than one"):
        resolve_native_stash([first, first], first.stash_digest)
