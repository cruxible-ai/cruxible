"""The adapter layer: declared bindings, observed bytes, and the five forms.

The adapter is where a filesystem meets coverage and where it stops. These pin
the two properties that make that safe -- a binding is declared rather than
inferred, and an observation's digest reproduces from its own bytes -- plus the
mechanical reduction of §11.7's five request forms to spans.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_core.playbill.coverage.adapter import (
    WorkingPathBindingsV1,
    WorkingPathBindingV1,
    WorkingSourceObservationV1,
    build_overlay,
    coverage_span_requests,
    observations_for_grep_hits,
    observe_working_path,
    observe_working_paths,
    observe_working_source,
    observed_commitment,
    parse_grep_batch,
    read_working_path,
    selection_for_lines,
    working_set_observations,
)
from cruxible_core.playbill.coverage.contracts import (
    CoverageError,
    LogicalSourceIdentityV1,
    occurrence_identity_digest,
)
from tests.test_playbill._coverage_support import (
    CITED,
    EPILOGUE,
    HANDBOOK,
    PREAMBLE,
    SCRATCH,
    capture,
    index,
    sha256,
)

HANDBOOK_BODY = PREAMBLE + CITED + EPILOGUE
HANDBOOK_PATH = "docs/handbook.md"
SCRATCH_PATH = "notes/scratch.txt"


def _bindings() -> WorkingPathBindingsV1:
    return WorkingPathBindingsV1(
        bindings=(
            WorkingPathBindingV1(path=HANDBOOK_PATH, source=HANDBOOK),
            WorkingPathBindingV1(path=SCRATCH_PATH, source=SCRATCH),
        )
    )


def _workspace(tmp_path: Path) -> Path:
    (tmp_path / "docs").mkdir(parents=True)
    (tmp_path / "notes").mkdir(parents=True)
    (tmp_path / HANDBOOK_PATH).write_bytes(HANDBOOK_BODY)
    (tmp_path / SCRATCH_PATH).write_bytes(b"Notes to self.\nNothing governed.\n")
    return tmp_path


# -- binding is declared, never inferred -----------------------------------


def test_an_undeclared_working_path_is_refused_rather_than_guessed() -> None:
    bindings = _bindings()

    with pytest.raises(CoverageError) as refusal:
        bindings.source_for("docs/handbook-copy.md")

    assert "no declared logical source binding" in str(refusal.value)


def test_two_working_paths_may_not_claim_the_same_logical_source() -> None:
    with pytest.raises(ValidationError):
        WorkingPathBindingsV1(
            bindings=(
                WorkingPathBindingV1(path=HANDBOOK_PATH, source=HANDBOOK),
                WorkingPathBindingV1(path="docs/copy.md", source=HANDBOOK),
            )
        )


def test_one_working_path_may_not_claim_two_logical_sources() -> None:
    with pytest.raises(ValidationError):
        WorkingPathBindingsV1(
            bindings=(
                WorkingPathBindingV1(path=HANDBOOK_PATH, source=HANDBOOK),
                WorkingPathBindingV1(path=HANDBOOK_PATH, source=SCRATCH),
            )
        )


def test_a_bound_path_may_not_be_absolute_or_traverse_upward() -> None:
    for path in ("/etc/passwd", "../outside.md"):
        with pytest.raises(ValidationError):
            WorkingPathBindingV1(path=path, source=SCRATCH)


def test_a_working_path_may_not_be_read_from_outside_its_declared_root(tmp_path: Path) -> None:
    root = _workspace(tmp_path / "workspace")
    (tmp_path / "outside.md").write_bytes(b"not in the working set\n")

    with pytest.raises(CoverageError):
        read_working_path("../outside.md", root=root)


# -- observed bytes --------------------------------------------------------


def test_an_observation_hashes_the_bytes_it_actually_read(tmp_path: Path) -> None:
    root = _workspace(tmp_path)

    observation = observe_working_path(HANDBOOK_PATH, bindings=_bindings(), root=root)

    assert observation.source == HANDBOOK
    assert observation.content == HANDBOOK_BODY
    assert observation.content_digest == sha256(HANDBOOK_BODY)
    assert observation.content_digest == observed_commitment(HANDBOOK_BODY)
    assert observation.byte_length == len(HANDBOOK_BODY)


def test_a_declared_digest_that_does_not_reproduce_is_refused() -> None:
    honest = observe_working_source(HANDBOOK, HANDBOOK_BODY)

    with pytest.raises(ValidationError):
        WorkingSourceObservationV1(
            source=HANDBOOK,
            content_base64=honest.content_base64,
            content_digest=sha256(b"different bytes entirely"),
            byte_length=honest.byte_length,
        )


def test_a_declared_byte_length_that_does_not_match_is_refused() -> None:
    honest = observe_working_source(HANDBOOK, HANDBOOK_BODY)

    with pytest.raises(ValidationError):
        WorkingSourceObservationV1(
            source=HANDBOOK,
            content_base64=honest.content_base64,
            content_digest=honest.content_digest,
            byte_length=honest.byte_length + 1,
        )


# -- the five request forms ------------------------------------------------


def test_a_line_range_becomes_the_byte_window_it_covers() -> None:
    content = b"first\nsecond\nthird\n"

    selection = selection_for_lines(content, start_line=2, end_line=2)

    assert content[selection.start_byte : selection.end_byte] == b"second\n"
    whole = selection_for_lines(content, start_line=1, end_line=3)
    assert whole.start_byte == 0
    assert whole.end_byte == len(content)


def test_a_line_range_past_the_observed_content_is_refused() -> None:
    with pytest.raises(CoverageError):
        selection_for_lines(b"only one line\n", start_line=9, end_line=9)


def test_changed_paths_are_observed_whole_and_ask_one_span_each(tmp_path: Path) -> None:
    root = _workspace(tmp_path)

    observations = observe_working_paths(
        (SCRATCH_PATH, HANDBOOK_PATH, HANDBOOK_PATH),
        bindings=_bindings(),
        root=root,
    )
    spans = coverage_span_requests(observations)

    assert len(observations) == 2
    assert all(item.selections == () for item in observations)
    assert [span.source for span in spans] == [HANDBOOK, SCRATCH]
    assert all(span.selection is None for span in spans)


def test_a_grep_batch_collapses_to_one_observation_per_file_with_a_window_per_hit(
    tmp_path: Path,
) -> None:
    root = _workspace(tmp_path)
    hits = parse_grep_batch(
        f"{HANDBOOK_PATH}:3:The reviewer accepted\n"
        f"{HANDBOOK_PATH}:1:# Handbook\n"
        f"{SCRATCH_PATH}:2:Nothing governed\n"
    )

    observations = observations_for_grep_hits(hits, bindings=_bindings(), root=root)
    spans = coverage_span_requests(observations)

    assert [item.source for item in observations] == [HANDBOOK, SCRATCH]
    assert len(observations[0].selections) == 2
    # One operation, three spans -- never one operation per grep line.
    assert len(spans) == 3
    assert all(span.selection is not None for span in spans)


def test_a_malformed_grep_batch_line_is_refused() -> None:
    with pytest.raises(CoverageError):
        parse_grep_batch("docs/handbook.md-not-a-hit\n")


def test_the_working_set_scope_form_observes_every_declared_path(tmp_path: Path) -> None:
    root = _workspace(tmp_path)

    observations = working_set_observations(bindings=_bindings(), root=root)

    assert {item.source for item in observations} == {HANDBOOK, SCRATCH}


# -- the overlay the operation consumes ------------------------------------


def test_the_overlay_finds_cited_content_that_moved_and_keeps_its_identity() -> None:
    citations = index(capture(HANDBOOK, CITED))
    wanted = citations.wanted_selections()

    before = build_overlay((observe_working_source(HANDBOOK, PREAMBLE + CITED),), wanted=wanted)
    after = build_overlay(
        (observe_working_source(HANDBOOK, PREAMBLE + EPILOGUE + CITED),),
        wanted=wanted,
    )

    expected = occurrence_identity_digest(
        source=HANDBOOK,
        observed_commitment_digest=sha256(CITED),
        ordinal=0,
    )
    for overlay in (before, after):
        moved = next(
            item for item in overlay.occurrences if item.observed_commitment_digest == sha256(CITED)
        )
        assert moved.identity_digest == expected
    # Only the presentation overlay moved.
    assert before.occurrences != after.occurrences


def test_an_observation_of_an_unknown_source_still_reports_its_whole_commitment() -> None:
    unknown = LogicalSourceIdentityV1(plane="external", identity="workspace.unknown")

    overlay = build_overlay((observe_working_source(unknown, b"nothing governed\n"),))

    assert overlay.commitment_for(unknown) is not None
    assert overlay.scope == (unknown,)
