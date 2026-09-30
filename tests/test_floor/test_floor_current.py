"""The values-first current/ layer: one readable, greppable file per Subject."""

from __future__ import annotations

import json
from typing import Any

import pytest
import yaml

from cruxible_client.contracts.write import PlaybillWriteRequestV1, WriteOutcome
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from cruxible_core.service.floor.floor import service_export_playbill_floor
from cruxible_core.service.floor.floor_current import literal_scalar, yaml_scalar
from tests.core_support._write_support import KIND, caller, seed_write_surface

WI1 = f"{KIND}/wi-1"
WI2 = f"{KIND}/wi-2"
WI3 = f"{KIND}/wi-3"
RULING = "Rulings are text.\nEvery line of this one greps on its own.\n"


def _write(instance: PlaybillInstance, *changes: dict[str, Any], **options: Any) -> WriteOutcome:
    request = PlaybillWriteRequestV1.model_validate(
        {"because": "The writer checked it.", "changes": list(changes), **options}
    )
    outcome = service_playbill_write(instance, request=request, caller=caller())
    assert outcome.status == "accepted", outcome
    return outcome


def _set(subject: str, field: str, value: object, **extra: object) -> dict[str, Any]:
    return {"op": "set", "subject": subject, "field": field, "value": value, **extra}


def _add(subject: str, field: str, value: object) -> dict[str, Any]:
    return {"op": "add", "subject": subject, "field": field, "value": value}


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    instance, _owner = seed_write_surface(tmp_path_factory.mktemp("floor-current"))
    written = _write(
        instance,
        _set(WI1, "title", "Tidy the CLI: part #1"),
        _set(WI1, "status", "ready"),
        _add(WI1, "governs", WI2),
        _add(WI1, "governs", WI3),
        _set(WI1, "ruling", RULING),
        _set(WI1, "measured", 3),
    )
    first = _write(instance, _set(WI2, "status", "ready"))
    _write(instance, _set(WI2, "status", "blocked"))
    _write(instance, _set(WI2, "status", "done", contend=True), at=first.coordinate.git_oid)
    claims = {change.field: change.claim for change in written.changes}
    return {
        "instance": instance,
        "claims": claims,
        "governs": sorted(change.claim for change in written.changes if change.field == "governs"),
        "files": service_export_playbill_floor(instance),
    }


def _current(world: dict[str, Any], subject_id: str) -> str:
    return world["files"][f"current/{KIND}/{subject_id}.yaml"].decode()


def test_the_header_is_one_line_naming_ref_kind_and_coordinate(world: dict[str, Any]) -> None:
    instance: PlaybillInstance = world["instance"]
    head = instance.accepted_coordinate().git_oid
    generation = len(instance.accepted_history()) - 1
    first_line = _current(world, "wi-1").splitlines()[0]
    assert first_line == f"# {WI1}  kind={KIND}  at {head} gen {generation}"
    manifest = json.loads(world["files"]["manifest.json"])
    assert manifest["coordinate"]["git_oid"] == head


def test_values_come_first_under_short_names_with_their_claim(world: dict[str, Any]) -> None:
    text = _current(world, "wi-1")
    claims = world["claims"]
    assert f'title: "Tidy the CLI: part #1"  # {claims["title"]} CAP-' in text
    assert f"status: ready  # {claims['status']} CAP-" in text
    assert "measured: 3  # " in text
    governs = text[text.index("governs:\n") :].splitlines()[1:3]
    assert [line.split("  # ")[0] for line in governs] == [f"  - {WI2}", f"  - {WI3}"]
    assert sorted(line.split("  # ")[1].split()[0] for line in governs) == world["governs"]


def test_current_files_hold_no_digests_or_addresses(world: dict[str, Any]) -> None:
    for path, content in world["files"].items():
        if not path.startswith("current/"):
            continue
        text = content.decode()
        assert "sha256:" not in text, path
        assert "subjects/" not in text, path
        assert text.count(" at ") == 1, path
        assert len(content) < 2048, path


def test_every_current_file_parses_as_yaml_with_its_values(world: dict[str, Any]) -> None:
    parsed = yaml.safe_load(_current(world, "wi-1"))
    assert parsed["title"] == "Tidy the CLI: part #1"
    assert parsed["status"] == "ready"
    assert parsed["governs"] == [WI2, WI3]
    assert parsed["measured"] == 3
    assert parsed["flags"] == {"measured": ["uncovered"]}
    for path, content in world["files"].items():
        if path.startswith("current/") and path.endswith(".yaml"):
            assert isinstance(yaml.safe_load(content), dict | None), path


def test_a_contested_slot_lists_every_live_value_and_is_flagged(world: dict[str, Any]) -> None:
    parsed = yaml.safe_load(_current(world, "wi-2"))
    assert sorted(parsed["status"]) == ["blocked", "done"]
    assert "contested" in parsed["flags"]["status"]


def test_the_digests_move_to_provenance_subjects(world: dict[str, Any]) -> None:
    provenance = json.loads(world["files"][f"provenance/subjects/{KIND}/wi-1.json"])
    assert provenance["current"] == f"current/{KIND}/wi-1.yaml"
    rows = {row["claim"].removeprefix("Claim:"): row for row in provenance["claims"]}
    assert rows[world["claims"]["status"]]["artifact_digest"].startswith("sha256:")
    assert rows[world["claims"]["status"]]["statement"]["object"]["value"] == "ready"


def test_identical_accepted_state_gives_identical_bytes(world: dict[str, Any]) -> None:
    instance: PlaybillInstance = world["instance"]
    instance.floor_export_memo.clear()
    instance.floor_structure_memo.clear()
    assert service_export_playbill_floor(instance) == world["files"]


@pytest.mark.parametrize(
    "value",
    [
        "ready",
        "Tidy the CLI",
        "yes",
        "No",
        "null",
        "2026-09-30",
        "12",
        "- dash",
        "a: b",
        "trailing ",
        "hash #tag",
        'quote "inside"',
        "tab\there",
        "line\u2028separator",
        "del\x7fchar",
        "",
        "ünïcode välue",
        "[flow]",
    ],
)
def test_scalars_round_trip_through_yaml(value: str) -> None:
    assert yaml.safe_load(f"k: {yaml_scalar(value)}\n") == {"k": value}


@pytest.mark.parametrize("value", [None, True, False, 0, -3, {"b": [1, "x"], "a": None}, ["z"]])
def test_literal_values_round_trip_through_yaml(value: object) -> None:
    assert yaml.safe_load(f"k: {literal_scalar(value)}\n") == {"k": value}
