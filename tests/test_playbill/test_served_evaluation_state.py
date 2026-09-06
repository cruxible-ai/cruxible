"""Served preflight, submission and settlement reuse exact derivations, not laws."""

import pytest

from cruxible_core.playbill import evaluation_state_cache as cache_module
from cruxible_core.playbill import proposals
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.proposals import AuthenticatedActor
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_authoring_change_set_intents import (
    _change_set,
    _claim,
    _coordinator,
)
from tests.test_playbill.test_authoring_preflight import TIMESTAMP, _seed_claim_surface


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_served_writes_reuse_dependency_state_and_match_cold_evaluation_and_reopen(
    tmp_path, monkeypatch, object_format
):
    instance, owner = initialize_local(tmp_path, object_format=object_format)
    _seed_claim_surface(instance, owner)
    actor = AuthenticatedActor(actor_id="owner")
    coordinator = _coordinator(instance)
    base = instance.accepted_coordinate()
    instance._evaluation_state_cache.derive(instance.tree_at(base.git_oid))

    def unexpected(*args, **kwargs):
        pytest.fail("warm served write rebuilt all accepted dependencies")

    for i in range(2):
        base = instance.accepted_coordinate()
        intent = coordinator.create(
            actor=actor,
            payload=_change_set(_claim(qualifier=f"sample-{i}")),
            canonical_timestamp=TIMESTAMP,
        ).intent
        with monkeypatch.context() as guarded:
            guarded.setattr(cache_module, "build_tree_state", unexpected)
            guarded.setattr(proposals, "build_tree_state", unexpected)
            submitted = coordinator.submit(intent.intent_id, actor=actor)
        assert submitted.status.state == "ready_to_activate"
        candidate = instance.proposal_evidence().read_candidate(submitted.status.candidate_digest)
        evaluation = instance.proposal_evidence().read_evaluation(submitted.status.proposal_id)
        tree = instance.proposal_tree(evaluation.evaluated_tree_oid)
        base_tree = instance.tree_at(base.git_oid)
        oracle = proposals.evaluate_proposal_tree(
            base_tree=base_tree,
            current_tree=base_tree,
            proposed_tree=tree,
            current=base,
            bodies=instance.body_store(),
            timestamp=TIMESTAMP,
            rebased=False,
            actor_id="owner",
        )
        assert not oracle.diagnostics
        assert oracle.candidate == candidate
        assert oracle.tree == tree
        attestation = _sign(owner, candidate.candidate_digest, base.semantic_root)
        service_submit_playbill_approval(
            instance,
            proposal_id=submitted.status.proposal_id,
            attestation=attestation.attestation,
            authenticated_submitter="owner",
        )
        with monkeypatch.context() as guarded:
            guarded.setattr(cache_module, "build_tree_state", unexpected)
            guarded.setattr(proposals, "build_tree_state", unexpected)
            receipt = service_activate_playbill_proposal(
                instance, proposal_id=submitted.status.proposal_id, activated_by="owner"
            )
        assert receipt.status == "accepted"
        assert instance.accepted_coordinate() != base

    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert reopened.accepted_coordinate() == instance.accepted_coordinate()
    assert reopened.accepted_history() == instance.accepted_history()
    assert reopened._evaluation_state_cache._state is None
    instance.refresh()
    assert instance._evaluation_state_cache._state is None
