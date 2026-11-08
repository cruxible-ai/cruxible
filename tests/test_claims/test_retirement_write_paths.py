"""Retirement records check member-local paths, including pre-existing edits."""

import pytest

from cruxible_client.contracts.claims import (
    ClaimRetireDependent,
    claim_artifact_digest,
    claim_path,
    parse_claim,
)
from cruxible_core.claims.claim_retirement import (
    ChangeRetirementClosures,
    RetirementClosureWalker,
    build_claim_retirement_candidate,
)
from cruxible_core.derived import derived_state
from tests.test_claims.test_claim_type_migrations import _accepted_claim_world


@pytest.mark.parametrize("earlier_edits", [1, 2000])
@pytest.mark.parametrize("extra_write", [None, "overwrite", "delete"])
def test_record_checks_exact_member_writes_without_rescanning_earlier_edits(
    tmp_path, monkeypatch, earlier_edits, extra_write
):
    instance, claim_id, _ = _accepted_claim_world(tmp_path)
    coordinate = instance.accepted_coordinate()
    before = instance.immutable_tree_at(coordinate.git_oid).fork()
    for index in range(earlier_edits):
        before[f"changesets/earlier-{index}.json"] = b"prior edit"
    path = claim_path(claim_id)
    claim = parse_claim(before[path], path=path)
    after, results = build_claim_retirement_candidate(
        before,
        root=ClaimRetireDependent(
            artifact_identity=claim.identity,
            predecessor_digest=claim_artifact_digest(claim).tagged,
            reason="was-wrong",
        ),
        dependents=(),
    )
    # The count shortcut saw edits {b} -> {a,b}, written {a}, and missed b's new bytes.
    extra = "changesets/earlier-0.json"
    if extra_write == "overwrite":
        after[extra] = b"a second edit of b"
    elif extra_write == "delete":
        del after[extra]

    def no_scan(*args, **kwargs):
        raise AssertionError("a retirement must not diff all prior staged edits")

    monkeypatch.setattr(derived_state, "changed_paths", no_scan)
    closures = ChangeRetirementClosures(instance, coordinate=coordinate)
    closures._walker = RetirementClosureWalker(lambda _identity: ())
    if extra_write is None:
        closures.record(before=before, after=after, results=results)
        assert closures._walker._retired == {claim.identity.qualified}
    else:
        with pytest.raises(AssertionError, match="outside its results' Claim paths"):
            closures.record(before=before, after=after, results=results)
        assert not closures._walker._retired


def test_fork_local_tracking_includes_deletes_and_does_not_accept_another_base():
    root = derived_state.SnapshotTree({"claims/a.json": b"a", "claims/b.json": b"b"})
    before = root.fork()
    before["claims/b.json"] = b"earlier"
    after = before.fork()
    after["claims/a.json"] = b"retired"
    del after["claims/b.json"]
    assert after.touched_paths_since(before) == {"claims/a.json", "claims/b.json"}
    assert after.touched_paths_since(root) is None
    assert after.fork().touched_paths_since(after) == frozenset()
