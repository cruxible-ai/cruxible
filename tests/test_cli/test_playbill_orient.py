"""`cruxible orient` against a served daemon: the map, one kind, a refusal."""

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

    mapped = cruxible.json(
        "orient",
    )

    assert mapped["tag"] == "playbill-orient-v1"
    (kind,) = mapped["kinds"]
    assert (kind["kind"], kind["subjects"]) == (KIND, 1)
    assert [item["name"] for item in kind["predicates"]] == ["status"]
    assert mapped["artifacts"]["claim_types"] == 1
    assert mapped["you"]["actor"] is not None
    # Rendered for the CLI: commands, not tool calls.
    assert mapped["next"][0] == f"cruxible orient --kind {KIND}"
    assert all(line.startswith("cruxible ") for line in mapped["next"])

    text = cruxible.run(
        "orient",
    ).stdout
    assert f"  {KIND}  subjects=1" in text
    assert "    status  one  enum[blocked|done|ready]" in text
    assert "Next:\n  cruxible orient --kind " + KIND in text

    detail = cruxible.json("orient", "--kind", KIND)
    assert detail["kind_detail"]["sample_subject_ids"] == ["wi-42"]
    assert detail["kind_detail"]["predicates"][0]["live_claims"] == 1

    page = cruxible.json("orient", "--section", "claim_types", "--limit", "1")
    assert [row["predicate"] for row in page["claim_types"]] == [f"{KIND}.status"]
    assert "(no procedures)" in cruxible.run("orient", "--section", "procedures").stdout
    assert mapped["artifacts"]["interfaces"] == 0
    assert cruxible.json("orient", "--section", "interfaces")["interfaces"] == []
    assert "(no interfaces)" in cruxible.run("orient", "--section", "interfaces").stdout
    assert "floor" not in mapped

    # With a floor exported into this workspace, orient says where it is.
    cruxible.json("floor", "export")
    floored = cruxible.json(
        "orient",
    )
    assert floored["floor"] == {"at": mapped["coordinate"]["git_oid"], "generations_behind": 0}
    text = cruxible.run(
        "orient",
    ).stdout
    assert f"Floor: .cruxible/floor at {mapped['coordinate']['git_oid'][:12]}, current" in text


def test_an_interfaces_page_prints_each_contract_and_who_implements_it() -> None:
    from cruxible_core.cli.commands.playbill import _render_orient

    text = _render_orient(
        {
            "instance": "inst",
            "generation": 3,
            "coordinate": {"git_oid": "a" * 64},
            "accepted_at": "2026-09-29T00:00:00Z",
            "section": "interfaces",
            "interfaces": [
                {
                    "name": "workspace.file",
                    "description": "Structure the bytes of one workspace file read.",
                    "input": ["bytes: string", "byte_length: integer"],
                    "output": ["content: object"],
                    "effect": "none",
                    "providers": ["cruxible-provider-workspace"],
                }
            ],
            "next": [],
        }
    )

    assert "workspace.file  effect=none  providers=cruxible-provider-workspace" in text
    assert "  Structure the bytes of one workspace file read." in text
    assert "  in:  bytes: string, byte_length: integer" in text
    assert "  out: content: object" in text


def test_orient_prints_the_ledger_mirror_a_reviewer_clones() -> None:
    from cruxible_core.cli.commands.playbill import _render_orient

    header = {
        "instance": "inst",
        "generation": 3,
        "coordinate": {"git_oid": "a" * 64},
        "accepted_at": "2026-09-29T00:00:00Z",
        "next": [],
    }
    bound = _render_orient({**header, "mirror_url": "https://forge.test/ledger.git"})
    assert "Ledger mirror: https://forge.test/ledger.git" in bound
    assert "Ledger mirror" not in _render_orient(header)


def test_orient_refuses_a_wrong_kind_with_the_nearest_names(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    cruxible = served_cli
    _bootstrap(cruxible, tmp_path)
    _govern_the_bytes(cruxible, tmp_path)

    refused = CliRunner().invoke(cli, ["orient", "--kind", "project.work_itm"])

    assert refused.exit_code != 0
    assert "cruxible.orient.kind_not_found" in refused.output
    assert KIND in refused.output


def test_both_kind_views_print_shared_evidence_once_and_empty_exceptions() -> None:
    from cruxible_core.cli.commands.playbill import _render_orient

    kind = {
        "kind": KIND,
        "subjects": 2,
        "evidence": ["feed-a", "feed-b"],
        "predicates": [
            {"name": "status", "type": "string", "cardinality": "one"},
            {"name": "owner", "type": "string", "cardinality": "one", "evidence": []},
            {"name": "note", "type": "string", "cardinality": "one", "evidence": ["other"]},
        ],
    }
    for view in ({"kinds": [kind]}, {"kind_detail": {**kind, "sample_subject_ids": []}}):
        text = _render_orient(
            {
                "instance": "inst",
                "generation": 3,
                "coordinate": {"git_oid": "a" * 64},
                "accepted_at": "2026-09-29T00:00:00Z",
                **view,
            }
        )
        assert text.count("evidence=feed-a,feed-b") == 1
        assert "owner  one  string  evidence=(none)" in text
        assert "note  one  string  evidence=other" in text


def test_a_stale_floor_prints_how_far_behind_it_is_and_how_to_refresh() -> None:
    from cruxible_core.cli.commands.playbill import _render_orient

    text = _render_orient(
        {
            "instance": "inst",
            "generation": 9,
            "coordinate": {"git_oid": "a" * 64},
            "accepted_at": "2026-09-30T00:00:00Z",
            "floor": {"at": "b" * 64, "generations_behind": 2},
            "next": [],
        }
    )

    assert (
        f"Floor: .cruxible/floor at {'b' * 12}, 2 generation(s) behind; "
        "refresh: cruxible floor export --force"
    ) in text
