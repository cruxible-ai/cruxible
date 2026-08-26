"""Nested projection observation succession and closed queue-vocabulary pins."""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from cruxible_core.service.playbill_next import (
    NextReason,
    NextRepairOperation,
    PlaybillNextSourceObservationV1,
    PlaybillNextSourceObservationV2,
    PlaybillNextSourceObservationV3,
    PlaybillNextWorkspaceObservationV1,
)
from tests.test_client.test_playbill_projection_observation import _CoverageClient, _observe
from tests.test_client.test_playbill_projection_repin import _repin, _RepinClient, _workspace


def _v2(root: Path) -> dict[str, object]:
    _workspace(root)
    _repin(_RepinClient(), root, claims=("CLM-first",))
    observation, _coordinate = _observe(_CoverageClient(), root)
    return observation["source_observations"][0]  # type: ignore[index,no-any-return]


def test_nested_union_preserves_exact_v1_v2_and_accepts_strict_tagged_v3(
    tmp_path: Path,
) -> None:
    previous = {
        "source_id": "corpus.runbook",
        "observed_source_digest": "sha256:" + "a" * 64,
    }
    assert PlaybillNextSourceObservationV1.model_validate(previous).model_dump() == previous
    richer = _v2(tmp_path)
    prior_v2 = {
        **richer,
        "tag": "playbill-next-source-observation-v2",
    }
    prior_v2.pop("document_id")
    assert (
        PlaybillNextSourceObservationV2.model_validate(prior_v2).model_dump(mode="json") == prior_v2
    )
    result = PlaybillNextWorkspaceObservationV1.model_validate({"source_observations": [richer]})
    assert isinstance(result.source_observations[0], PlaybillNextSourceObservationV3)  # type: ignore[index]
    assert result.source_observations[0].model_dump(mode="json") == richer  # type: ignore[index]

    legacy = PlaybillNextWorkspaceObservationV1.model_validate({"source_observations": [previous]})
    assert isinstance(legacy.source_observations[0], PlaybillNextSourceObservationV1)  # type: ignore[index]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"extra": "not allowed"}),
        lambda value: value.update({"tag": "playbill-next-source-observation-v4"}),
        lambda value: value.update({"scan_complete": False}),
        lambda value: value["occurrences"][0].update({"identity_digest": "sha256:" + "f" * 64}),
        lambda value: value["occurrences"][0]["source"].update({"identity": "corpus.other"}),
        lambda value: value["occurrences"][0]["line_overlay"].update({"end_byte": 10_000_000}),
        lambda value: value.update(
            {"scanned_commitment_digests": ["sha256:" + "f" * 64, "sha256:" + "a" * 64]}
        ),
        lambda value: value.update({"scan_notes": ["z", "a"]}),
    ],
)
def test_nested_v3_refuses_unknown_fields_and_unverified_or_incomplete_occurrences(
    tmp_path: Path, mutation: object
) -> None:
    candidate = _v2(tmp_path)
    mutation(candidate)  # type: ignore[operator]

    with pytest.raises(ValidationError):
        PlaybillNextSourceObservationV3.model_validate(candidate)


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
        "self_published_source_stale",
        "claim_dependency_stale",
        "document_modified",
    }
    assert set(get_args(NextRepairOperation)) == {
        "playbill.authoring.create",
        "playbill.authoring.bind",
        "playbill.claim_type.migrate",
        "playbill.floor.export",
        "playbill.block.repin",
        "playbill.document.propose",
    }
