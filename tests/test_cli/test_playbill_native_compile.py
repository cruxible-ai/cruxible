"""The compile contract driven end to end through ``cruxible ...`` argv.

§11.9.6's load-bearing law is a whole operator loop, not a library call:

    edit editable field -> compile -> accept -> render  preserves semantic payload

So the flagship test here does exactly that, on a served instance, through the
CLI: check knowledge out, change a rendered value, compile it into a governed
proposal, approve and activate that proposal with a client-held key, re-render,
and find the new value in the file with its governance facts requalified at the
generation that accepted it.

The other tests are the separations that make that loop mean anything. Editing
proposes nothing. Compiling accepts nothing. A preview submits nothing. A draft
with no disposition refuses and says whose name it collided with. Removal is
never a retirement.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from cruxible_core.cli.main import cli
from cruxible_core.playbill.native import NATIVE_RENDER_MANIFEST_PATH
from cruxible_core.playbill.native.grammar import NativeDraftMarkerV1, render_draft_marker
from tests.test_cli.test_playbill_knowledge_loop_smoke import (  # noqa: F401
    SIGNER_ID,
    _Cli,
    _proposal_id,
    _write,
    served_cli,
)
from tests.test_cli.test_playbill_native_surface import WI_42, WI_43, _render, _seed

PREDICATE = "project.work_item.status"


def _raw(*args: str) -> Any:
    """Invoke the CLI without asserting success: refusals exit non-zero by design."""

    return CliRunner().invoke(cli, list(args))


def _compile(cruxible: _Cli, output: Path, *extra: str) -> dict[str, Any]:
    return dict(cruxible.json("playbill", "native", "compile", str(output), *extra))


def _edit(page: Path, old: bytes, new: bytes) -> None:
    content = page.read_bytes()
    assert old in content, f"{old!r} is not in {page}"
    page.write_bytes(content.replace(old, new, 1))


def _append(page: Path, text: str) -> None:
    page.write_bytes(page.read_bytes() + text.encode("utf-8"))


def test_edit_compile_accept_render_preserves_the_semantic_payload(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The §11.9.6 flagship law, over a served instance, end to end.

    Every step is a separate act by a separate command, and the value only
    becomes accepted at the last one.
    """

    cruxible = served_cli
    _seed(cruxible, tmp_path)
    output = tmp_path / "knowledge"
    first = _render(cruxible, output)
    baseline_generation = first["manifest"]["coordinate"]["generation_root"]

    # 1. Edit one editable field. Nothing accepted has changed.
    page = output / WI_42
    _edit(page, b'\n"ready"\n', b'\n"done"\n')
    assert cruxible.json("playbill", "native", "status", str(output))["status"]["files"]

    # 2. Compile. This produces a candidate, and only a candidate.
    compiled = _compile(cruxible, output, "--submit", "--name", "native-edit")
    result = compiled["compile"]
    assert [item["kind"] for item in result["members"]] == ["locator_successor"]
    assert result["three_way"][0]["outcome"] == "unchanged_at_head"
    assert result["refusals"] == []
    proposal = compiled["proposal"]
    assert proposal["evaluation"]["verdict"] == "candidate"
    assert proposal["evaluation"]["rebased"] is False
    # Compiling accepted nothing: the ledger still answers "ready".
    accepted = cruxible.json("playbill", "query", "run", "project.work_items")
    projected = {
        next(field["value"] for field in row["fields"] if field["name"] == "item_id"): next(
            field["value"] for field in row["fields"] if field["name"] == "status"
        )
        for row in accepted["result"]["rows"]
    }
    assert projected["wi-42"] == "ready"

    # 3. Accept: approve with the client-held key, then activate. Two acts.
    activated = cruxible.accept(proposal["admission"]["proposal_id"])
    assert activated["status"] == "accepted"
    accepted_generation = activated["accepted_coordinate"]["generation_root"]
    assert accepted_generation != baseline_generation

    # 4. Re-render. The payload survived, and the governance beside it is
    #    requalified at the generation that accepted it rather than reused.
    again = _render(cruxible, output, "--discard")
    body = (output / WI_42).read_text(encoding="utf-8")
    assert '"done"' in body
    assert '"ready"' not in body
    assert accepted_generation in body
    assert baseline_generation not in body
    assert "verdict at render: supported" in body
    assert again["manifest"]["coordinate"]["generation_root"] == accepted_generation

    # And the accepted read agrees: this was a semantic change, not a text one.
    rerun = cruxible.json("playbill", "query", "run", "project.work_items")
    reprojected = {
        next(field["value"] for field in row["fields"] if field["name"] == "item_id"): next(
            field["value"] for field in row["fields"] if field["name"] == "status"
        )
        for row in rerun["result"]["rows"]
    }
    assert reprojected["wi-42"] == "done"


def test_a_preview_submits_nothing_and_matches_what_a_submit_would_send(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    """Preview and submit are two renderings of one compile result."""

    cruxible = served_cli
    _seed(cruxible, tmp_path)
    output = tmp_path / "knowledge"
    _render(cruxible, output)
    _edit(output / WI_42, b'\n"ready"\n', b'\n"done"\n')

    previewed = _compile(cruxible, output, "--preview")

    assert previewed["proposal"] is None
    submitted = _compile(cruxible, output, "--submit", "--name", "native-edit")
    assert submitted["compile"]["members"] == previewed["compile"]["members"]
    assert submitted["compile"]["three_way"] == previewed["compile"]["three_way"]
    assert submitted["proposal"] is not None


def test_compiling_an_unedited_checkout_proposes_nothing(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    cruxible = served_cli
    _seed(cruxible, tmp_path)
    output = tmp_path / "knowledge"
    _render(cruxible, output)

    compiled = _compile(cruxible, output, "--submit", "--name", "no-op")

    assert compiled["compile"]["members"] == []
    assert compiled["compile"]["refusals"] == []
    assert compiled["proposal"] is None
    human = cruxible.run("playbill", "native", "compile", str(output)).stdout
    assert "Nothing to compile" in human


def test_deleting_rendered_material_proposes_no_retirement(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    cruxible = served_cli
    _seed(cruxible, tmp_path)
    output = tmp_path / "knowledge"
    _render(cruxible, output)
    (output / WI_43).unlink()

    compiled = _compile(cruxible, output, "--preview")

    assert compiled["compile"]["members"] == []
    assert compiled["compile"]["refusals"] == []
    assert any(item["code"] == "rendered_file_absent" for item in compiled["compile"]["notices"])
    # The Claim the deleted page rendered is untouched and still accepted.
    assert cruxible.json("playbill", "claim", "list")["claims"]


def test_a_draft_without_a_disposition_refuses_and_names_the_candidate(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    cruxible = served_cli
    _seed(cruxible, tmp_path)
    output = tmp_path / "knowledge"
    _render(cruxible, output)
    _append(output / WI_42, "\n## Draft\n\nThe wi-42 rollout needs its own tracked item.\n")

    refused = _raw("playbill", "native", "compile", str(output))

    assert refused.exit_code != 0
    assert "draft_disposition_required" in refused.output
    assert "Subject:project.work_item/wi-42" in refused.output
    assert "new_distinct" in refused.output


def test_new_distinct_exposes_its_generated_claims_before_submission(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    """§11.9.3: the lowering is visible in preview, before anything is proposed."""

    cruxible = served_cli
    _seed(cruxible, tmp_path)
    output = tmp_path / "knowledge"
    _render(cruxible, output)
    marker = render_draft_marker(
        NativeDraftMarkerV1(
            disposition="new_distinct",
            subject_kind="project.epic",
            subject_id="wi-42",
            predicate=PREDICATE,
            value="ready",
        )
    )
    _append(output / WI_42, f"\n## Draft\n\n{marker}\nThe wi-42 epic is not the work item.\n")

    human = cruxible.run("playbill", "native", "compile", str(output)).stdout
    previewed = _compile(cruxible, output, "--preview")

    assert "lowers to semantic.distinct_from subjects/project.work_item/wi-42.yaml" in human
    assert "generated_distinct_from" in human
    draft = previewed["compile"]["drafts"][0]
    assert draft["disposition"]["kind"] == "new_distinct"
    assert [item["artifact_path"] for item in draft["generated_distinct_from"]] == [
        "subjects/project.work_item/wi-42.yaml"
    ]
    assert [item["kind"] for item in previewed["compile"]["members"]] == [
        "generated_distinct_from",
        "unbound_native_draft",
    ]
    assert previewed["proposal"] is None


def test_review_current_reports_superseded_by_rebase_after_the_head_moves(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    """Headless review currency: no forge is involved in any part of this."""

    cruxible = served_cli
    _seed(cruxible, tmp_path)
    output = tmp_path / "knowledge"
    _render(cruxible, output)
    _edit(output / WI_42, b'\n"ready"\n', b'\n"done"\n')

    first = _compile(cruxible, output, "--submit", "--name", "native-first")
    first_id = first["proposal"]["admission"]["proposal_id"]
    first_digest = first["proposal"]["evaluation"]["candidate_digest"]
    cruxible.run(
        "playbill",
        "proposal",
        "approve",
        first_id,
        "--signer-id",
        SIGNER_ID,
        "--key",
        str(cruxible.private_key),
        "--yes",
        "--json",
    )

    current = cruxible.json("playbill", "native", "review-current", first_id)
    assert current["status"] == "current"
    assert current["binding_signer_ids"] == [SIGNER_ID]

    # An unrelated accepted change moves the head under the render baseline.
    from tests.test_playbill._knowledge_loop_support import subject_shell

    proposed = cruxible.json(
        "playbill",
        "subject",
        "propose",
        "--envelope",
        _write(tmp_path / "wi-77.json", subject_shell("wi-77").model_dump(mode="json")),
        "--name",
        "unrelated-subject",
    )
    cruxible.accept(_proposal_id(proposed))

    second = _compile(cruxible, output, "--submit", "--name", "native-second")
    assert second["proposal"]["evaluation"]["rebased"] is True
    second_id = second["proposal"]["admission"]["proposal_id"]
    second_digest = second["proposal"]["evaluation"]["candidate_digest"]
    assert second_digest != first_digest
    assert (
        "head moved under this baseline"
        in cruxible.run("playbill", "native", "compile", str(output)).stdout
    )

    stale = _raw("playbill", "native", "review-current", second_id, "--bound", first_digest)
    assert stale.exit_code != 0
    assert "superseded_by_rebase" in stale.output
    assert second_digest in stale.output

    unreviewed = _raw("playbill", "native", "review-current", second_id, "--json")
    assert unreviewed.exit_code != 0
    assert json.loads(unreviewed.stdout)["status"] == "not_reviewed"


def test_compile_refuses_a_tampered_derived_field_without_interpreting_it(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    cruxible = served_cli
    _seed(cruxible, tmp_path)
    output = tmp_path / "knowledge"
    _render(cruxible, output)
    _edit(output / WI_42, b"- role: observation", b"- role: normative")

    refused = _raw("playbill", "native", "compile", str(output), "--submit", "--name", "tampered")

    assert refused.exit_code != 0
    assert "derived_region_tampered" in refused.output
    assert "Refused: nothing was submitted." in refused.output
    assert (output / NATIVE_RENDER_MANIFEST_PATH).is_file()


def test_one_compile_spans_two_files_as_a_single_generation(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    """§11.9.4 atomicity, end to end: one compile, one proposal, one generation.

    Two Claims on two pages, plus a second edited field on one of them, settle
    together or not at all. That is the property the multi-Claim propose surface
    exists to give this loop, and the only way to see it is to count generations.
    """

    cruxible = served_cli
    _seed(cruxible, tmp_path)
    output = tmp_path / "knowledge"
    _render(cruxible, output)
    _edit(output / WI_42, b'\n"ready"\n', b'\n"done"\n')
    _edit(output / WI_42, b"\n(none)\n", b"\nreviewed at the release gate\n")
    _edit(output / WI_43, b'\n"blocked"\n', b'\n"ready"\n')

    compiled = _compile(cruxible, output, "--submit", "--name", "native-multi")

    result = compiled["compile"]
    assert len(result["members"]) == 2
    assert {item["outcome"] for item in result["three_way"]} == {"unchanged_at_head"}
    assert sorted(len(item["region_ids"]) for item in result["three_way"]) == [1, 2]

    activated = cruxible.accept(compiled["proposal"]["admission"]["proposal_id"])
    generation = activated["accepted_coordinate"]["generation_root"]

    _render(cruxible, output, "--discard")
    wi_42 = (output / WI_42).read_text(encoding="utf-8")
    wi_43 = (output / WI_43).read_text(encoding="utf-8")
    assert '"done"' in wi_42
    assert "reviewed at the release gate" in wi_42
    assert '"ready"' in wi_43
    # One generation carries all of it: neither page landed without the other.
    assert generation in wi_42
    assert generation in wi_43


def test_a_disposition_supplied_on_the_command_line_answers_the_refusal(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    """In-file and on-the-command-line dispositions are two spellings of one act."""

    cruxible = served_cli
    _seed(cruxible, tmp_path)
    output = tmp_path / "knowledge"
    _render(cruxible, output)
    _append(output / WI_42, "\n## Draft\n\nThe wi-42 rollout is done as of today.\n")

    refused = _raw("playbill", "native", "compile", str(output), "--json")
    assert refused.exit_code != 0
    draft_id = json.loads(refused.stdout)["compile"]["drafts"][0]["draft_id"]

    dispositions = tmp_path / "dispositions.json"
    dispositions.write_text(
        json.dumps(
            [
                {
                    "draft_id": draft_id,
                    "kind": "reuse",
                    "target_path": "subjects/project.work_item/wi-42.yaml",
                    "predicate": PREDICATE,
                    "value": "done",
                }
            ]
        ),
        encoding="utf-8",
    )

    compiled = _compile(cruxible, output, "--dispositions", str(dispositions), "--preview")

    assert compiled["compile"]["refusals"] == []
    assert [item["kind"] for item in compiled["compile"]["members"]] == ["unbound_native_draft"]
    assert compiled["compile"]["drafts"][0]["draft_id"] == draft_id


def test_submitting_into_a_conflicting_head_refuses_and_exits_non_zero(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    """A refusal from the receive path is a failed compile, not a quiet success.

    Compile deliberately does not predict the rebase outcome, so the only place
    that outcome can be read is the evaluation it gets back. An operator working
    from a stale checkout has to be able to see that nothing is pending approval
    without parsing prose, which means the exit code has to say so.
    """

    cruxible = served_cli
    _seed(cruxible, tmp_path)
    output = tmp_path / "knowledge"
    _render(cruxible, output)

    # Land one edit, so the accepted head moves for this exact Claim.
    _edit(output / WI_42, b'\n"ready"\n', b'\n"done"\n')
    landed = _compile(cruxible, output, "--submit", "--name", "native-landed")
    cruxible.accept(landed["proposal"]["admission"]["proposal_id"])

    # The checkout is now stale, and the next edit conflicts at the same member.
    _edit(output / WI_42, b'\n"done"\n', b'\n"blocked"\n')
    stale = _raw("playbill", "native", "compile", str(output), "--submit", "--name", "native-stale")

    assert stale.exit_code != 0
    assert "playbill.rebase.member_conflict" in stale.output
    assert "Verdict: refused" in stale.output
    assert "Nothing is pending approval." in stale.output

    payload = _raw(
        "playbill",
        "native",
        "compile",
        str(output),
        "--submit",
        "--name",
        "native-stale-json",
        "--json",
    )
    assert payload.exit_code != 0
    compiled = json.loads(payload.stdout)
    assert compiled["compile"]["three_way"][0]["outcome"] == "changed_at_head"
    assert compiled["compile"]["refusals"] == []
    assert compiled["proposal"]["evaluation"]["verdict"] == "refused"
    assert compiled["proposal"]["evaluation"]["candidate_digest"] is None
