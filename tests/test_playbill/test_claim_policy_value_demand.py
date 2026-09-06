"""Only freeze admission needs the complete time-filtered Claim value views."""

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.claim_types import (
    AcceptedClaimType,
    claim_type_digest,
    claim_type_path,
)
from cruxible_client.contracts.claims import LiteralClaimObject, claim_path, render_claim
from cruxible_client.contracts.policies import ClaimAdmissionPolicyV1, FreezeRequirementV1
from cruxible_client.contracts.query.definitions import query_definition_digest
from cruxible_client.contracts.subjects import AcceptedSubject, subject_digest, subject_path
from cruxible_core.playbill import proposals
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_claim_corroboration_integration import (
    PROPOSAL_TIMESTAMP,
    _claim_for_type,
    _corroborated_type,
    _query,
    _seed_vocabulary,
    _submit_claim,
)
from tests.test_playbill.test_claims import _claim, _claim_type, _subject


def _make_claim(number, *, claim_type=None, value="ready"):
    claim_type = claim_type or _claim_type()
    claim = _claim(
        claim_id="CLM-" + f"{number:032x}",
        capture_digest="sha256:" + "2" * 64,
        source_digest="sha256:" + "3" * 64,
        source_length=1,
    )
    return claim.model_copy(
        update={
            "statement": claim.statement.model_copy(
                update={
                    "claim_type": claim_type.identity,
                    "claim_type_digest": claim_type_digest(claim_type).tagged,
                    "predicate": claim_type.predicate,
                    "object": LiteralClaimObject(value=value),
                }
            )
        }
    )


def _run(instance, *, parent, candidate, scope, claim_types):
    subject = _subject()
    path = subject_path(subject.subject_kind, subject.subject_id)
    return proposals._claim_admission_evaluations(
        current_tree=parent,
        candidate_tree=candidate,
        scope=scope,
        timestamp=PROPOSAL_TIMESTAMP,
        subjects={
            path: AcceptedSubject(
                path=path, shell=subject, artifact_digest=subject_digest(subject).tagged
            )
        },
        claim_types={
            item.identity.qualified: AcceptedClaimType(
                path=claim_type_path(item.predicate),
                claim_type=item,
                artifact_digest=claim_type_digest(item).tagged,
            )
            for item in claim_types
        },
        current=instance.accepted_coordinate(),
        query_facts_provider=None,
        replay_accounts=None,
    )


def test_empty_policy_does_not_construct_effective_value_views(tmp_path, monkeypatch):
    instance, _owner = initialize_local(tmp_path)
    unchanged, changed = _make_claim(1), _make_claim(2)
    parent = {claim_path(unchanged.identity.name): render_claim(unchanged)}
    path = claim_path(changed.identity.name)

    def unexpected(*args, **kwargs):
        pytest.fail("empty admission policy must not construct full Claim value views")

    monkeypatch.setattr(proposals, "_effective_claim_values", unexpected)
    result = _run(
        instance,
        parent=parent,
        candidate={**parent, path: render_claim(changed)},
        scope=(path,),
        claim_types=(_claim_type(),),
    )
    assert result == ({}, {}, {}, (), ())


def test_corroboration_only_still_evaluates_real_accepted_query_without_value_views(
    tmp_path, monkeypatch
):
    instance, owner = initialize_local(tmp_path)
    query = _query()
    claim_type = _corroborated_type(query_definition_digest(query).tagged)
    _seed_vocabulary(instance, owner, claim_types=(claim_type,), query=query)
    claim = _claim_for_type(instance, claim_type, claim_id="CLM-" + "a1" * 16)

    def unexpected(*args, **kwargs):
        pytest.fail("corroboration reads accepted query facts, not effective policy values")

    monkeypatch.setattr(proposals, "_effective_claim_values", unexpected)
    result, _ = _submit_claim(instance, claim, proposal_name="lazy-corroboration")
    assert result.evaluation.verdict == "candidate"
    (account,) = result.evaluation.claim_admission_accounts
    assert account.satisfied
    (observed,) = account.corroboration_results
    assert observed.satisfied and observed.observed_count == 1
    assert observed.query_definition_digest == query_definition_digest(query).tagged


@pytest.mark.parametrize("status_value, expected", [("done", "refused"), ("ready", "eligible")])
def test_freeze_builds_complete_parent_candidate_views_once_and_preserves_cross_type_values(
    tmp_path, monkeypatch, status_value, expected
):
    instance, _owner = initialize_local(tmp_path)
    status_type = _claim_type().model_copy(
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
    summary_type = _claim_type().model_copy(
        update={
            "identity": ArtifactIdentity(kind="ClaimType", name="project.work_item.summary"),
            "predicate": "project.work_item.summary",
            "admission_policy": status_type.admission_policy,
        }
    )
    unchanged = _make_claim(1, claim_type=status_type, value=status_value)
    first = _make_claim(2, claim_type=summary_type)
    second = _make_claim(3, claim_type=summary_type)
    parent = {
        claim_path(item.identity.name): render_claim(item) for item in (unchanged, first, second)
    }
    revised = [
        item.model_copy(
            update={
                "statement": item.statement.model_copy(
                    update={"object": LiteralClaimObject(value="blocked")}
                )
            }
        )
        for item in (first, second)
    ]
    scope = tuple(claim_path(item.identity.name) for item in revised)
    candidate = {
        **parent,
        **{claim_path(item.identity.name): render_claim(item) for item in revised},
    }
    original = proposals._effective_claim_values
    oracle = [original(tree, evaluation_time=PROPOSAL_TIMESTAMP) for tree in (parent, candidate)]
    calls = []

    def counted(tree, **kwargs):
        values = original(tree, **kwargs)
        calls.append((tree, values))
        return values

    monkeypatch.setattr(proposals, "_effective_claim_values", counted)
    result = _run(
        instance,
        parent=parent,
        candidate=candidate,
        scope=scope,
        claim_types=(status_type, summary_type),
    )
    assert [tree for tree, _ in calls] == [parent, candidate]
    assert [values for _, values in calls] == oracle
    entries, _digests, _queries, accounts, diagnostics = result
    assert not accounts
    assert len(entries) == 2
    for path in scope:
        assert len(entries[path]) == 2  # Two governing types share the single view pair.
        assert all(entry["candidate_result"]["verdict"] == expected for entry in entries[path])
    assert {item.code for item in diagnostics} == (
        {"playbill.claim_policy.freeze_active"} if expected == "refused" else set()
    )
