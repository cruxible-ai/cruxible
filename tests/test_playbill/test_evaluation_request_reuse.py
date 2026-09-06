"""Request-local reuse preserves exact policy views and member evaluation."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_client.contracts.claims import (
    ClaimArtifactAny,
    ClaimFormatError,
    LiteralClaimObject,
    claim_path,
    render_claim,
)
from cruxible_core.playbill import proposals
from cruxible_core.playbill.authoring.preflight import compute_preflight
from cruxible_core.playbill.proposals import AuthenticatedActor
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_authoring_change_set_intents import (
    _change_set,
    _coordinator,
)
from tests.test_playbill.test_authoring_change_set_intents import (
    _claim as claim_payload,
)
from tests.test_playbill.test_authoring_preflight import TIMESTAMP, _seed_claim_surface
from tests.test_playbill.test_claims import _claim


def test_policy_view_reuses_only_identical_path_and_bytes_and_filters_each_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = _claim(
        claim_id="CLM-" + "1" * 32,
        capture_digest="sha256:" + "2" * 64,
        source_digest="sha256:" + "3" * 64,
        source_length=1,
    )
    start = datetime(2026, 8, 21, tzinfo=UTC)
    end = datetime(2026, 8, 22, tzinfo=UTC)
    claim = claim.model_copy(
        update={
            "statement": claim.statement.model_copy(
                update={"effective_from": start, "effective_until": end}
            )
        }
    )
    path = claim_path(claim.identity.name)
    parent = {path: render_claim(claim)}
    revised = claim.model_copy(
        update={
            "statement": claim.statement.model_copy(
                update={"object": LiteralClaimObject(value="blocked")}
            )
        }
    )
    candidate = {path: render_claim(revised)}
    samples = [
        (parent, "2026-08-20T23:59:59.000000Z"),
        (parent, "2026-08-21T00:00:00.000000Z"),
        (candidate, "2026-08-21T12:00:00.000000Z"),
        (candidate, "2026-08-22T00:00:00.000000Z"),
    ]
    oracle = [proposals._effective_claim_values(tree, evaluation_time=at) for tree, at in samples]
    calls = []
    parse = proposals.parse_claim

    def counted(content: bytes, *, path: str):
        calls.append((path, content))
        return parse(content, path=path)

    monkeypatch.setattr(proposals, "parse_claim", counted)
    memo: dict[tuple[str, bytes], ClaimArtifactAny] = {}
    actual = [
        proposals._effective_claim_values(tree, evaluation_time=at, parsed_claims=memo)
        for tree, at in samples
    ]
    assert actual == oracle
    assert actual[0] == actual[3] == {}
    assert actual[1] != actual[2]
    assert len(calls) == 2
    # A malformed changed body and a valid body under the wrong path must still
    # pass through canonical/path validation, even with a populated memo.
    for tree in (
        {path: b"{}"},
        {claim_path("CLM-" + "4" * 32): parent[path]},
    ):
        with pytest.raises(ClaimFormatError):
            proposals._effective_claim_values(tree, evaluation_time=TIMESTAMP, parsed_claims=memo)


def test_multi_claim_preflight_derives_accepted_referents_once_per_evaluation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    intent = coordinator.create(
        actor=actor,
        payload=_change_set(*(claim_payload(qualifier=f"sample-{index}") for index in range(4))),
        canonical_timestamp=TIMESTAMP,
    ).intent
    original = proposals.accepted_referent_coordinates_from_tree
    calls = []

    def counted(tree, *, current):
        calls.append(current)
        return original(tree, current=current)

    monkeypatch.setattr(proposals, "accepted_referent_coordinates_from_tree", counted)
    first = compute_preflight(instance, intent=intent, actor=actor)
    assert first.result.verdict == "passed"
    assert len(calls) == 1
    second = compute_preflight(instance, intent=intent, actor=actor)
    assert len(calls) == 2  # Not cached across evaluations or authority epochs.
    assert first.evaluation is not None and second.evaluation is not None
    assert first.evaluation.candidate == second.evaluation.candidate
    assert first.evaluation.tree == second.evaluation.tree


def test_removal_refusal_does_not_read_unneeded_referent_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinate = instance.accepted_coordinate()
    parent = instance.tree_at(coordinate.git_oid)
    candidate = dict(parent)
    del candidate["subjects/project.work_item/wi-42.json"]

    def unexpected(*args, **kwargs):
        pytest.fail("a removal-only refusal must not read referent history")

    monkeypatch.setattr(proposals, "accepted_referent_coordinates_from_tree", unexpected)
    result = proposals.evaluate_proposal_tree(
        base_tree=parent,
        current_tree=parent,
        proposed_tree=candidate,
        current=coordinate,
        bodies=instance.body_store(),
        timestamp=TIMESTAMP,
        actor_id="owner",
        rebased=False,
    )
    assert result.candidate is None
    assert [item.code for item in result.diagnostics] == ["playbill.subject.removal_unsupported"]
