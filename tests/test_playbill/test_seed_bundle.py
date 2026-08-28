"""PC-G-H3: grouping a seed bundle into the fewest proposals it can legally become.

The planner is pure -- bytes in, a plan out -- so everything here runs without a
daemon, a clock, or accepted state. That is the property the `--plan` flag and
the run manifest's `plan_digest` both rest on: two arms that print the same plan
digest are seeding the same world, and nothing about when or where they ran can
move it.

What is under test is the *minimization* and its limits. The minimum is not a
number this module picks; it is what the bundle's own declared closures allow,
given that only Claims have a plural authoring operation and that a proposal
settles against the base it was admitted at.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.authoring.seed import (
    SEED_GROUP_OPERATION_DIGEST_DOMAIN,
    SEED_GROUP_OPERATIONS,
    SeedBundleError,
    plan_seed_bundle,
    read_seed_bundle,
    render_seed_plan,
    seed_group_operation_digest,
    seed_group_proposal_name,
    seed_plan_digest,
)
from cruxible_client.contracts.canonical import Sha256Value, typed_digest

EXAMPLE = Path(__file__).resolve().parents[2] / "benchmarks/playbill_taubench/seed-example"


def example_bundle() -> dict[str, bytes]:
    """The committed example bundle, read exactly as the CLI reads it."""

    return {
        path.relative_to(EXAMPLE).as_posix(): path.read_bytes()
        for path in sorted(EXAMPLE.rglob("*"))
        if path.is_file()
    }


def _entry(files: dict[str, bytes], path: str) -> dict[str, Any]:
    return dict(json.loads(files[path].decode("utf-8")))


def _write(files: dict[str, bytes], path: str, payload: Any) -> None:
    files[path] = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")


def test_the_example_bundle_plans_three_proposals_and_names_what_rides_along() -> None:
    """Twelve files, four artifacts carried, three proposals.

    The example declares its ClaimType and all three Subjects at top level *and*
    carries them inside the Claim authorings. Neither is redundant authoring: the
    top-level files are what a reader of the bundle looks at, and the carried
    copies are what makes them free. The plan says which is which by name.
    """

    plan = plan_seed_bundle(example_bundle(), proposal_name="taubench-example")

    assert plan.group_ids == (
        "claims",
        "query_definition:project.work_items",
        "procedure:project.work_item.digest",
    )
    claims = plan.group("claims")
    assert claims.operation == SEED_GROUP_OPERATIONS["claim"]
    assert claims.entry_paths == (
        "claims/wi-101-status.json",
        "claims/wi-102-status.json",
        "claims/wi-103-status.json",
    )
    assert {item.path for item in plan.carried} == {
        "claim-types/project.work_item.status.json",
        "subjects/wi-101.json",
        "subjects/wi-102.json",
        "subjects/wi-103.json",
    }
    assert all(item.carried_by.startswith("claims/") for item in plan.carried)
    assert plan.body_paths == (
        "bodies/corpus/handbook.md",
        "bodies/corpus/runbook.md",
        "bodies/notes/scratch.md",
    )
    assert plan.next_group_id("claims") == "query_definition:project.work_items"
    assert plan.next_group_id("query_definition:project.work_items") == (
        "procedure:project.work_item.digest"
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
    group = first.group("claims")

    expected = typed_digest(
        Sha256Value,
        SEED_GROUP_OPERATION_DIGEST_DOMAIN,
        {
            "plan_digest": seed_plan_digest(first).tagged,
            "group_id": group.group_id,
        },
    )

    assert seed_group_operation_digest(first, group) == expected
    assert seed_group_operation_digest(relabeled, relabeled.group("claims")) == expected
    assert seed_group_proposal_name(first, group) == f"seed-{expected.value}"
    assert (
        seed_group_operation_digest(
            first,
            first.group("query_definition:project.work_items"),
        )
        != expected
    )


def test_an_artifact_no_claim_carries_earns_its_own_proposal() -> None:
    """The minimization is the bundle's closures, not a rewriting of them.

    Drop the closure out of every Claim and the ClaimType has to be proposed on
    its own surface first, because there is no plural operation for it and
    nothing else in the change set admits it. The planner never adds the closure
    back: deciding that a Claim should carry a dependency is an authoring
    decision the admission laws adjudicate, not a convenience this module takes.
    """

    files = example_bundle()
    stripped = _entry(files, "claims/wi-101-status.json")
    stripped["claim_type_artifact"] = None
    _write(files, "claims/wi-101-status.json", stripped)

    plan = plan_seed_bundle(files, proposal_name="taubench-example")

    assert plan.group_ids == (
        "claim_type:project.work_item.status",
        "claims",
        "query_definition:project.work_items",
        "procedure:project.work_item.digest",
    )
    assert (
        plan.group("claim_type:project.work_item.status").operation
        == (SEED_GROUP_OPERATIONS["claim_type"])
    )
    # The Subjects are still carried, so they still cost nothing.
    assert {item.kind for item in plan.carried} == {"subject"}


def test_a_bundle_that_cannot_be_legally_grouped_refuses_with_the_conflict_it_is() -> None:
    """Two byte strings, one canonical path, one change set: there is no grouping.

    This is the cross-authoring conflict the retired seed apply path had to
    refuse before reaching the proposal service. It is caught at plan time so it is
    reported before a single body is stored rather than three groups later.
    """

    files = example_bundle()
    divergent = _entry(files, "claim-types/project.work_item.status.json")
    divergent["literal_schema"] = {"enum": ["done", "ready"], "type": "string"}
    _write(files, "claim-types/project.work_item.status.json", divergent)

    with pytest.raises(SeedBundleError) as refusal:
        plan_seed_bundle(files, proposal_name="taubench-example")

    assert "one canonical path in one change set" in str(refusal.value)
    assert "project.work_item.status" in str(refusal.value)


def test_two_claims_carrying_different_copies_of_one_dependency_refuse_too() -> None:
    """The same law on the other side of the closure."""

    files = example_bundle()
    second = _entry(files, "claims/wi-102-status.json")
    carried = _entry(files, "claims/wi-101-status.json")["claim_type_artifact"]
    carried["literal_schema"] = {"enum": ["blocked"], "type": "string"}
    second["claim_type_artifact"] = carried
    _write(files, "claims/wi-102-status.json", second)

    with pytest.raises(SeedBundleError) as refusal:
        plan_seed_bundle(files, proposal_name="taubench-example")

    assert "carry different ClaimType artifacts" in str(refusal.value)


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
