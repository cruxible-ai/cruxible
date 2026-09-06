"""Projection metadata batching preserves the exact requested backing set."""

from types import SimpleNamespace

import pytest

from cruxible_client.authoring.blocks import ProjectionRepinError, _claim_backings
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.claim_reads import ClaimBackingsResultV1
from cruxible_client.contracts.declared_blocks import ProjectionClaimBackingV1
from tests.test_client.test_playbill_block_sync import NEW_COORDINATE, OLD_COORDINATE


def test_metadata_batch_is_bounded_and_does_not_read_claim_verdicts():
    calls = []

    class Client:
        def get_playbill_claim_backings(self, instance_id, *, claim_ids, at):
            assert instance_id == "instance"
            assert at == OLD_COORDINATE.model_dump(mode="json")
            calls.append(claim_ids)
            return ClaimBackingsResultV1(
                coordinate=OLD_COORDINATE.model_dump(mode="json"),
                backings=tuple(
                    ProjectionClaimBackingV1(
                        identity=ArtifactIdentity(kind="Claim", name=name),
                        statement_digest="sha256:" + "a" * 64,
                    )
                    for name in claim_ids
                ),
            )

        def get_playbill_claim(self, *args, **kwargs):
            raise AssertionError("stamping must not materialize a verdict")

    names = tuple("CLM-" + f"{i:032x}" for i in range(300))
    result = _claim_backings(
        Client(),
        "instance",
        names=names,
        coordinate=OLD_COORDINATE,
        evaluation_time="2026-09-05T00:00:00Z",
    )
    assert [len(batch) for batch in calls] == [256, 44]
    assert tuple(b.identity.name for b in result) == names


@pytest.mark.parametrize("bad_coordinate", [False, True])
def test_incomplete_or_mixed_coordinate_backing_batch_refuses(bad_coordinate):
    class Client:
        def get_playbill_claim_backings(self, *args, **kwargs):
            return SimpleNamespace(
                coordinate=NEW_COORDINATE if bad_coordinate else OLD_COORDINATE, backings=()
            )

    with pytest.raises(ProjectionRepinError):
        _claim_backings(
            Client(),
            "instance",
            names=("CLM-" + "a" * 32,),
            coordinate=OLD_COORDINATE,
            evaluation_time="2026-09-05T00:00:00Z",
        )
