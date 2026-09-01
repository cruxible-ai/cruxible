"""Nested projection observation succession and closed queue-vocabulary pins."""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from cruxible_core.service.playbill_next import (
    NextReason,
    NextRepairOperation,
    PlaybillNextSourceObservationV3,
    PlaybillNextSourceObservationV4,
    PlaybillNextWorkspaceObservationV1,
)
from tests.test_client.test_playbill_projection_observation import _CoverageClient, _observe
from tests.test_client.test_playbill_projection_repin import _repin, _RepinClient, _workspace


def _v4(root: Path) -> dict[str, object]:
    _workspace(root)
    _repin(_RepinClient(), root, claims=("CLM-first",))
    observation, _coordinate = _observe(_CoverageClient(), root)
    return observation["source_observations"][0]  # type: ignore[index,no-any-return]


def test_nested_union_refuses_v1_v2_and_accepts_strict_tagged_v3_v4(
    tmp_path: Path,
) -> None:
    previous = {
        "source_id": "corpus.runbook",
        "observed_source_digest": "sha256:" + "a" * 64,
    }
    richer = _v4(tmp_path)
    scanned = [
        item["commitment_digest"]  # type: ignore[index]
        for item in richer["commitment_scan_proofs"]  # type: ignore[union-attr]
    ]
    prior_v2 = {
        "tag": "playbill-next-source-observation-v2",
        "source_id": richer["source_id"],
        "observed_source_digest": richer["observed_source_digest"],
        "byte_length": richer["byte_length"],
        "marker_summaries": richer["marker_summaries"],
        "occurrences": richer["occurrences"],
        "scanned_commitment_digests": scanned,
        "scan_complete": True,
        "scan_notes": richer["scan_notes"],
        "marker_notes": richer["marker_notes"],
    }
    prior_v3 = {**prior_v2, "tag": "playbill-next-source-observation-v3", "document_id": "runbook"}
    assert (
        PlaybillNextSourceObservationV3.model_validate(prior_v3).model_dump(mode="json") == prior_v3
    )
    result = PlaybillNextWorkspaceObservationV1.model_validate({"source_observations": [richer]})
    assert isinstance(result.source_observations[0], PlaybillNextSourceObservationV4)  # type: ignore[index]
    assert result.source_observations[0].model_dump(mode="json") == richer  # type: ignore[index]

    with pytest.raises(ValidationError):
        PlaybillNextWorkspaceObservationV1.model_validate({"source_observations": [previous]})
    with pytest.raises(ValidationError):
        PlaybillNextWorkspaceObservationV1.model_validate({"source_observations": [prior_v2]})


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"extra": "not allowed"}),
        lambda value: value.update({"tag": "playbill-next-source-observation-v5"}),
        lambda value: value.update({"commitment_scan_proofs": []}),
        lambda value: value["occurrences"][0].update({"identity_digest": "sha256:" + "f" * 64}),
        lambda value: value["occurrences"][0]["source"].update({"identity": "corpus.other"}),
        lambda value: value["occurrences"][0]["line_overlay"].update({"end_byte": 10_000_000}),
        lambda value: value["commitment_scan_proofs"][0]["source"].update(
            {"identity": "corpus.other"}
        ),
        lambda value: value.update({"scan_notes": ["z", "a"]}),
    ],
)
def test_nested_v4_refuses_unknown_fields_and_unproved_or_mismatched_occurrences(
    tmp_path: Path, mutation: object
) -> None:
    candidate = _v4(tmp_path)
    mutation(candidate)  # type: ignore[operator]

    with pytest.raises(ValidationError):
        PlaybillNextSourceObservationV4.model_validate(candidate)


def test_nested_queue_vocabulary_adds_exactly_the_ratified_projection_variants() -> None:
    assert set(get_args(NextReason)) == {
        "claim_conflicted",
        "claim_uncovered",
        "claim_stale_evidence",
        "citation_drifted",
        "citation_source_unobserved",
        "evidence_expiring",
        "floor_missing",
        "floor_stale",
        "floor_invalid",
        "projection_dirty",
        "projection_backing_stale",
        "projection_marker_invalid",
        "self_published_source_stale",
        "claim_dependency_stale",
        "claim_attestation_threshold_met",
        "claim_contradicting_evidence_available",
        "claim_new_evidence_supporting",
        "claim_new_evidence_unreviewed",
        "document_modified",
        "claim_cites_retired",
        "retired_claim_source_stale",
        "unregistered_projection_block",
        "provider_lane_unavailable",
        "procedure_projection_missing",
    }
    assert set(get_args(NextRepairOperation)) == {
        "playbill.authoring.create",
        "playbill.authoring.bind",
        "playbill.claim.retire",
        "playbill.floor.export",
        "playbill.block.repin",
        "playbill.document.propose",
        "hand_edit",
    }
