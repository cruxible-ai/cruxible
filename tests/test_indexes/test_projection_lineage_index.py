"""A disposable index retains complete lineage and exact coordinate semantics."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from cruxible_client.contracts.claims import claim_artifact_digest, claim_path, render_claim
from cruxible_client.contracts.errors import CruxibleError, FormatError
from cruxible_core.service.floor import projection_lineage as lineage
from tests.test_claims.test_claims import _claim
from tests.test_client.test_playbill_block_sync import OLD_COORDINATE


def _generation(i):
    return SimpleNamespace(
        oid=str(i) * 64,
        semantic_root=SimpleNamespace(tagged="sha256:" + str(i) * 64),
        generation_root=SimpleNamespace(tagged="sha256:" + str(i) * 64),
        sequence=i,
    )


def _member_digest(raw):
    from cruxible_client.contracts.canonical import file_digest
    from cruxible_client.contracts.claims import parse_claim

    try:
        return claim_artifact_digest(parse_claim(raw, path="claims/x.json")).tagged
    except CruxibleError:
        return file_digest(raw).tagged


class _History:
    """The history index the fixture's trees imply: a member wherever bytes moved."""

    def __init__(self, instance, trees):
        self.instance = instance
        self.trees = trees
        self.member_reads = 0

    def _prefix(self, at):
        generations = self.instance.accepted_history.return_value
        sequence = next(g.sequence for g in generations if g.oid == at.git_oid)
        return generations[: sequence + 1]

    def reader(self, at):
        from contextlib import nullcontext

        prefix = self._prefix(at)
        history = self

        def member_versions(paths):
            history.member_reads += 1
            found = {}
            for path in paths:
                rows = []
                for before, generation in zip(prefix, prefix[1:], strict=False):
                    old = self.trees[before.oid].get(path)
                    new = self.trees[generation.oid].get(path)
                    if old != new:
                        rows.append(
                            (generation.sequence, None if new is None else _member_digest(new))
                        )
                found[path] = tuple(rows)
            return found

        def versions_at(sequence):
            assert sequence == 0
            return tuple(SimpleNamespace(path=path) for path in self.trees[prefix[0].oid])

        def generation(sequence):
            return SimpleNamespace(git_oid=prefix[sequence].oid)

        return nullcontext(
            SimpleNamespace(
                member_versions=member_versions,
                versions_at=versions_at,
                generation=generation,
                generation_for_oid=lambda oid: next(g for g in prefix if g.oid == oid),
            )
        )


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
    genesis = _generation(0)
    generations = [genesis, *(_generation(i) for i in range(1, 4))]

    def coordinate(oid):
        return SimpleNamespace(
            git_oid=oid,
            semantic_root="sha256:" + oid,
            generation_root="sha256:" + oid,
            compiler=SimpleNamespace(rule_digest=OLD_COORDINATE.compiler_digest),
        )

    # Genesis holds no Claims; both are accepted at generation one and never move.
    trees = {genesis.oid: {}} | {
        g.oid: dict(zip(paths, map(render_claim, claims), strict=True)) for g in generations[1:]
    }
    instance = Mock()
    instance.resolve_accepted_coordinate.side_effect = lambda **kw: coordinate(kw["git_oid"])
    instance.coordinate_for_oid.side_effect = coordinate
    instance.accepted_history.return_value = tuple(generations)
    instance.blobs_at.side_effect = lambda oid, wanted: {
        p: trees[oid][p] for p in wanted if p in trees[oid]
    }
    history = _History(instance, trees)
    instance.accepted_history_reader.side_effect = lambda at=None: history.reader(at)
    at = OLD_COORDINATE.model_copy(update={"git_oid": generations[-1].oid})
    return instance, claims, paths, generations, trees, at


def test_batch_rebuild_warm_reuse_extension_and_historical_rollback():
    instance, claims, paths, generations, trees, at = _fixture()
    cold = lineage.read_claim_lineages(instance, paths=paths, at=at)
    # Only the generation that accepted them: revisions, not generations times Claims.
    assert instance.blobs_at.call_count == 1
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
    # Room for one Claim's source bytes: the second path evicts the first.
    monkeypatch.setattr(lineage, "_MAX_SOURCE_BYTES", len(render_claim(claims[0])) + 1)
    expected = lineage.read_claim_lineages(instance, paths=paths, at=at)
    instance.blobs_at.reset_mock()
    assert lineage.read_claim_lineages(instance, paths=paths, at=at) == expected
    assert instance.blobs_at.call_count == 1  # evicted path only, at its one revision
    assert all(call.args[1] == (paths[0],) for call in instance.blobs_at.call_args_list)
    missing = claim_path("CLM-" + "c" * 32)
    assert lineage.read_claim_lineages(instance, paths=(missing,), at=at) == {missing: {}}


def test_absent_paths_are_answered_but_never_retained():
    instance, claims, paths, generations, trees, at = _fixture()
    lineage.read_claim_lineages(instance, paths=paths, at=at)
    for batch in range(20):
        missing = tuple(
            claim_path("CLM-" + f"{batch:02x}{index:02x}".ljust(32, "c")) for index in range(50)
        )
        assert lineage.read_claim_lineages(instance, paths=missing, at=at) == {
            path: {} for path in missing
        }
    # A thousand distinct absent paths later, only the two real lineages are held.
    assert set(lineage._CACHE[instance]) == set(paths)  # noqa: SLF001
    instance.blobs_at.reset_mock()
    # Asking again re-reads only the history index: no blob read.
    assert lineage.read_claim_lineages(instance, paths=missing[:1], at=at) == {missing[0]: {}}
    instance.blobs_at.assert_not_called()


def test_request_history_prefix_stays_fixed_during_concurrent_acceptance():
    instance, claims, paths, generations, trees, at = _fixture()
    original = instance.blobs_at.side_effect

    def read(oid, wanted):
        instance.accepted_history.return_value = (*generations, SimpleNamespace(oid="4" * 64))
        return original(oid, wanted)

    instance.blobs_at.side_effect = read
    result = lineage.read_claim_lineages(instance, paths=paths, at=at)
    assert len(result[paths[0]]) == 1
    # The one revision generation within the fixed prefix, never the new one.
    assert [call.args[0] for call in instance.blobs_at.call_args_list] == [generations[1].oid]


def test_failed_parse_does_not_publish_partial_index():
    instance, claims, paths, generations, trees, at = _fixture()
    trees[generations[2].oid][paths[1]] = b"not a claim"
    with pytest.raises(CruxibleError):
        lineage.read_claim_lineages(instance, paths=paths, at=at)
    instance.blobs_at.reset_mock()
    trees[generations[2].oid][paths[1]] = render_claim(claims[1])
    assert len(lineage.read_claim_lineages(instance, paths=paths, at=at)[paths[1]]) == 1
    # Nothing was published, so both paths are read again at their one revision.
    assert instance.blobs_at.call_count == 1


@pytest.mark.parametrize("read_failure", [False, True])
def test_batch_errors_preserve_all_marker_findings(read_failure, monkeypatch):
    from cruxible_client.contracts.authoring.models import BlockSyncReadRequest
    from cruxible_client.contracts.claims import claim_statement_digest
    from cruxible_client.contracts.declared_blocks import (
        ProjectionBlockStampV1,
        ProjectionClaimBacking,
    )
    from cruxible_core.service.authoring.projection_sync import (
        service_read_playbill_block_sync_backing,
    )

    instance, claims, paths, generations, trees, at = _fixture()
    from contextlib import nullcontext

    # Both Claims are still owned at the checked coordinate; only the second
    # one's historical lineage is unreadable.
    present = {c.identity.qualified for c in claims}
    instance.bind_accepted_projection.return_value = nullcontext(
        SimpleNamespace(
            typed=SimpleNamespace(
                envelope=lambda identity: object() if identity in present else None
            )
        )
    )
    monkeypatch.setattr(
        "cruxible_core.service.authoring.projection_sync.ProjectionCheckContext._claim_status",
        lambda *a: "accepted",
    )
    # The read also reports whether the block is still registered (one stat of
    # the declaration file, which this mocked instance has no root for).
    monkeypatch.setattr(
        "cruxible_core.service.authoring.projection_sync.projection_block_registered",
        lambda *a, **k: True,
    )

    at = at.model_copy(
        update={"semantic_root": "sha256:" + at.git_oid, "generation_root": "sha256:" + at.git_oid}
    )
    instance.accepted_coordinate.return_value = instance.coordinate_for_oid(at.git_oid)
    instance.tree_at.side_effect = lambda oid: trees[oid]
    instance.blob_at.side_effect = lambda oid, path: trees[oid].get(path)
    trees[generations[2].oid][paths[1]] = b"corrupt historical claim"
    if read_failure:
        batch = instance.blobs_at.side_effect
        single = instance.blob_at.side_effect

        def read_batch(oid, wanted):
            if oid == generations[2].oid:
                raise FormatError("historical blob unavailable")
            return batch(oid, wanted)

        def read_one(oid, path):
            if oid == generations[2].oid and path == paths[1]:
                raise FormatError("historical blob unavailable")
            return single(oid, path)

        instance.blobs_at.side_effect = read_batch
        instance.blob_at.side_effect = read_one

    backings = tuple(
        ProjectionClaimBacking(
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
        instance, request=BlockSyncReadRequest(stamp=stamp)
    )
    assert result.reason == "block_backing_changed"
    # Once the first backing is valid, the historical failure on the second
    # remains observable. It is not dropped, cached as absence, or swallowed.
    valid = stamp.model_copy(update={"backing": backings})
    checked = service_read_playbill_block_sync_backing(
        instance, request=BlockSyncReadRequest(stamp=valid)
    )
    assert checked.status == "unchecked"
    assert checked.issues[-1].identity == backings[1].identity
    assert checked.issues[-1].status == "unchecked"
