"""The retained pure planner pins seed-bundle bytes without applying them.

The planner is pure -- bytes in, a plan out -- so everything here runs without a
daemon, a clock, or accepted state. That is the property the `--plan` flag and
the run manifest's `plan_digest` both rest on: two arms that print the same plan
digest are seeding the same world, and nothing about when or where they ran can
move it.

The state-changing seed adapter is retired. Current ClaimInput entries each map
to one AuthoringIntent; the plan remains a deterministic, offline description.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.authoring.seed import (
    SEED_GROUP_OPERATION_DIGEST_DOMAIN,
    SeedBundleError,
    plan_seed_bundle,
    read_seed_bundle,
    render_seed_plan,
    seed_group_operation_digest,
    seed_group_proposal_name,
    seed_plan_digest,
)
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from tests.test_playbill._claim_authoring_support import _status_authoring

EXAMPLE = Path(__file__).resolve().parents[2] / "benchmarks/playbill_taubench/seed-example"


def example_bundle() -> dict[str, bytes]:
    """The committed example bundle, read exactly as the CLI reads it."""

    return {
        path.relative_to(EXAMPLE).as_posix(): path.read_bytes()
        for path in sorted(EXAMPLE.rglob("*"))
        if path.is_file()
    }


def _write(files: dict[str, bytes], path: str, payload: Any) -> None:
    files[path] = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")


def _legacy_claim_payload() -> dict[str, Any]:
    return _status_authoring().model_dump(mode="json")


def test_the_example_bundle_plans_each_surviving_writer_in_dependency_order() -> None:

    plan = plan_seed_bundle(example_bundle(), proposal_name="taubench-example")

    assert plan.group_ids == (
        "claim_type:project.work_item.status",
        "subject:project.work_item/wi-101",
        "subject:project.work_item/wi-102",
        "subject:project.work_item/wi-103",
        "claim_input:project.work_item/wi-101#project.work_item.status",
        "claim_input:project.work_item/wi-102#project.work_item.status",
        "claim_input:project.work_item/wi-103#project.work_item.status",
        "query_definition:project.work_items",
        "procedure:project.work_item.digest",
    )
    assert plan.carried == ()
    assert plan.body_paths == (
        "bodies/corpus/handbook.md",
        "bodies/corpus/runbook.md",
        "bodies/notes/scratch.md",
    )
    assert plan.next_group_id("claim_type:project.work_item.status") == (
        "subject:project.work_item/wi-101"
    )
    assert plan.next_group_id("procedure:project.work_item.digest") is None
    procedure = plan.group("procedure:project.work_item.digest")
    assert procedure.operation == "playbill_authoring_submit"
    assert procedure.entry_paths == ("procedures/project.work_item.digest.json",)


def test_the_plan_is_a_pure_function_of_the_bundle_bytes() -> None:
    """Same bytes, same digest -- which is what a run manifest pins."""

    first = plan_seed_bundle(example_bundle(), proposal_name="taubench-example")
    second = plan_seed_bundle(example_bundle(), proposal_name="taubench-example")

    assert seed_plan_digest(first) == seed_plan_digest(second)
    assert render_seed_plan(first) == render_seed_plan(second)
    assert seed_plan_digest(first).tagged in render_seed_plan(first)[0]


def test_example_seed_entries_contain_only_ordinary_claims() -> None:
    entries = read_seed_bundle(example_bundle())

    claims = tuple(item for item in entries if item.kind == "claim")
    assert tuple(item.path for item in claims) == (
        "claims/wi-101-status.json",
        "claims/wi-102-status.json",
        "claims/wi-103-status.json",
    )


def test_a_seed_group_operation_has_the_frozen_content_addressed_identity() -> None:
    first = plan_seed_bundle(example_bundle(), proposal_name="human-run-a")
    relabeled = plan_seed_bundle(example_bundle(), proposal_name="human-run-b")
    group_id = "claim_input:project.work_item/wi-101#project.work_item.status"
    group = first.group(group_id)

    expected = typed_digest(
        Sha256Value,
        SEED_GROUP_OPERATION_DIGEST_DOMAIN,
        {
            "plan_digest": seed_plan_digest(first).tagged,
            "group_id": group.group_id,
        },
    )

    assert seed_group_operation_digest(first, group) == expected
    assert seed_group_operation_digest(relabeled, relabeled.group(group_id)) == expected
    assert seed_group_proposal_name(first, group) == f"seed-{expected.value}"
    assert (
        seed_group_operation_digest(
            first,
            first.group("query_definition:project.work_items"),
        )
        != expected
    )


def test_a_file_outside_the_bundle_directories_refuses_rather_than_being_skipped() -> None:
    """Silently ignoring part of a bundle makes "applied" untrue invisibly."""

    files = example_bundle()
    files["README.md"] = b"# not an authoring\n"

    with pytest.raises(SeedBundleError) as refusal:
        plan_seed_bundle(files, proposal_name="taubench-example")

    assert "is not in a seed bundle directory" in str(refusal.value)


def test_two_files_declaring_one_artifact_refuse() -> None:
    files = example_bundle()
    files["subjects/duplicate.json"] = files["subjects/wi-101.json"]

    with pytest.raises(SeedBundleError) as refusal:
        plan_seed_bundle(files, proposal_name="taubench-example")

    assert "a bundle declares each artifact once" in str(refusal.value)


def test_a_malformed_authoring_names_its_own_file() -> None:
    files = example_bundle()
    files["subjects/wi-101.json"] = b'{"subject_kind": "project.work_item"}\n'

    with pytest.raises(SeedBundleError) as refusal:
        plan_seed_bundle(files, proposal_name="taubench-example")

    assert "subjects/wi-101.json is not a well-formed subject authoring" in str(refusal.value)


def test_legacy_claim_authoring_refuses_before_a_retired_operation_can_be_planned() -> None:
    files = example_bundle()
    for path in tuple(files):
        if path.startswith("claims/"):
            del files[path]
    legacy = _legacy_claim_payload()
    legacy["claim_type_artifact"] = None
    legacy["subject_shell"] = None
    _write(files, "claims/legacy-status.json", legacy)

    with pytest.raises(SeedBundleError) as refusal:
        plan_seed_bundle(files, proposal_name="legacy")

    assert "legacy Claim authoring is retired" in str(refusal.value)
    assert "playbill_propose_claims" not in str(refusal.value)


def test_top_level_and_carried_claim_type_conflict_still_refuses() -> None:
    files = example_bundle()
    for path in tuple(files):
        if path.startswith("claims/"):
            del files[path]
    legacy = _legacy_claim_payload()
    top_level = json.loads(files["claim-types/project.work_item.status.json"])
    top_level["literal_schema"] = {"enum": ["done", "ready"], "type": "string"}
    _write(files, "claim-types/project.work_item.status.json", top_level)
    _write(files, "claims/legacy-status.json", legacy)

    with pytest.raises(SeedBundleError) as refusal:
        plan_seed_bundle(files, proposal_name="legacy")

    assert "one canonical path in one change set" in str(refusal.value)
    assert "project.work_item.status" in str(refusal.value)


def test_two_legacy_claims_carrying_different_claim_types_still_refuse() -> None:
    files = example_bundle()
    for path in tuple(files):
        if path.startswith("claims/"):
            del files[path]
    first = _legacy_claim_payload()
    second = _legacy_claim_payload()
    second["statement"]["subject"]["artifact_path"] = "subjects/project.work_item/wi-43.yaml"
    second["subject_shell"]["subject_id"] = "wi-43"
    assert isinstance(second["claim_type_artifact"], dict)
    second["claim_type_artifact"]["literal_schema"] = {
        "enum": ["blocked"],
        "type": "string",
    }
    _write(files, "claims/legacy-a.json", first)
    _write(files, "claims/legacy-b.json", second)

    with pytest.raises(SeedBundleError) as refusal:
        plan_seed_bundle(files, proposal_name="legacy")

    assert "carry different ClaimType artifacts" in str(refusal.value)
