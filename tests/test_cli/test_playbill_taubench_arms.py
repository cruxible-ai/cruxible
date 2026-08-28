"""PC-G-H3: the committed §11.8 TauBench arm recipe, run end to end.

`benchmarks/playbill_taubench/` claims that a TauBench integrator needs only
that directory: the recipe, the example seed bundle, and the README. This is the
proof of that claim, and it is a proof rather than an illustration because it
imports the committed recipe and calls its steps -- there is no second
implementation of any of them here.

The scale is miniature (three Claims, one entrypoint, a three-file corpus, one
scripted turn) so it runs in the ordinary suite instead of being a benchmark
nobody runs. What it exercises is not scale:

* the seed bundle authored through the surviving governed writers, one intent or
  proposal at a time,
  with approval and activation as separate acts the harness performs;
* the arm file surface exported as floor-v2 artifacts and the coverage boundary
  in one greppable tree;
* all four §11.8 arms constructed, with arms 3 and 4 differing in exactly one
  boolean and producing byte-identical workspaces;
* the flagship §11.8 outcome: the same event stream yields identical raw tool
  output in both arms, and the drifted card appears only in arm 4;
* the run manifest carrying every field §11.8 requires pinned per run.
"""

from __future__ import annotations

import json
import sys
from dataclasses import fields
from pathlib import Path

import pytest

from tests.test_cli.test_playbill_knowledge_loop_smoke import (  # noqa: F401
    _Cli,
    served_cli,
)

BENCHMARK_DIR = Path(__file__).resolve().parents[2] / "benchmarks/playbill_taubench"
if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import recipe  # noqa: E402  -- the committed recipe, imported and driven, not copied

pytestmark = pytest.mark.taubench


@pytest.fixture
def arm_run(served_cli: _Cli, tmp_path: Path) -> dict[str, object]:  # noqa: F811
    """The whole recipe against one served instance.

    `served_cli` patches the CLI's client onto an in-process transport, so the
    recipe's own `run_cli` -- the same in-process `cruxible ...` invocation an
    operator's shell would make -- reaches this test's daemon with no injection
    seam of its own.
    """

    return recipe.run_all(tmp_path / "run", server_url="http://cruxible")


def _entry(transcript: tuple[dict[str, object], ...], kind: str) -> dict[str, object]:
    return next(item for item in transcript if item["kind"] == kind)


def test_the_seed_plan_is_readable_with_no_instance_daemon_or_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--plan` answers offline, which is what makes it a *plan*.

    Deliberately not using the served fixture: no remembered context, no client,
    no daemon. Reading a bundle and grouping it is a fact about committed bytes,
    so an integrator can inspect what a bundle would do before standing anything
    up -- and the plan digest they see is the one the run manifest later pins.
    """

    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "absent-context.json"))
    printed = "\n".join(
        recipe.plan_seed_directory(recipe.BUNDLE_DIR, proposal_name="offline").rendered
    )

    assert "9 proposal(s)" in printed
    assert "1. claim_type:project.work_item.status" in printed
    assert "5. claim_input:project.work_item/wi-101#project.work_item.status" in printed
    assert "8. query_definition:project.work_items" in printed
    assert "9. procedure:project.work_item.digest" in printed


def test_the_seed_bundle_uses_only_surviving_governed_writers(
    arm_run: dict[str, object],
) -> None:
    """Dependencies, Claims, query, and Procedure settle through nine writes."""

    manifest = arm_run["manifest"]
    assert isinstance(manifest, dict)
    groups = manifest["seed"]["groups"]

    assert [item["group_id"] for item in groups] == [
        "claim_type:project.work_item.status",
        "subject:project.work_item/wi-101",
        "subject:project.work_item/wi-102",
        "subject:project.work_item/wi-103",
        "claim_input:project.work_item/wi-101#project.work_item.status",
        "claim_input:project.work_item/wi-102#project.work_item.status",
        "claim_input:project.work_item/wi-103#project.work_item.status",
        "query_definition:project.work_items",
        "procedure:project.work_item.digest",
    ]
    assert [item["operation"] for item in groups] == [
        "playbill_propose_claim_type",
        "playbill_propose_subject",
        "playbill_propose_subject",
        "playbill_propose_subject",
        "playbill_authoring_submit",
        "playbill_authoring_submit",
        "playbill_authoring_submit",
        "playbill_propose_query_definition",
        "playbill_authoring_submit",
    ]
    # Each group settled its own generation, in the order the plan named them.
    coordinates = [item["accepted_coordinate"]["generation_root"] for item in groups]
    assert len(set(coordinates)) == len(coordinates)
    # The plan the run applied is the plan the bundle's committed bytes produce.
    assert manifest["seed"]["applied_plan_digest"] == manifest["seed"]["plan_digest"]

    # And the accepted world is readable through the ordinary reads.
    run = recipe.run_cli_json("playbill", "query", "run", "project.work_items")
    projected = {
        next(field["value"] for field in row["fields"] if field["name"] == "item_id"): next(
            field["value"] for field in row["fields"] if field["name"] == "status"
        )
        for row in run["result"]["rows"]
    }
    assert projected == {"wi-101": "done", "wi-102": "blocked", "wi-103": "ready"}


def test_the_arm_file_surface_is_floor_v2_artifacts_and_the_boundary(
    arm_run: dict[str, object],
) -> None:
    """One greppable pointer-model tree with no native projection residue."""

    setups = arm_run["setups"]
    assert isinstance(setups, dict)
    surface = setups[3].workspace / "playbill-floor"
    written = {
        path.relative_to(surface).as_posix() for path in surface.rglob("*") if path.is_file()
    }

    assert {"manifest.json", "coverage-manifest.json"} <= written
    assert any(item.endswith(".profile.json") for item in written)
    assert not any(item.endswith(".md") for item in written)
    assert "render-manifest.json" not in written
    floor = json.loads((surface / "manifest.json").read_text(encoding="utf-8"))
    assert floor["tag"] == "playbill-floor-manifest-v2"
    assert floor["format"] == "playbill-floor-export-v2"
    floor_paths = {item["path"] for item in floor["files"]}
    assert not any(path.endswith(".md") for path in floor_paths)
    assert "render-manifest.json" not in floor_paths
    assert not any(path.startswith("briefs/") for path in floor_paths)
    assert any(path.startswith("procedures/") for path in floor_paths)


def test_arms_three_and_four_are_identical_but_for_one_boolean(
    arm_run: dict[str, object],
) -> None:
    """The §11.8 sharing rule, asserted rather than described.

    "Arms 3 and 4 share the same model, harness loop, task corpus, accepted
    ledger, Playbill state, and tool implementations; only the coverage-delivery
    adapter changes." Two checks make that structural: the two arm records differ
    in exactly one field, and the two workspaces are byte-identical before the
    turn runs.
    """

    setups = arm_run["setups"]
    assert isinstance(setups, dict)
    third, fourth = setups[3], setups[4]

    differing = {
        field.name
        for field in fields(recipe.ArmSetupV1)
        if field.name not in {"arm", "label", "workspace", "middleware"}
        and getattr(third, field.name) != getattr(fourth, field.name)
    }
    assert differing == {"deliver_coverage"}
    assert (third.deliver_coverage, fourth.deliver_coverage) == (False, True)
    # Both hold a middleware; arm 3 simply never calls it.
    assert third.middleware is not None and fourth.middleware is not None
    assert third.middleware.config == fourth.middleware.config
    assert third.middleware.config.tag == "playbill-coverage-workspace-config-v2"
    assert third.middleware.config.floor_output is not None
    assert third.middleware.config.floor_output.format == "playbill-floor-export-v2"

    def tree(root: Path) -> dict[str, bytes]:
        return {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }

    # The turn already ran and edited the same governed line in both arms, so
    # the trees stay byte-identical after it too: the delivery adapter changed
    # what the model saw and nothing about what the tools did.
    assert tree(third.workspace) == tree(fourth.workspace)


def test_the_same_event_stream_produces_cards_in_arm_four_and_none_in_arm_three(
    arm_run: dict[str, object],
) -> None:
    """The flagship: identical raw outputs, one drifted card, same turn.

    Arm 3 receives exactly the tool's own output. Arm 4 receives that same
    string plus a pure addendum naming the affected Claim -- inside the same turn
    as the edit, with nothing compiled, proposed, or accepted.
    """

    transcripts = arm_run["transcripts"]
    assert isinstance(transcripts, dict)
    third, fourth = transcripts[3], transcripts[4]

    # Same events, same tools, same raw outputs.
    assert (
        [item["kind"] for item in third]
        == [item["kind"] for item in fourth]
        == [
            "read",
            "grep",
            "edit",
        ]
    )
    assert [item["original_output"] for item in third] == [
        item["original_output"] for item in fourth
    ]

    # Arm 3 says nothing at all, ever.
    assert all(item["coverage_lines"] == [] for item in third)
    assert all(item["model_visible_output"] == item["original_output"] for item in third)
    assert all(item["result"] is None for item in third)

    # Arm 4 reads the governed span as exact, then sees it drift on the edit.
    read = _entry(fourth, "read")
    assert read["result"] is not None
    assert read["result"].tag == "playbill-coverage-result-v3"
    assert read["result"].summary.exact == 1
    assert read["model_visible_output"].startswith(read["original_output"])
    assert "exact  external:corpus.handbook.md" in read["model_visible_output"]

    edit = _entry(fourth, "edit")
    assert edit["result"] is not None
    assert edit["result"].summary.drifted == 1
    card = edit["result"].spans[0].cards[0]
    assert card.match_state == "drifted"
    assert card.grants_mutation_authority is False
    # The original tool output is preserved and annotated, never replaced.
    assert edit["model_visible_output"] == (
        edit["original_output"] + "\n" + "\n".join(edit["coverage_lines"])
    )
    assert "drifted  external:corpus.handbook.md" in edit["model_visible_output"]
    assert card.claim_addresses[0].artifact_path in edit["model_visible_output"]

    # Nothing accepted moved across the turn.
    assert edit["result"].at == read["result"].at


def test_arms_one_and_two_carry_the_corpus_and_arm_two_carries_a_scratchpad(
    arm_run: dict[str, object],
) -> None:
    """The controls: ordinary files, and ordinary files plus a real notebook."""

    setups = arm_run["setups"]
    assert isinstance(setups, dict)
    first, second = setups[1], setups[2]

    assert first.middleware is None and second.middleware is None
    assert not first.deliver_coverage and not second.deliver_coverage
    assert (first.workspace / recipe.GOVERNED_PATH).is_file()
    assert not (first.workspace / "scratchpad").exists()
    assert (second.workspace / "scratchpad/NOTES.md").is_file()
    # Neither control gets the Playbill surface or a binding configuration.
    for setup in (first, second):
        assert not (setup.workspace / "playbill-floor").exists()
        assert not (setup.workspace / ".playbill").exists()
    # The task corpus is the same bytes in every arm, which is what makes the
    # comparison a comparison.
    assert (first.workspace / recipe.GOVERNED_PATH).read_bytes() == (
        second.workspace / recipe.GOVERNED_PATH
    ).read_bytes()


def test_the_run_manifest_pins_every_field_the_evaluation_requires(
    arm_run: dict[str, object], tmp_path: Path
) -> None:
    """ "Resolver, index, manifest, hook-adapter, and accepted-generation versions
    are pinned per run" -- each read off an artifact the run produced."""

    manifest = arm_run["manifest"]
    assert isinstance(manifest, dict)

    assert manifest["tag"] == "playbill-taubench-run-manifest-v1"
    adapter = manifest["hook_adapter"]
    assert adapter["adapter"] == "playbill-coverage-middleware-v1"
    # Recorded as absent rather than omitted: the owned-harness middleware has
    # no vendor hook envelope to version.
    assert "envelope_version" in adapter and adapter["envelope_version"] is None
    assert adapter["rule_set_digest"].startswith("sha256:")
    assert adapter["rule_set"][0]["identity_prefix"] == "corpus."

    coverage = manifest["coverage"]
    for field in ("index_digest", "overlay_digest", "manifest_digest"):
        assert str(coverage[field]).startswith("sha256:"), field
    assert coverage["epoch"] is not None
    assert coverage["boundary_format"] == "playbill-coverage-manifest-v2"

    for field in ("generation_root", "semantic_root", "compiler_digest", "floor_digest"):
        assert str(manifest["accepted"][field]).startswith("sha256:"), field
    assert manifest["accepted"]["format"] == "playbill-floor-export-v2"
    assert "native_render" not in manifest
    assert manifest["seed"]["plan_digest"].startswith("sha256:")
    assert manifest["arms"] == {
        "1": "files",
        "2": "files+scratchpad",
        "3": "playbill-surface",
        "4": "playbill-surface+coverage-delivery",
    }

    # It is written where the recipe says it writes it, and it is JSON.
    written = json.loads((tmp_path / "run/run-manifest.json").read_text(encoding="utf-8"))
    assert written == manifest
