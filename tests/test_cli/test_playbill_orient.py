"""`cruxible playbill orient` against a served daemon: the map, one kind, a refusal."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from cruxible_core.cli.main import cli
from tests.test_cli.test_playbill_coverage_surface import _bootstrap, _govern_the_bytes
from tests.test_cli.test_playbill_knowledge_loop_smoke import (  # noqa: F401
    _Cli,
    served_cli,
)

KIND = "project.work_item"


def test_orient_maps_kinds_and_suggests_cli_commands(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    cruxible = served_cli
    _bootstrap(cruxible, tmp_path)
    _govern_the_bytes(cruxible, tmp_path)

    mapped = cruxible.json("playbill", "orient")

    assert mapped["tag"] == "playbill-orient-v1"
    (kind,) = mapped["kinds"]
    assert (kind["kind"], kind["subjects"]) == (KIND, 1)
    assert [item["name"] for item in kind["predicates"]] == ["status"]
    assert mapped["artifacts"]["claim_types"] == 1
    assert mapped["you"]["actor"] is not None
    # Rendered for the CLI: commands, not tool calls.
    assert mapped["next"][0] == f"cruxible playbill orient --kind {KIND}"
    assert all(line.startswith("cruxible playbill ") for line in mapped["next"])

    text = cruxible.run("playbill", "orient").stdout
    assert f"  {KIND}  subjects=1" in text
    assert "    status  one  enum[blocked|done|ready]" in text
    assert "Next:\n  cruxible playbill orient --kind " + KIND in text

    detail = cruxible.json("playbill", "orient", "--kind", KIND)
    assert detail["kind_detail"]["sample_subject_ids"] == ["wi-42"]
    assert detail["kind_detail"]["predicates"][0]["live_claims"] == 1

    page = cruxible.json("playbill", "orient", "--section", "claim_types", "--limit", "1")
    assert [row["predicate"] for row in page["claim_types"]] == [f"{KIND}.status"]
    assert "(no procedures)" in cruxible.run("playbill", "orient", "--section", "procedures").stdout


def test_orient_refuses_a_wrong_kind_with_the_nearest_names(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    cruxible = served_cli
    _bootstrap(cruxible, tmp_path)
    _govern_the_bytes(cruxible, tmp_path)

    refused = CliRunner().invoke(cli, ["playbill", "orient", "--kind", "project.work_itm"])

    assert refused.exit_code != 0
    assert "playbill.orient.kind_not_found" in refused.output
    assert KIND in refused.output
