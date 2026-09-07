"""Same-call reuse is isolated from SDK certificates and mutable observations."""

from __future__ import annotations

import pytest

from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.proposal_models import (
    ProposalAdmissionRequest,
    ProposalReceiveLimits,
)
from cruxible_core.playbill import proposals
from cruxible_core.playbill.authoring import preflight
from cruxible_core.playbill.derived_state import DerivedState, SnapshotTree
from cruxible_core.playbill.prepared_evaluation import PreparedEvaluationAdapter
from cruxible_core.playbill.proposals import AuthenticatedActor
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_authoring_change_set_intents import _change_set, _claim, _coordinator
from tests.test_playbill.test_authoring_preflight import TIMESTAMP, _seed_claim_surface


@pytest.fixture
def world(tmp_path):
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    intent = coordinator.create(
        actor=actor, payload=_change_set(_claim()), canonical_timestamp=TIMESTAMP
    ).intent
    computed = preflight.compute_preflight(instance, intent=intent, actor=actor)
    assert computed.evaluation is not None and computed.evaluation.candidate is not None
    return instance, coordinator, actor, intent, computed


def test_submit_evaluates_once_and_matches_full_preflight(world, monkeypatch):
    instance, coordinator, actor, intent, oracle = world
    calls = []
    original = proposals.evaluate_proposal_tree

    def counted(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(preflight, "evaluate_proposal_tree", counted)
    monkeypatch.setattr(proposals, "evaluate_proposal_tree", counted)
    result = coordinator.submit(intent.intent_id, actor=actor)
    assert len(calls) == 1
    assert instance.prepared_evaluations.status()["reused"] == 1
    candidate = instance.proposal_evidence().read_candidate(result.status.candidate_digest)
    assert candidate == oracle.evaluation.candidate
    # A separate preflight cannot supply a reusable certificate to this call.
    assert instance.prepared_evaluations.status()["started"] == 1


@pytest.mark.parametrize("reason", ["clear", "budget", "query", "receipt", "promotion"])
def test_invalidation_recomputes_and_preserves_candidate(world, monkeypatch, reason):
    instance, coordinator, actor, intent, oracle = world
    calls = []
    original = proposals.evaluate_proposal_tree

    def counted(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(preflight, "evaluate_proposal_tree", counted)
    monkeypatch.setattr(proposals, "evaluate_proposal_tree", counted)
    original_bind = coordinator._compute_and_bind_preflight

    def bind(self, *args, **kwargs):
        prepared = kwargs["prepared"]
        if reason == "budget":
            instance.prepared_evaluations.max_bytes = 0
        result = original_bind(*args, **kwargs)
        if reason == "clear":
            instance.derived.clear()
        elif reason in ("query", "receipt"):
            assert prepared.operational(lambda value: value)("observed") == "observed"
        elif reason == "promotion":

            class Verifier:
                def verify_promotion(self, value):
                    return value

            assert prepared.promotion(Verifier()).verify_promotion("observed") == "observed"
        return result

    monkeypatch.setattr(type(coordinator), "_compute_and_bind_preflight", bind)
    result = coordinator.submit(intent.intent_id, actor=actor)
    assert len(calls) == 2
    assert instance.prepared_evaluations.status()["reused"] == 0
    assert (
        instance.proposal_evidence().read_candidate(result.status.candidate_digest)
        == oracle.evaluation.candidate
    )


class Bodies:
    def __init__(self):
        self.content = b"original"
        self.present = True
        self.raise_read = False
        self.calls = []

    def read(self, digest, *, access):
        self.calls.append(("read", digest, access))
        if self.raise_read:
            raise ValueError("missing or corrupt body")
        return self.content

    def verify(self, digest):
        self.calls.append(("verify", digest))
        return self.present

    def store(self, content):
        self.content = content
        return None


def retained(world, adapter, scope):
    instance, _coordinator_, actor, _intent_, computed = world
    request = ProposalAdmissionRequest(
        target_ref="refs/proposals/owner/test",
        proposed_base_oid=instance.accepted_coordinate().git_oid,
    )
    fields = dict(
        current=instance.accepted_coordinate(),
        actor=actor,
        request=request,
        limits=ProposalReceiveLimits(),
        timestamp=TIMESTAMP,
    )
    scope.retain(computed.evaluation, operation=b"intent-and-descriptor", **fields)
    assert scope.handoff(b"intent-and-descriptor") is scope
    return dict(owner=adapter, tree=scope.submission_tree, bodies=instance.body_store(), **fields)


@pytest.mark.parametrize(
    "change", ["actor", "timestamp", "ref", "limits", "current", "tree", "owner"]
)
def test_every_handoff_binding_mismatch_falls_back(world, change):
    adapter = PreparedEvaluationAdapter(DerivedState())
    with adapter.scope() as scope:
        args = retained(world, adapter, scope)
        if change == "actor":
            args["actor"] = AuthenticatedActor(actor_id="another")
        elif change == "timestamp":
            args["timestamp"] = "2026-08-21T12:00:01.000000Z"
        elif change == "ref":
            args["request"] = args["request"].model_copy(
                update={"target_ref": "refs/proposals/owner/other"}
            )
        elif change == "limits":
            args["limits"] = args["limits"].model_copy(update={"max_file_bytes": 1})
        elif change == "current":
            args["current"] = args["current"].model_copy(
                update={"generation_root": "sha256:" + "f" * 64}
            )
        elif change == "tree":
            args["tree"] = SnapshotTree(dict(args["tree"]))
        else:
            args["owner"] = PreparedEvaluationAdapter(DerivedState())
        assert scope.take(**args) is None
        assert adapter.status()["reused"] == 0


@pytest.mark.parametrize("change", ["bytes", "missing", "error", "unchanged"])
def test_cas_reads_are_rechecked_with_the_original_access(world, change):
    adapter = PreparedEvaluationAdapter(DerivedState())
    store = Bodies()
    access = BodyAccessContext(principal_id="read-principal", can_read_body=True)
    with adapter.scope() as scope:
        observed = scope.bodies(store)
        assert observed.verify("digest") is True
        assert observed.read("digest", access=access) == b"original"
        args = retained(world, adapter, scope)
        if change == "bytes":
            store.content = b"changed"
        elif change == "missing":
            store.present = False
        elif change == "error":
            store.raise_read = True
        result = scope.take(**{**args, "bodies": store})
        assert (result is not None) == (change == "unchanged")
        assert len(store.calls) > 2
        if change == "unchanged":
            assert store.calls[:2] == store.calls[2:]
            assert scope.take(**args) is None


def test_operation_revision_expiry_and_result_mutation_cannot_poison_handoff(world):
    adapter = PreparedEvaluationAdapter(DerivedState())
    with adapter.scope() as scope:
        args = retained(world, adapter, scope)
        assert scope.handoff(b"another-intent-revision") is None
        original = world[-1].evaluation.candidate
        prior = original.candidate_digest
        original.__dict__["candidate_digest"] = "poisoned"
        try:
            result = scope.take(**args)
            assert result.candidate.candidate_digest == prior
        finally:
            original.__dict__["candidate_digest"] = prior
    assert scope.take(**args) is None
    assert scope.submission_tree is None


def test_unknown_body_operations_writes_and_inconsistent_observations_disable_reuse():
    adapter = PreparedEvaluationAdapter(DerivedState())
    store = Bodies()
    with adapter.scope() as scope:
        observed = scope.bodies(store)
        observed.verify("digest")
        store.present = False
        observed.verify("digest")
        assert scope.ineligible
    with adapter.scope() as scope:
        scope.bodies(store).store(b"write")
        assert scope.ineligible
    with adapter.scope() as scope:
        assert scope.bodies(store).present is False
        assert scope.ineligible


def test_fresh_cas_failure_reenters_evaluator_and_refuses_publication(world, monkeypatch):
    instance, coordinator, actor, intent, _oracle = world
    original_bind = coordinator._compute_and_bind_preflight
    original_bodies = instance.body_store()
    original_service = instance.proposal_service
    calls = []
    evaluate = proposals.evaluate_proposal_tree

    def counted(**kwargs):
        calls.append(kwargs)
        return evaluate(**kwargs)

    class MissingBodies:
        def verify(self, digest):
            return False

        def read(self, digest, *, access):
            return original_bodies.read(digest, access=access)

        def store(self, content):
            return original_bodies.store(content)

    def bind(self, *args, **kwargs):
        result = original_bind(*args, **kwargs)

        def changed_service():
            service = original_service()
            service.bodies = MissingBodies()
            return service

        monkeypatch.setattr(instance, "proposal_service", changed_service)
        return result

    monkeypatch.setattr(type(coordinator), "_compute_and_bind_preflight", bind)
    monkeypatch.setattr(preflight, "evaluate_proposal_tree", counted)
    monkeypatch.setattr(proposals, "evaluate_proposal_tree", counted)
    # Preserve the coordinator's existing unchanged-coordinate integrity error.
    with pytest.raises(RuntimeError, match="unchanged-coordinate preflight binding"):
        coordinator.submit(intent.intent_id, actor=actor)
    assert len(calls) == 2
    assert instance.prepared_evaluations.status()["invalidated"] == 1
    assert calls[-1]["bodies"].verify("missing") is False


def test_handoff_does_not_bypass_fresh_write_guard(world):
    instance = world[0]
    adapter = instance.prepared_evaluations
    with adapter.scope() as scope:
        args = retained(world, adapter, scope)
        service = instance.proposal_service()

        def read_only():
            raise proposals.ProposalAdmissionError("fresh writable guard")

        service._require_writable = read_only
        with pytest.raises(proposals.ProposalAdmissionError, match="fresh writable guard"):
            service.submit(
                actor=args["actor"],
                request=args["request"],
                candidate_tree=args["tree"],
                timestamp=args["timestamp"],
                prepared=scope,
            )
        assert not scope.consumed
