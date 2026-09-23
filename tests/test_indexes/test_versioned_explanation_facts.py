"""An artifact version's explanation names its accepting generation, at every later read."""

from __future__ import annotations

import json
from pathlib import Path

from cruxible_core.service.authoring.documents import PlaybillAcceptedCoordinate
from cruxible_core.service.claims.subjects import service_list_playbill_subjects
from tests.core_support._candidate_support import submit_query_definition_candidate
from tests.core_support._knowledge_loop_support import (
    TIMESTAMP,
    accept_proposal,
    seed_claims,
    work_item_query,
)


def _explanations(instance, coordinate) -> dict[str, list[dict[str, object]]]:
    listing = service_list_playbill_subjects(
        instance, at=PlaybillAcceptedCoordinate.from_internal(coordinate)
    )
    return {
        str(view.envelope["identity"]): sorted(
            (
                fact
                for fact in view.facts
                if str(fact["schema_id"]).startswith("playbill.subject.")
                and "attestation" in str(fact["schema_id"])
            ),
            key=lambda fact: json.dumps(fact, sort_keys=True),
        )
        for view in listing.subjects
    }


def _proof_coordinates(facts: object) -> set[str]:
    found: set[str] = set()

    def walk(value: object) -> None:
        if isinstance(value, dict):
            accepted = value.get("accepted_coordinate")
            if isinstance(accepted, dict):
                found.add(str(accepted["git_oid"]))
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(facts)
    return found


def test_unchanged_artifacts_explain_the_same_at_every_later_coordinate(tmp_path: Path) -> None:
    instance, owner = seed_claims(tmp_path)
    before = instance.accepted_coordinate()
    first = _explanations(instance, before)
    assert any(first.values()), "the seeded Subjects carry attestation explanations"

    unrelated = submit_query_definition_candidate(
        instance,
        query=work_item_query("unrelated.query"),
        actor_id="owner",
        proposal_name="unrelated-query",
        timestamp=TIMESTAMP,
    )
    accept_proposal(instance, owner, unrelated)
    after = instance.accepted_coordinate()
    assert after.git_oid != before.git_oid

    second = _explanations(instance, after)
    assert second == first
    # Every proof names a generation that existed before the unrelated change:
    # the one that accepted the Subject, never the coordinate being read.
    history = {generation.oid for generation in instance.accepted_history()}
    cited = _proof_coordinates(second)
    assert cited and after.git_oid not in cited
    assert cited <= history
