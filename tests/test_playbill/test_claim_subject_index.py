"""Subject indexes preserve policy results while excluding unrelated Claim bytes."""

from datetime import UTC, datetime

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.claim_types import (
    AcceptedClaimType,
    claim_type_digest,
    claim_type_path,
)
from cruxible_client.contracts.claims import (
    ClaimArtifactV3,
    ClaimRetirementAttributionV1,
    LiteralClaimObject,
    claim_artifact_digest,
    claim_path,
    render_claim,
)
from cruxible_client.contracts.policies import ClaimAdmissionPolicyV1, FreezeRequirementV1
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import AcceptedSubject, subject_digest, subject_path
from cruxible_core.playbill import claim_subject_index as index_module
from cruxible_core.playbill import proposals
from cruxible_core.playbill.claim_subject_index import (
    build_claim_subject_index,
    update_claim_subject_index,
)
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_claim_policy_value_demand import _make_claim
from tests.test_playbill.test_claims import _claim_type, _subject

AT = "2026-08-21T12:00:00.000000Z"
OTHER = "subjects/project.work_item/other.json"


def _at_subject(claim, subject):
    return claim.model_copy(
        update={
            "statement": claim.statement.model_copy(
                update={"subject": SemanticAddress.whole_artifact(subject)}
            )
        }
    )


def _tree(*claims):
    return {claim_path(c.identity.name): render_claim(c) for c in claims}


def _retire(claim):
    return ClaimArtifactV3(
        identity=claim.identity,
        statement=claim.statement,
        backing=claim.backing,
        pins=claim.pins,
        lifecycle=ArtifactLifecycle(
            state="retired", predecessor_digest=claim_artifact_digest(claim).tagged
        ),
        retirement=ClaimRetirementAttributionV1(reason="was-rescinded"),
    )


def test_incremental_subject_membership_matches_cold_across_move_retire_remove_and_rewind(
    monkeypatch,
):
    first, second = _make_claim(1), _make_claim(2)
    moved = _at_subject(first, OTHER)  # Pins still name the old Subject on purpose.
    states = [
        _tree(first, second),
        _tree(moved, second),
        _tree(_retire(moved), second),
        _tree(second),
        {},
        _tree(first, second),
    ]
    previous = {}
    index = build_claim_subject_index(previous)
    original = index_module.parse_claim
    calls = []

    def parse(content, *, path):
        calls.append(path)
        return original(content, path=path)

    for tree in states:
        changed = {p for p in previous.keys() | tree.keys() if previous.get(p) != tree.get(p)}
        expected = build_claim_subject_index(tree)
        calls.clear()
        with monkeypatch.context() as guarded:
            guarded.setattr(index_module, "parse_claim", parse)
            result = update_claim_subject_index(index, tree=tree, changed=changed)
        assert result == expected
        assert set(calls) == changed & tree.keys()
        assert index == build_claim_subject_index(previous)  # Caller-owned prior state unchanged.
        if claim_path(first.identity.name) in tree and tree[
            claim_path(first.identity.name)
        ] != render_claim(first):
            assert result.subject_by_claim[claim_path(first.identity.name)] == OTHER
        previous, index = tree, result


@pytest.mark.parametrize(
    "at",
    [
        "2026-08-20T23:59:59.000000Z",
        "2026-08-21T00:00:00.000000Z",
        "2026-08-22T00:00:00.000000Z",
    ],
)
def test_index_keeps_time_and_lifecycle_filtering_fresh_and_matches_full_values(at):
    first = _make_claim(1)
    timed = first.model_copy(
        update={
            "statement": first.statement.model_copy(
                update={
                    "effective_from": datetime(2026, 8, 21, tzinfo=UTC),
                    "effective_until": datetime(2026, 8, 22, tzinfo=UTC),
                }
            )
        }
    )
    tree = _tree(timed, _retire(_make_claim(2)), _at_subject(_make_claim(3), OTHER))
    index = build_claim_subject_index(tree)
    assert len(index.subject_by_claim) == 3
    expected = proposals._effective_claim_values(tree, evaluation_time=at)
    for subject, paths in index.claims_by_subject.items():
        actual = proposals._effective_claim_values({p: tree[p] for p in paths}, evaluation_time=at)
        assert actual.get(subject, {}) == expected.get(subject, {})


def _types():
    status = _claim_type().model_copy(
        update={
            "admission_policy": ClaimAdmissionPolicyV1(
                freeze_requirements=(
                    FreezeRequirementV1(
                        requirement_id="done-freezes-summary",
                        while_predicate="project.work_item.status",
                        while_values=("done",),
                        frozen_predicates=("project.work_item.summary",),
                    ),
                )
            )
        }
    )
    summary = status.model_copy(
        update={
            "identity": ArtifactIdentity(kind="ClaimType", name="project.work_item.summary"),
            "predicate": "project.work_item.summary",
        }
    )
    return status, summary


def _evaluate(instance, parent, candidate, scope, types, *, indexed):
    shells = [
        _subject(),
        _subject().model_copy(
            update={
                "identity": ArtifactIdentity(kind="Subject", name="project.work_item/other"),
                "subject_id": "other",
            }
        ),
    ]
    return proposals._claim_admission_evaluations(
        current_tree=parent,
        candidate_tree=candidate,
        scope=scope,
        timestamp=AT,
        subjects={
            subject_path(s.subject_kind, s.subject_id): AcceptedSubject(
                path=subject_path(s.subject_kind, s.subject_id),
                shell=s,
                artifact_digest=subject_digest(s).tagged,
            )
            for s in shells
        },
        claim_types={
            t.identity.qualified: AcceptedClaimType(
                path=claim_type_path(t.predicate),
                claim_type=t,
                artifact_digest=claim_type_digest(t).tagged,
            )
            for t in types
        },
        current=instance.accepted_coordinate(),
        query_facts_provider=None,
        replay_accounts=None,
        parent_claim_index=build_claim_subject_index(parent) if indexed else None,
        candidate_claim_index=build_claim_subject_index(candidate) if indexed else None,
    )


@pytest.mark.parametrize("status", ["done", "ready"])
def test_indexed_freeze_matches_full_oracle_without_parsing_unrelated_claims(
    tmp_path, monkeypatch, status
):
    instance, _ = initialize_local(tmp_path)
    types = _types()
    governing = _make_claim(1, claim_type=types[0], value=status)
    previous = _make_claim(2, claim_type=types[1])
    unrelated = _at_subject(_make_claim(3, claim_type=types[0], value="done"), OTHER)
    parent = _tree(governing, previous, unrelated)
    revised = previous.model_copy(
        update={
            "statement": previous.statement.model_copy(
                update={"object": LiteralClaimObject(value="blocked")}
            )
        }
    )
    candidate = {**parent, **_tree(revised)}
    scope = (claim_path(previous.identity.name),)
    expected = _evaluate(instance, parent, candidate, scope, types, indexed=False)
    original = proposals.parse_claim
    calls = []

    def parse(content, *, path):
        assert path != claim_path(unrelated.identity.name), "policy parsed an unrelated Subject"
        calls.append(path)
        return original(content, path=path)

    with monkeypatch.context() as guarded:
        guarded.setattr(proposals, "parse_claim", parse)
        actual = _evaluate(instance, parent, candidate, scope, types, indexed=True)
    assert actual == expected
    assert claim_path(governing.identity.name) in calls  # Unchanged cross-type input still read.
    assert bool(actual[-1]) == (status == "done")


def test_multiple_changed_subjects_and_retargeting_match_full_oracle(tmp_path):
    instance, _ = initialize_local(tmp_path)
    types = _types()
    first = _make_claim(1, claim_type=types[1])
    second = _at_subject(_make_claim(2, claim_type=types[1]), OTHER)
    parent = _tree(_make_claim(3, claim_type=types[0], value="done"), first, second)
    # Move one Claim out and revise the other; both parent/candidate buckets matter.
    candidate = {
        **parent,
        **_tree(
            _at_subject(first, OTHER), _at_subject(second, first.statement.subject.artifact_path)
        ),
    }
    scope = tuple(_tree(first, second))
    assert _evaluate(instance, parent, candidate, scope, types, indexed=True) == _evaluate(
        instance, parent, candidate, scope, types, indexed=False
    )


def test_malformed_changed_claim_preserves_predecessor_index():
    tree = _tree(_make_claim(1), _make_claim(2))
    index = build_claim_subject_index(tree)
    path = next(iter(tree))
    malformed = {**tree, path: b"{}\n"}
    with pytest.raises(Exception) as cold:
        build_claim_subject_index(malformed)
    with pytest.raises(type(cold.value)) as incremental:
        update_claim_subject_index(index, tree=malformed, changed=(path,))
    assert str(incremental.value) == str(cold.value)
    assert index == build_claim_subject_index(tree)


def test_subject_index_remains_detached_in_shared_evaluation_cache():
    from cruxible_core.playbill.evaluation_state_cache import EvaluationStateCache

    tree = _tree(_make_claim(1))
    cache = EvaluationStateCache()
    state = cache.derive(tree)
    state.claim_subjects.subject_by_claim.clear()
    state.claim_subjects.claims_by_subject.clear()
    assert cache.derive(tree).claim_subjects == build_claim_subject_index(tree)
