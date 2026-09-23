"""Claim values and verdicts are read without materializing full Claim views."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.claim_reads import ClaimReadBatchRequestV1, ClaimValuesRequestV1
from cruxible_core.service.claims import claim_reads
from cruxible_core.service.claims.claim_reads import (
    service_read_claim_batch,
    service_read_claim_values,
)
from cruxible_core.service.discovery.search import claim_resolution_statuses
from cruxible_core.service.evidence.evidence import ClaimVerdictReadContext
from tests.core_support._knowledge_loop_support import seed_claims
from tests.test_integration.test_playbill_search import EVALUATION_TIME

SUBJECTS = (
    "subjects/project.work_item/wi-42.json",
    "subjects/project.work_item/wi-43.json",
)


def test_values_match_full_views_and_slot_statuses(tmp_path: Path, monkeypatch) -> None:
    instance, _owner = seed_claims(tmp_path)
    full = service_read_claim_batch(
        instance,
        request=ClaimReadBatchRequestV1(subject_paths=SUBJECTS, evaluation_time=EVALUATION_TIME),
    )
    expected = {view.envelope["identity"].removeprefix("Claim:"): view for view in full.claims}

    def unused(*args, **kwargs):
        pytest.fail("a values read must not materialize full Claim views")

    monkeypatch.setattr(claim_reads, "materialize_playbill_claim_view", unused)
    result = service_read_claim_values(
        instance,
        request=ClaimValuesRequestV1(subject_paths=SUBJECTS, evaluation_time=EVALUATION_TIME),
    )

    assert result.coordinate == full.coordinate
    assert {row.claim_id for row in result.values} == set(expected)
    context = ClaimVerdictReadContext(instance, instance.accepted_coordinate())
    statuses = claim_resolution_statuses(
        instance,
        claims=context.claims(),
        at=result.coordinate,
        evaluation_time=EVALUATION_TIME,
    )
    for row in result.values:
        view = expected[row.claim_id]
        statement = next(
            f["value"] for f in view.facts if f["schema_id"] == "playbill.claim.statement"
        )
        assert row.value == statement["object"]["value"]
        assert row.predicate == statement["predicate"]
        assert row.subject_path in SUBJECTS
        assert row.status == statuses[row.claim_id]
        # The same verdict the full view reports as its current verdict.
        current = next(
            f["value"] for f in view.facts if f["schema_id"] == "playbill.claim.current_verdict"
        )
        assert row.verdict == current["verdict"]


def test_values_selection_is_bounded_to_the_requested_predicates(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)
    result = service_read_claim_values(
        instance,
        request=ClaimValuesRequestV1(
            subject_paths=SUBJECTS,
            predicates=("project.work_item.nonexistent",),
            evaluation_time=EVALUATION_TIME,
        ),
    )
    assert result.values == ()
