"""The §11.7 owned-harness middleware, headless.

Everything here runs without a daemon: the middleware takes its resolve callable
by injection, so a stub answers it and the tests are about the *adapter* --
configuration, binding, the four event reductions, fail-open, and the integrity
rules -- rather than about coverage semantics, which are the resolver's and are
proved in the resolver's own suites.

The one thing a stub cannot fake is the reference-parity law, and the tests
below assert it rather than duplicating it: the middleware's rendered lines are
compared against `render_coverage_result` on the same result object, so a
divergence fails here even though no expected string is written down twice.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_core.playbill.coverage.adapter import (
    WorkingSourceObservationV1,
    coverage_span_requests,
)
from cruxible_core.playbill.coverage.contracts import (
    CoverageError,
    CoverageRequestV1,
    CoverageResultV3,
)
from cruxible_core.playbill.coverage.middleware import (
    CONFIG_RELATIVE_PATH,
    CoverageExactPathRuleV1,
    CoveragePathPrefixRuleV1,
    CoverageWorkspaceConfigV1,
    CoverageWorkspaceConfigV2,
    FloorGenerationPairV1,
    FloorOutputV1,
    HarnessLineRangeV1,
    HarnessToolEventV1,
    coverage_middleware,
    grep_event,
    load_coverage_config,
)
from cruxible_core.playbill.coverage.render import (
    UNAVAILABLE_NOTE_PREFIX,
    render_coverage_result,
)
from cruxible_core.playbill.coverage.resolver import resolve_coverage_v3
from tests.test_playbill._coverage_support import (
    INSTANCE_ID,
    coordinate,
    index_v2,
    manifest_v2,
    overlay,
    profile,
)

CORPUS_BYTES = b"# Handbook\n\nThe reviewer accepted the plan.\nFiled by the group.\n"
NOTES_BYTES = b"ordinary working notes\n"


class _Recorder:
    """A resolve callable that records its input and answers through the real resolver.

    Deliberately not a stubbed result object. The middleware's job is to hand
    the operation well-formed observations and render what comes back, and a
    fabricated `CoverageResultV3` would prove neither: this runs the genuine
    resolver over an empty accepted ledger, so the spans really are `none`, the
    summary really is computed, and the parity and integrity assertions below
    run against bytes the reference renderer actually produced.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[WorkingSourceObservationV1, ...]] = []

    def __call__(
        self,
        observations: Sequence[WorkingSourceObservationV1],
    ) -> CoverageResultV3:
        ordered = tuple(observations)
        self.calls.append(ordered)
        citations = index_v2()
        snapshot = overlay(*(item.material for item in ordered), citations=citations)
        return resolve_coverage_v3(
            CoverageRequestV1(
                instance_id=INSTANCE_ID,
                at=coordinate(),
                spans=coverage_span_requests(ordered),
            ),
            index=citations,
            overlay=snapshot,
            access=profile(),
            manifest=manifest_v2(citations, snapshot),
        )

    @property
    def observed(self) -> tuple[WorkingSourceObservationV1, ...]:
        assert len(self.calls) == 1, f"expected exactly one resolve, got {len(self.calls)}"
        return self.calls[0]


def _config(**overrides: object) -> CoverageWorkspaceConfigV1:
    base: dict[str, object] = {
        "rules": (
            CoveragePathPrefixRuleV1(
                path_prefix="corpus/",
                plane="external",
                identity_prefix="corpus.",
            ),
        )
    }
    base.update(overrides)
    return CoverageWorkspaceConfigV1(**base)  # type: ignore[arg-type]


def _floor_config(**overrides: object) -> CoverageWorkspaceConfigV2:
    base: dict[str, object] = {
        "floor_output": FloorOutputV1(path="playbill-floor"),
    }
    base.update(overrides)
    return CoverageWorkspaceConfigV2(**base)  # type: ignore[arg-type]


def _write_floor_manifest(workspace: Path) -> None:
    floor = workspace / "playbill-floor"
    floor.mkdir(exist_ok=True)
    (floor / "manifest.json").write_text(
        json.dumps(
            {
                "tag": "playbill-floor-manifest-v2",
                "format": "playbill-floor-export-v2",
                "coordinate": coordinate().model_dump(mode="json"),
                "files": [],
                "floor_digest": typed_digest(
                    Sha256Value,
                    "playbill-floor-export-v2",
                    {"files": []},
                ).tagged,
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "corpus").mkdir()
    (tmp_path / "corpus" / "handbook.md").write_bytes(CORPUS_BYTES)
    (tmp_path / "notes.txt").write_bytes(NOTES_BYTES)
    return tmp_path


# -- configuration and binding ---------------------------------------------


def test_the_path_normalizer_is_non_lossy_and_keeps_the_extension() -> None:
    """`corpus/handbook.md` is `corpus.handbook.md`, not `corpus.handbook`.

    Dropping the extension would let `handbook.md` and `handbook.txt` collide
    onto one accepted source, which is the cross-source confusion §11.6.1 exists
    to prevent arriving through the configuration file instead of the resolver.
    """

    config = _config()

    assert config.source_for("corpus/handbook.md") is not None
    assert config.source_for("corpus/handbook.md").identity == "corpus.handbook.md"  # type: ignore[union-attr]
    assert config.source_for("corpus/deep/note.md").identity == "corpus.deep.note.md"  # type: ignore[union-attr]
    # Two files that a lossy normalizer would have merged stay distinct.
    assert config.source_for("corpus/handbook.txt").identity == "corpus.handbook.txt"  # type: ignore[union-attr]


def test_an_exact_rule_beats_a_prefix_rule_and_the_longest_prefix_wins() -> None:
    config = CoverageWorkspaceConfigV1(
        rules=(
            CoveragePathPrefixRuleV1(path_prefix="corpus/", plane="external", identity_prefix="a."),
            CoveragePathPrefixRuleV1(
                path_prefix="corpus/deep/", plane="external", identity_prefix="b."
            ),
            CoverageExactPathRuleV1(
                path="corpus/deep/pinned.md", plane="external", identity="pinned.source"
            ),
        )
    )

    assert config.source_for("corpus/x.md").identity == "a.x.md"  # type: ignore[union-attr]
    assert config.source_for("corpus/deep/x.md").identity == "b.x.md"  # type: ignore[union-attr]
    assert config.source_for("corpus/deep/pinned.md").identity == "pinned.source"  # type: ignore[union-attr]


def test_an_identity_the_frozen_grammar_refuses_binds_nothing_rather_than_raising() -> None:
    """An unusable identity is an unbound path, and unbound is silent."""

    config = CoveragePathPrefixRuleV1(
        path_prefix="corpus/", plane="external", identity_prefix="Corpus."
    )
    # Capital letters are outside the frozen external-identity grammar.
    assert config.identity_for("corpus/handbook.md") == "Corpus.handbook.md"
    assert CoverageWorkspaceConfigV1(rules=(config,)).source_for("corpus/handbook.md") is None


def test_ambiguous_rules_are_refused_at_load_rather_than_resolved_by_file_order() -> None:
    with pytest.raises(ValueError, match="at most one exact coverage rule"):
        CoverageWorkspaceConfigV1(
            rules=(
                CoverageExactPathRuleV1(path="a.md", plane="external", identity="one"),
                CoverageExactPathRuleV1(path="a.md", plane="external", identity="two"),
            )
        )
    with pytest.raises(ValueError, match="at most one coverage rule"):
        CoverageWorkspaceConfigV1(
            rules=(
                CoveragePathPrefixRuleV1(path_prefix="c/", plane="external", identity_prefix="a."),
                CoveragePathPrefixRuleV1(path_prefix="c/", plane="external", identity_prefix="b."),
            )
        )


def test_the_config_round_trips_through_the_committed_file_shape(workspace: Path) -> None:
    (workspace / ".playbill").mkdir()
    (workspace / CONFIG_RELATIVE_PATH).write_text(
        json.dumps(
            {
                "instance_id": "inst_0123456789abcdef",
                "scan_budget": {"max_scanned_bytes": 4096},
                "max_observed_paths": 8,
                "rules": [
                    {
                        "tag": "playbill-coverage-path-prefix-rule-v1",
                        "path_prefix": "corpus/",
                        "plane": "external",
                        "identity_prefix": "corpus.",
                        "normalizer": "playbill-coverage-path-identity-v1",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_coverage_config(workspace)

    assert loaded.instance_id == "inst_0123456789abcdef"
    assert loaded.source_for("corpus/handbook.md").identity == "corpus.handbook.md"  # type: ignore[union-attr]
    # Both budgets survive the round trip: the scan budget rides to the
    # operation with the caller's resolver, and the path bound is applied here.
    assert loaded.scan_budget is not None
    assert loaded.scan_budget.max_scanned_bytes == 4096
    assert loaded.max_observed_paths == 8


def test_v2_config_adds_only_the_optional_normalized_floor_output(workspace: Path) -> None:
    (workspace / ".playbill").mkdir()
    (workspace / CONFIG_RELATIVE_PATH).write_text(
        json.dumps(
            {
                "tag": "playbill-coverage-workspace-config-v2",
                "floor_output": {
                    "tag": "playbill-floor-output-v1",
                    "path": "playbill-floor",
                    "format": "playbill-floor-export-v2",
                },
            }
        ),
        encoding="utf-8",
    )

    loaded = load_coverage_config(workspace)

    assert isinstance(loaded, CoverageWorkspaceConfigV2)
    assert loaded.floor_output == FloorOutputV1(path="playbill-floor")


@pytest.mark.parametrize(
    "path",
    ("", ".", "/tmp/floor", "../floor", "a/../floor", ".playbill/floor", "a\\floor"),
)
def test_floor_output_refuses_non_normalized_or_reserved_paths(path: str) -> None:
    with pytest.raises(ValueError):
        FloorOutputV1(path=path)


def test_the_declared_path_bound_clips_how_many_sources_one_event_observes(
    workspace: Path,
) -> None:
    """A runaway event may not turn into an unbounded working set."""

    for index_ in range(5):
        (workspace / "corpus" / f"file{index_}.md").write_bytes(b"body\n")
    recorder = _Recorder()
    middleware = coverage_middleware(
        root=workspace,
        config=_config(max_observed_paths=2),
        resolve=recorder,
    )

    middleware.after_tool(
        HarnessToolEventV1(
            kind="read",
            paths=tuple(f"corpus/file{index_}.md" for index_ in range(5)),
        )
    )

    assert len(recorder.observed) == 2


def test_a_missing_configuration_is_a_typed_refusal_not_a_silent_default(tmp_path: Path) -> None:
    with pytest.raises(CoverageError, match="could not be read"):
        load_coverage_config(tmp_path)


# -- the four event reductions ---------------------------------------------


def test_a_whole_file_read_observes_the_whole_source(workspace: Path) -> None:
    recorder = _Recorder()
    middleware = coverage_middleware(root=workspace, config=_config(), resolve=recorder)

    middleware.after_tool(HarnessToolEventV1(kind="read", paths=("corpus/handbook.md",)))

    observation = recorder.observed[0]
    assert observation.source.identity == "corpus.handbook.md"
    assert observation.content == CORPUS_BYTES
    assert observation.selections == ()


def test_a_windowed_read_becomes_one_byte_window_over_the_bytes_that_were_read(
    workspace: Path,
) -> None:
    """Line numbers enter at the adapter and stop there."""

    recorder = _Recorder()
    middleware = coverage_middleware(root=workspace, config=_config(), resolve=recorder)

    middleware.after_tool(
        HarnessToolEventV1(
            kind="read",
            ranges=(HarnessLineRangeV1(path="corpus/handbook.md", start_line=3, end_line=3),),
        )
    )

    selection = recorder.observed[0].selections[0]
    start = CORPUS_BYTES.index(b"The reviewer")
    assert (selection.start_byte, selection.end_byte) == (
        start,
        start + len(b"The reviewer accepted the plan.\n"),
    )


def test_a_grep_batch_collapses_to_one_observation_per_file_with_a_window_per_hit(
    workspace: Path,
) -> None:
    """One operation for the batch, which is what makes the one-summary rule reachable."""

    recorder = _Recorder()
    middleware = coverage_middleware(root=workspace, config=_config(), resolve=recorder)

    middleware.after_tool(
        grep_event("corpus/handbook.md:1:# Handbook\ncorpus/handbook.md:3:The reviewer accepted")
    )

    assert len(recorder.observed) == 1
    assert len(recorder.observed[0].selections) == 2


def test_an_edit_or_write_observes_the_changed_path_whole(workspace: Path) -> None:
    """A changed path is asked about whole: nobody has to guess which window moved."""

    for kind in ("edit", "write"):
        recorder = _Recorder()
        middleware = coverage_middleware(root=workspace, config=_config(), resolve=recorder)

        middleware.after_tool(
            HarnessToolEventV1(kind=kind, paths=("corpus/handbook.md",))  # type: ignore[arg-type]
        )

        assert recorder.observed[0].selections == (), kind
        assert recorder.observed[0].content == CORPUS_BYTES, kind


def test_after_filesystem_change_is_the_changed_paths_form(workspace: Path) -> None:
    recorder = _Recorder()
    middleware = coverage_middleware(root=workspace, config=_config(), resolve=recorder)

    middleware.after_filesystem_change(("corpus/handbook.md",))

    assert recorder.observed[0].source.identity == "corpus.handbook.md"


def test_before_tool_answers_but_never_renders(workspace: Path) -> None:
    """Nothing has been output yet, so there is nothing to annotate."""

    recorder = _Recorder()
    middleware = coverage_middleware(root=workspace, config=_config(), resolve=recorder)

    delivery = middleware.before_tool(
        HarnessToolEventV1(kind="edit", paths=("corpus/handbook.md",), original_output="tool said")
    )

    assert delivery.lines == ()
    assert delivery.appended_coverage_text == ""
    assert delivery.original_output == "tool said"
    assert delivery.result is not None


# -- silence, integrity, and parity ----------------------------------------


def test_an_unbound_path_is_dropped_silently_and_never_named(workspace: Path) -> None:
    """No card, no note, no mention -- and no false `none` about it either."""

    recorder = _Recorder()
    middleware = coverage_middleware(root=workspace, config=_config(), resolve=recorder)

    delivery = middleware.after_tool(
        HarnessToolEventV1(kind="read", paths=("notes.txt",), original_output="notes body")
    )

    assert recorder.calls == []
    assert delivery.lines == ()
    assert delivery.spliced() == "notes body"
    assert "notes.txt" not in delivery.spliced()
    # Recorded for a harness that wants to measure configuration coverage, and
    # never rendered.
    assert delivery.unbound_paths == ("notes.txt",)


def test_a_partly_bound_event_asks_only_about_the_bound_half(workspace: Path) -> None:
    recorder = _Recorder()
    middleware = coverage_middleware(root=workspace, config=_config(), resolve=recorder)

    delivery = middleware.after_tool(
        HarnessToolEventV1(kind="read", paths=("corpus/handbook.md", "notes.txt"))
    )

    assert [item.source.identity for item in recorder.observed] == ["corpus.handbook.md"]
    assert delivery.unbound_paths == ("notes.txt",)


def test_the_original_output_is_returned_byte_identical_and_the_delta_is_pure_addendum(
    workspace: Path,
) -> None:
    """§11.8: preserved and annotated, never replaced or suppressed."""

    recorder = _Recorder()
    middleware = coverage_middleware(root=workspace, config=_config(), resolve=recorder)
    original = "corpus/handbook.md:3:The reviewer accepted the plan."

    delivery = middleware.after_tool(grep_event("corpus/handbook.md:3:x", original_output=original))

    assert delivery.original_output == original
    spliced = delivery.spliced()
    assert spliced.startswith(original)
    # The whole delta is the rendered lines and nothing else.
    assert spliced[len(original) :] == "\n" + delivery.appended_coverage_text


def test_the_rendered_delta_is_exactly_the_reference_rendering(workspace: Path) -> None:
    """The parity law, asserted rather than duplicated."""

    recorder = _Recorder()
    middleware = coverage_middleware(root=workspace, config=_config(), resolve=recorder)

    delivery = middleware.after_tool(HarnessToolEventV1(kind="read", paths=("corpus/handbook.md",)))

    assert delivery.result is not None
    assert delivery.lines == render_coverage_result(delivery.result)


def test_the_delta_contains_no_source_bytes(workspace: Path) -> None:
    """§11.8: hooks never inject source bodies or hidden hits."""

    recorder = _Recorder()
    middleware = coverage_middleware(root=workspace, config=_config(), resolve=recorder)

    delivery = middleware.after_tool(HarnessToolEventV1(kind="read", paths=("corpus/handbook.md",)))

    body = CORPUS_BYTES.decode("utf-8")
    for line in body.splitlines():
        if line.strip():
            assert line not in delivery.appended_coverage_text


def test_the_same_event_against_the_same_state_renders_identical_bytes(workspace: Path) -> None:
    middleware = coverage_middleware(root=workspace, config=_config(), resolve=_Recorder())
    event = HarnessToolEventV1(kind="read", paths=("corpus/handbook.md",), original_output="body")

    first = middleware.after_tool(event)
    second = middleware.after_tool(event)

    assert first.spliced() == second.spliced()


def test_floor_hits_are_freshness_only_and_a_stale_floor_gets_one_batch_line(
    workspace: Path,
) -> None:
    _write_floor_manifest(workspace)
    recorder = _Recorder()
    config = _floor_config(
        rules=(
            CoveragePathPrefixRuleV1(
                path_prefix="playbill-floor/",
                plane="external",
                identity_prefix="floor.",
            ),
        )
    )
    middleware = coverage_middleware(
        root=workspace,
        config=config,
        resolve=recorder,
        resolve_floor_generations=lambda _: FloorGenerationPairV1(
            floor_generation=2,
            current_generation=4,
        ),
    )

    delivery = middleware.after_tool(
        HarnessToolEventV1(
            kind="read",
            paths=("playbill-floor/manifest.json", "playbill-floor/other.json"),
            original_output="floor bytes",
        )
    )

    assert recorder.calls == []
    assert delivery.lines == ("floor at generation 2, current 4; re-export required",)
    assert delivery.unbound_paths == (
        "playbill-floor/manifest.json",
        "playbill-floor/other.json",
    )
    assert delivery.spliced() == (
        "floor bytes\nfloor at generation 2, current 4; re-export required"
    )


def test_current_floor_is_silent_and_invalid_floor_is_explicitly_unavailable(
    workspace: Path,
) -> None:
    _write_floor_manifest(workspace)
    event = HarnessToolEventV1(kind="grep", paths=("playbill-floor/manifest.json",))
    current = coverage_middleware(
        root=workspace,
        config=_floor_config(),
        resolve=_Recorder(),
        resolve_floor_generations=lambda _: FloorGenerationPairV1(
            floor_generation=4,
            current_generation=4,
        ),
    ).after_tool(event)
    assert current.lines == ()

    (workspace / "playbill-floor/manifest.json").write_text("{}", encoding="utf-8")
    unavailable = coverage_middleware(
        root=workspace,
        config=_floor_config(),
        resolve=_Recorder(),
        resolve_floor_generations=lambda _: FloorGenerationPairV1(
            floor_generation=4,
            current_generation=4,
        ),
    ).after_tool(event)
    assert unavailable.lines == ("floor freshness unavailable",)


# -- fail open on infrastructure -------------------------------------------


def test_an_unreachable_resolver_degrades_to_the_original_plus_one_line(
    workspace: Path,
) -> None:
    def explode(_: Sequence[WorkingSourceObservationV1]) -> CoverageResultV3:
        raise RuntimeError("connection refused")

    middleware = coverage_middleware(root=workspace, config=_config(), resolve=explode)

    delivery = middleware.after_tool(
        HarnessToolEventV1(kind="read", paths=("corpus/handbook.md",), original_output="body")
    )

    assert delivery.failed_open is True
    assert delivery.failure_code == "coverage_operation_unavailable"
    assert delivery.original_output == "body"
    assert len(delivery.lines) == 1
    assert delivery.lines[0].startswith(UNAVAILABLE_NOTE_PREFIX)
    # Failing open on infrastructure never means guessing a match state.
    assert delivery.result is None


def test_an_unreadable_working_file_degrades_the_same_way(workspace: Path) -> None:
    recorder = _Recorder()
    middleware = coverage_middleware(root=workspace, config=_config(), resolve=recorder)

    delivery = middleware.after_tool(
        HarnessToolEventV1(kind="read", paths=("corpus/absent.md",), original_output="body")
    )

    assert delivery.failure_code == "working_source_unreadable"
    assert delivery.original_output == "body"
    assert recorder.calls == []


def test_a_degraded_delivery_carries_no_cards_at_all(workspace: Path) -> None:
    """Fail open on infrastructure, fail closed on semantics."""

    def explode(_: Sequence[WorkingSourceObservationV1]) -> CoverageResultV3:
        raise RuntimeError("boom")

    middleware = coverage_middleware(root=workspace, config=_config(), resolve=explode)

    delivery = middleware.after_tool(HarnessToolEventV1(kind="edit", paths=("corpus/handbook.md",)))

    assert delivery.result is None
    assert "exact" not in delivery.appended_coverage_text
    assert "drifted" not in delivery.appended_coverage_text
    assert "candidate" not in delivery.appended_coverage_text
