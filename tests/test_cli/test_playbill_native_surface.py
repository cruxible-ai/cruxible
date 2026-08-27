"""The native knowledge surface, driven end to end through ``cruxible ...`` argv.

§11.9 makes the committed render of native knowledge the primary greppable
discovery surface for half of the TauBench corpus, so the thing that has to
work is the operator loop, not a library call: check the knowledge out, read it,
edit a field, see the edit reported, and find that a re-render refuses to eat it.

No served operation was added for any of this. The render is computed in the CLI
from reads that already exist, which is also what makes §11.9.5's explicit-sync
law structural rather than promised -- the daemon never produced the bytes, so
there is no path by which it could commit them into a repository.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from cruxible_core.cli.main import cli
from cruxible_core.playbill.native import NATIVE_RENDER_MANIFEST_PATH
from tests.test_cli.test_playbill_knowledge_loop_smoke import (  # noqa: F401
    _claim_authoring,
    _Cli,
    _proposal_id,
    _write,
    served_cli,
)
from tests.test_playbill._knowledge_loop_support import subject_shell, work_item_query
from tests.test_playbill.test_claims import _claim_type

READ_TIME = "2026-08-16T21:00:00+00:00"
WI_42 = "subjects/project.work_item/wi-42.md"
WI_43 = "subjects/project.work_item/wi-43.md"


def _seed(cruxible: _Cli, tmp_path: Path) -> None:
    """Bootstrap a served instance and accept the knowledge the render shows."""

    cruxible.json("--server-url", "http://cruxible", "playbill", "host", "create")
    cruxible.bootstrap(tmp_path, with_reviewer=True)
    proposed = cruxible.json(
        "playbill",
        "claim-type",
        "propose",
        "--envelope",
        _write(tmp_path / "claim-type.json", _claim_type().model_dump(mode="json")),
        "--name",
        "seed-claim-type",
    )
    cruxible.accept(_proposal_id(proposed))
    proposed = cruxible.json(
        "playbill",
        "subject",
        "propose",
        "--envelope",
        _write(tmp_path / "subject.json", subject_shell("wi-42").model_dump(mode="json")),
        "--name",
        "seed-subject",
    )
    cruxible.accept(_proposal_id(proposed))
    for subject_id, value, seed_subject in (("wi-42", "ready", False), ("wi-43", "blocked", True)):
        authoring = _claim_authoring(subject_id, value, seed_subject=seed_subject)
        proposal = cruxible.json(
            "playbill",
            "claim",
            "propose",
            "--authoring",
            _write(tmp_path / f"claim-{subject_id}.json", authoring.model_dump(mode="json")),
            "--name",
            f"seed-claim-{subject_id}",
        )
        cruxible.accept(_proposal_id(proposal["proposal"]))
    proposed = cruxible.json(
        "playbill",
        "query",
        "propose",
        "--envelope",
        _write(tmp_path / "query.json", work_item_query().model_dump(mode="json")),
        "--name",
        "seed-query",
    )
    cruxible.accept(_proposal_id(proposed))


def _render(cruxible: _Cli, output: Path, *extra: str) -> dict[str, Any]:
    result = cruxible.json(
        "playbill",
        "native",
        "render",
        "--output",
        str(output),
        "--evaluation-time",
        READ_TIME,
        *extra,
    )
    return dict(result)


def test_native_render_checks_knowledge_out_as_a_browsable_editable_tree(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    cruxible = served_cli
    _seed(cruxible, tmp_path)
    output = tmp_path / "knowledge"

    rendered = _render(cruxible, output)

    manifest = rendered["manifest"]
    assert (output / NATIVE_RENDER_MANIFEST_PATH).is_file()
    assert (output / "README.md").is_file()
    assert (output / WI_42).is_file()
    assert (output / WI_43).is_file()
    assert manifest["format"] == "playbill-coverage-manifest-v1"
    assert datetime.fromisoformat(manifest["evaluation_time"]) == datetime.fromisoformat(READ_TIME)
    assert manifest["lens"]["grammar_class"] == "experimental"

    # The page is a genuine document about the Subject, with its Claim on it.
    body = (output / WI_42).read_text(encoding="utf-8")
    assert "project.work_item/wi-42" in body
    assert "project.work_item.status" in body
    assert '"ready"' in body
    assert "verdict at render: supported" in body
    assert manifest["coordinate"]["generation_root"] in body
    # The orientation floor names the roots and the entrypoints.
    readme = (output / "README.md").read_text(encoding="utf-8")
    assert "subjects/" in readme
    assert "query-definitions/project.work_items.md" in readme


def test_native_render_is_byte_stable_and_a_clean_rerender_writes_nothing(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    cruxible = served_cli
    _seed(cruxible, tmp_path)
    output = tmp_path / "knowledge"

    first = _render(cruxible, output)
    before = (output / WI_42).read_bytes()
    second = _render(cruxible, output)

    assert second["manifest"]["render_digest"] == first["manifest"]["render_digest"]
    assert (output / WI_42).read_bytes() == before
    assert second["plan"]["write_paths"] == []
    assert second["plan"]["stash_required"] is False


def test_status_reports_a_local_edit_and_a_rerender_refuses_to_eat_it(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    cruxible = served_cli
    _seed(cruxible, tmp_path)
    output = tmp_path / "knowledge"
    _render(cruxible, output)

    page = output / WI_42
    page.write_bytes(page.read_bytes().replace(b'\n"ready"\n', b'\n"shipped"\n'))

    reported = cruxible.json("playbill", "native", "status", str(output))
    rows = {item["path"]: item for item in reported["status"]["files"]}
    assert rows[WI_42]["state"] == "dirty"
    assert rows[WI_42]["dirty_regions"] == 1
    assert rows[WI_43]["state"] == "clean"
    # The derived display beside the edit is invalidated through the resolver,
    # and the drift card grants the edited material nothing.
    invalidation = reported["invalidation"]
    assert invalidation["coverage"]["summary"]["drifted"] >= 1
    assert invalidation["invalidated_region_ids"]
    assert all(
        card["grants_mutation_authority"] is False
        for span in invalidation["coverage"]["spans"]
        for card in span["cards"]
    )

    refused = CliRunner().invoke(
        cli,
        [
            "playbill",
            "native",
            "render",
            "--output",
            str(output),
            "--evaluation-time",
            READ_TIME,
        ],
    )
    assert refused.exit_code != 0
    assert "dirty region" in refused.output
    assert b'"shipped"' in page.read_bytes()

    discarded = _render(cruxible, output, "--discard")
    assert discarded["plan"]["write_paths"] == [WI_42]
    assert b'"ready"' in page.read_bytes()

    after = cruxible.json("playbill", "native", "status", str(output))
    assert all(item["state"] == "clean" for item in after["status"]["files"])
    assert after["invalidation"]["invalidated_region_ids"] == []


def test_stashing_keeps_the_edit_a_rerender_would_have_eaten(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The third answer to the dirty-re-render refusal, end to end.

    `--discard` was the only way past the refusal that a person could actually
    type, which made the default outcome "lose the edit". Stashing keeps the
    bytes beside the render, re-renders over them, and hands back an identifier
    that puts them back -- and none of it goes anywhere near accepted state.
    """

    cruxible = served_cli
    _seed(cruxible, tmp_path)
    output = tmp_path / "knowledge"
    _render(cruxible, output)
    page = output / WI_42
    page.write_bytes(page.read_bytes().replace(b'\n"ready"\n', b'\n"shipped"\n'))
    edited = page.read_bytes()

    stashed = _render(cruxible, output, "--stash")

    assert stashed["plan"]["stashed_region_ids"]
    assert stashed["plan"]["discarded_region_ids"] == []
    assert stashed["stash_id"] is not None
    assert page.read_bytes() != edited
    assert b'"ready"' in page.read_bytes()

    listed = cruxible.json("playbill", "native", "stash", "list", str(output))["stashes"]
    assert [item["stash_digest"] for item in listed] == [stashed["stash_id"]]

    shown = cruxible.json("playbill", "native", "stash", "show", str(output), stashed["stash_id"])
    assert shown["stash_digest"] == stashed["stash_id"]
    assert [item["region_kind"] for item in shown["body"]["regions"]] == ["statement_value"]

    restored = cruxible.json(
        "playbill", "native", "stash", "restore", str(output), stashed["stash_id"], "--drop"
    )
    assert restored["restore"]["write_paths"] == [WI_42]
    assert restored["restore"]["unresolved_region_ids"] == []
    assert restored["dropped"] is True
    assert page.read_bytes() == edited

    after = cruxible.json("playbill", "native", "status", str(output))
    rows = {item["path"]: item for item in after["status"]["files"]}
    assert rows[WI_42]["dirty_regions"] == 1
    # Nothing accepted moved through any of it.
    assert cruxible.json("playbill", "claim", "list")["claims"]


def test_status_refuses_a_tampered_derived_field_and_never_interprets_it(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    cruxible = served_cli
    _seed(cruxible, tmp_path)
    output = tmp_path / "knowledge"
    _render(cruxible, output)

    page = output / WI_42
    page.write_bytes(page.read_bytes().replace(b"- role: observation", b"- role: normative"))

    status = cruxible.json("playbill", "native", "status", str(output))["status"]

    rows = {item["path"]: item for item in status["files"]}
    assert rows[WI_42]["state"] == "tampered"
    assert rows[WI_42]["tampered_regions"] == 1
    assert rows[WI_42]["dirty_regions"] == 0
    refusals = [item for item in status["diagnostics"] if item["severity"] == "refusal"]
    assert [item["code"] for item in refusals] == ["derived_region_tampered"]
    assert "Re-render" in refusals[0]["instruction"]


def test_deleting_the_rendered_directory_loses_nothing_accepted(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    """§11.9: nothing in the ledger references render output."""

    cruxible = served_cli
    _seed(cruxible, tmp_path)
    output = tmp_path / "knowledge"
    first = _render(cruxible, output)
    digests = json.loads((output / NATIVE_RENDER_MANIFEST_PATH).read_bytes())

    for path in sorted(output.rglob("*"), reverse=True):
        path.unlink() if path.is_file() else path.rmdir()
    output.rmdir()

    again = _render(cruxible, output)

    assert again["manifest"]["render_digest"] == first["manifest"]["render_digest"]
    assert json.loads((output / NATIVE_RENDER_MANIFEST_PATH).read_bytes()) == digests
    assert cruxible.json("playbill", "claim", "list")["claims"]
