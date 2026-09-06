"""A disposable index retains complete lineage and exact coordinate semantics."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cruxible_client.contracts.claims import claim_artifact_digest, claim_path, render_claim
from cruxible_client.contracts.errors import PlaybillError, PlaybillFormatError
from cruxible_core.service import playbill_projection_lineage as lineage
from tests.test_client.test_playbill_block_sync import OLD_COORDINATE
from tests.test_playbill.test_claims import _claim


def _fixture():
    claims = [
        _claim(
            claim_id="CLM-" + letter * 32,
            capture_digest="sha256:" + "1" * 64,
            source_digest="sha256:" + "2" * 64,
            source_length=1,
        )
        for letter in ("a", "b")
    ]
    paths = tuple(claim_path(c.identity.name) for c in claims)
    generations = [
        SimpleNamespace(
            oid=str(i) * 64,
            semantic_root=SimpleNamespace(tagged="sha256:" + str(i) * 64),
            generation_root=SimpleNamespace(tagged="sha256:" + str(i) * 64),
            sequence=i,
        )
        for i in range(1, 4)
    ]

    def coordinate(oid):
        return SimpleNamespace(
            git_oid=oid,
            semantic_root="sha256:" + oid,
            generation_root="sha256:" + oid,
            compiler=SimpleNamespace(rule_digest=OLD_COORDINATE.compiler_digest),
        )

    trees = {g.oid: dict(zip(paths, map(render_claim, claims))) for g in generations}
    instance = Mock()
    instance.resolve_accepted_coordinate.side_effect = lambda **kw: coordinate(kw["git_oid"])
    instance.coordinate_for_oid.side_effect = coordinate
    instance.accepted_history.return_value = tuple(generations)
    instance.blobs_at.side_effect = lambda oid, wanted: {
        p: trees[oid][p] for p in wanted if p in trees[oid]
    }
    at = OLD_COORDINATE.model_copy(update={"git_oid": generations[-1].oid})
    return instance, claims, paths, generations, trees, at


def test_batch_rebuild_warm_reuse_extension_and_historical_rollback():
    instance, claims, paths, generations, trees, at = _fixture()
    cold = lineage.read_claim_lineages(instance, paths=paths, at=at)
    assert instance.blobs_at.call_count == 3  # generations, not Claims times generations
    assert all(len(nodes) == 1 for nodes in cold.values())
    instance.blobs_at.reset_mock()
    assert lineage.read_claim_lineages(instance, paths=paths, at=at) == cold
    instance.blobs_at.assert_not_called()
    # Returned mutable model fields cannot poison a later read.
    cold[paths[0]].clear()
    warm = lineage.read_claim_lineages(instance, paths=paths, at=at)
    assert warm[paths[0]]
    assert next(iter(warm[paths[0]].values())).claim.backing.capture_digests
    successor = claims[0].model_copy(
        update={
            "lifecycle": claims[0].lifecycle.model_copy(
                update={
                    "predecessor_digest": claim_artifact_digest(claims[0]).tagged,
                    "state": "retired",
                }
            )
        }
    )
    g = SimpleNamespace(
        oid="4" * 64,
        semantic_root=SimpleNamespace(tagged="sha256:" + "4" * 64),
        generation_root=SimpleNamespace(tagged="sha256:" + "4" * 64),
        sequence=4,
    )
    instance.accepted_history.return_value = (*generations, g)
    trees[g.oid] = {**trees[at.git_oid], paths[0]: render_claim(successor)}
    new_at = at.model_copy(update={"git_oid": g.oid})
    extended = lineage.read_claim_lineages(instance, paths=paths, at=new_at)
    assert instance.blobs_at.call_count == 1
    assert len(extended[paths[0]]) == 2
    # Pinning an earlier coordinate never includes a cached future successor.
    earlier = lineage.read_claim_lineages(instance, paths=paths, at=at)
    assert earlier == warm
    lineage.clear_lineage_index(instance)
    assert lineage.read_claim_lineages(instance, paths=paths, at=new_at) == extended


def test_eviction_is_only_a_rebuild_and_missing_path_can_appear(monkeypatch):
    instance, claims, paths, generations, trees, at = _fixture()
    monkeypatch.setattr(lineage, "_MAX_PATHS", 1)
    expected = lineage.read_claim_lineages(instance, paths=paths, at=at)
    instance.blobs_at.reset_mock()
    assert lineage.read_claim_lineages(instance, paths=paths, at=at) == expected
    assert instance.blobs_at.call_count == 3  # evicted path only
    assert all(call.args[1] == (paths[0],) for call in instance.blobs_at.call_args_list)
    missing = claim_path("CLM-" + "c" * 32)
    assert lineage.read_claim_lineages(instance, paths=(missing,), at=at) == {missing: {}}


def test_request_history_prefix_stays_fixed_during_concurrent_acceptance():
    instance, claims, paths, generations, trees, at = _fixture()
    original = instance.blobs_at.side_effect

    def read(oid, wanted):
        instance.accepted_history.return_value = (*generations, SimpleNamespace(oid="4" * 64))
        return original(oid, wanted)

    instance.blobs_at.side_effect = read
    result = lineage.read_claim_lineages(instance, paths=paths, at=at)
    assert len(result[paths[0]]) == 1
    assert [call.args[0] for call in instance.blobs_at.call_args_list] == [
        g.oid for g in generations
    ]


def test_failed_parse_does_not_publish_partial_index():
    instance, claims, paths, generations, trees, at = _fixture()
    trees[generations[1].oid][paths[1]] = b"not a claim"
    with pytest.raises(PlaybillError):
        lineage.read_claim_lineages(instance, paths=paths, at=at)
    instance.blobs_at.reset_mock()
    trees[generations[1].oid][paths[1]] = render_claim(claims[1])
    assert len(lineage.read_claim_lineages(instance, paths=paths, at=at)[paths[1]]) == 1
    assert instance.blobs_at.call_count == 3


@pytest.mark.parametrize("read_failure", [False, True])
def test_batch_errors_preserve_first_marker_refusal(read_failure):
    from cruxible_client.contracts.authoring.models import PlaybillBlockSyncReadRequestV1
    from cruxible_client.contracts.claims import claim_statement_digest
    from cruxible_client.contracts.declared_blocks import (
        ProjectionBlockStampV1,
        ProjectionClaimBackingV1,
    )
    from cruxible_core.service.playbill_projection_sync import (
        service_read_playbill_block_sync_backing,
    )

    instance, claims, paths, generations, trees, at = _fixture()
    at = at.model_copy(
        update={"semantic_root": "sha256:" + at.git_oid, "generation_root": "sha256:" + at.git_oid}
    )
    instance.accepted_coordinate.return_value = instance.coordinate_for_oid(at.git_oid)
    instance.tree_at.side_effect = lambda oid: trees[oid]
    instance.blob_at.side_effect = lambda oid, path: trees[oid].get(path)
    trees[generations[1].oid][paths[1]] = b"corrupt historical claim"
    if read_failure:
        batch = instance.blobs_at.side_effect
        single = instance.blob_at.side_effect

        def read_batch(oid, wanted):
            if oid == generations[1].oid:
                raise PlaybillFormatError("historical blob unavailable")
            return batch(oid, wanted)

        def read_one(oid, path):
            if oid == generations[1].oid and path == paths[1]:
                raise PlaybillFormatError("historical blob unavailable")
            return single(oid, path)

        instance.blobs_at.side_effect = read_batch
        instance.blob_at.side_effect = read_one

    backings = tuple(
        ProjectionClaimBackingV1(
            identity=c.identity, statement_digest=claim_statement_digest(c.statement).tagged
        )
        for c in claims
    )
    stamp = ProjectionBlockStampV1(
        source_id="example",
        block_id="example",
        declared_generation=3,
        declared_coordinate=at,
        body_digest="sha256:" + "a" * 64,
        backing=(
            backings[0].model_copy(update={"statement_digest": "sha256:" + "9" * 64}),
            backings[1],
        ),
    )
    result = service_read_playbill_block_sync_backing(
        instance, request=PlaybillBlockSyncReadRequestV1(stamp=stamp)
    )
    assert result.reason == "block_backing_changed"
    # Once the first backing is valid, the historical failure on the second
    # remains observable. It is not dropped, cached as absence, or swallowed.
    valid = stamp.model_copy(update={"backing": backings})
    with pytest.raises(PlaybillError):
        service_read_playbill_block_sync_backing(
            instance, request=PlaybillBlockSyncReadRequestV1(stamp=valid)
        )
