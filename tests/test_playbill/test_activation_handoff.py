"""Owned activation installs verified state; unsafe outcomes retain recovery."""

from __future__ import annotations

import fcntl
import os
import threading
from dataclasses import replace

import pytest

from cruxible_client.contracts.documents import render_document
from cruxible_core.playbill import activation as activation_module
from cruxible_core.playbill import instance as instance_module
from cruxible_core.playbill.bootstrap import render_principal
from cruxible_core.playbill.git import NOTE_REFS
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.keys import generate_client_principal_key
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from cruxible_core.playbill.serving import SERVING_MANIFEST_FILE
from cruxible_core.playbill.settlement import ChangeActorBinding, render_change_set
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_activation import _sign
from tests.test_playbill.test_proposals import TIMESTAMP, _shell


def _proposal(instance, owner, name, *, members=None):
    base = instance.accepted_coordinate()
    if members is None:
        body = instance.store_document_body(f"# {name}\n".encode())
        shell = _shell(body.digest).model_copy(
            update={"identity": f"document:{name}", "title": name}
        )
        members = {f"documents/{name}.json": render_document(shell)}
    proposal = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/owner/{name}", proposed_base_oid=base.git_oid
        ),
        candidate_tree={**instance.tree_at(base.git_oid), **members},
        timestamp=TIMESTAMP,
    )
    assert proposal.candidate is not None, proposal.evaluation.diagnostics
    approval = _sign(owner, proposal.candidate.candidate_digest, base.semantic_root)
    service_submit_playbill_approval(
        instance,
        proposal_id=proposal.admission.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter="owner",
    )
    return proposal


def _activate(instance, proposal):
    return service_activate_playbill_proposal(
        instance, proposal_id=proposal.admission.proposal_id, activated_by="owner"
    )


def _arguments(instance, proposal):
    evaluation = proposal.evaluation
    return dict(
        base=instance.coordinate_for_oid(evaluation.evaluated_base_oid),
        candidate_tree=instance.proposal_tree(evaluation.evaluated_tree_oid),
        candidate=proposal.candidate,
        approvals=instance.proposal_evidence().read_approvals(proposal.candidate.candidate_digest),
        actor_binding=ChangeActorBinding(actor_id=proposal.admission.actor_id),
        proposal_actor_id=proposal.admission.actor_id,
    )


def _assert_reopened_parity(instance):
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert instance.accepted_coordinate() == reopened.accepted_coordinate()
    assert instance.accepted_history() == reopened.accepted_history()
    assert instance._recovered.head.principals == reopened._recovered.head.principals
    assert instance._recovered.projection.manifest == reopened._recovered.projection.manifest
    with instance.bind_accepted_projection(instance.accepted_coordinate()):
        pass
    return reopened


def _count_recovery(monkeypatch):
    calls = []
    original = instance_module.recover_instance

    def recover(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(instance_module, "recover_instance", recover)
    return calls


@pytest.mark.parametrize("object_format", ["sha1", "sha256"])
def test_two_served_writes_handoff_without_recovery_and_detach_mutable_state(
    tmp_path, monkeypatch, object_format
):
    instance, owner = initialize_local(tmp_path, object_format=object_format)
    bundles = []
    original = instance.prepare_generation

    def prepare(**kwargs):
        bundle = original(**kwargs)
        bundles.append(bundle)
        return bundle

    monkeypatch.setattr(instance, "prepare_generation", prepare)
    with monkeypatch.context() as guarded:

        def unexpected(*args, **kwargs):
            pytest.fail("a clean owned activation must not call recover_instance")

        guarded.setattr(instance_module, "recover_instance", unexpected)
        for sequence, name in enumerate(("first", "second"), start=1):
            proposal = _proposal(instance, owner, name)
            old = instance.accepted_coordinate()
            instance.tree_at(old.git_oid)
            instance.coordinate_for_oid(old.git_oid)
            instance.claim_read_history_memo["sentinel"] = object()
            receipt = _activate(instance, proposal)
            assert receipt.status == "accepted"
            assert receipt.accepted_coordinate.git_oid == instance.accepted_coordinate().git_oid
            assert instance._recovered.head.sequence == sequence
            assert not instance._tree_memo and not instance.claim_read_history_memo
            assert instance._history_lookup is None
            with instance.bind_accepted_projection(instance.accepted_coordinate()):
                pass
            expected_record = render_change_set(instance.accepted_history()[-1].record)
            bundles[-1].record.law_digests.clear()
            bundles[-1].tree[bundles[-1].record_path] = b"mutated detached bundle\n"
            assert render_change_set(instance.accepted_history()[-1].record) == expected_record
    _assert_reopened_parity(instance)


def test_principal_registration_and_revocation_install_successor_registry(tmp_path, monkeypatch):
    instance, owner = initialize_local(tmp_path)
    newcomer = generate_client_principal_key(
        tmp_path / "newcomer-key",
        principal_id="newcomer",
        kind="ordinary",
        forbidden_roots=(instance.root,),
    ).principal
    with monkeypatch.context() as guarded:

        def unexpected(*args, **kwargs):
            pytest.fail("principal handoff must use the verified successor registry")

        guarded.setattr(instance_module, "recover_instance", unexpected)
        for name, principal in (
            ("register", newcomer),
            ("revoke", newcomer.model_copy(update={"status": "revoked"})),
        ):
            proposal = _proposal(
                instance,
                owner,
                name,
                members={"principals/newcomer.json": render_principal(principal)},
            )
            assert _activate(instance, proposal).status == "accepted"
            assert principal in instance._recovered.head.principals.principals
            assert (
                instance._recovered.head.principals.semantic_root
                == instance.accepted_coordinate().semantic_root
            )
        assert instance.accepted_history()[1].principals.require_active("newcomer") == newcomer
    _assert_reopened_parity(instance)


def test_lost_cas_recovers_winner_without_installing_loser(tmp_path, monkeypatch):
    instance, owner = initialize_local(tmp_path)
    winner = _proposal(instance, owner, "winner")
    loser = _proposal(instance, owner, "loser")
    kwargs = _arguments(instance, winner)
    winner_bundle = instance.prepare_generation(**kwargs, sequence=1)
    winner_publisher = instance.activation_publisher()
    winner_projection = winner_publisher.prebuild(winner_bundle, base=kwargs["base"])
    publisher = instance.activation_publisher()
    activate = publisher.activate

    def race(bundle, projection, **options):
        assert (
            winner_publisher.activate(winner_bundle, winner_projection, base=kwargs["base"]).status
            == "accepted"
        )
        return activate(bundle, projection, **options)

    monkeypatch.setattr(publisher, "activate", race)
    monkeypatch.setattr(instance, "activation_publisher", lambda: publisher)
    calls = _count_recovery(monkeypatch)
    receipt = _activate(instance, loser)
    assert receipt.status == "lost_cas" and receipt.accepted_coordinate is None
    assert len(calls) == 1
    assert instance.accepted_coordinate().git_oid == winner_bundle.oid
    assert (
        instance.accepted_history()[-1].record.candidate_digest == winner.candidate.candidate_digest
    )
    _assert_reopened_parity(instance)


@pytest.mark.parametrize("reason", ["epoch_changed", "successor_unavailable"])
def test_unsafe_handoff_recovers_instead_of_installing_captured_epoch(
    tmp_path, monkeypatch, reason
):
    instance, owner = initialize_local(tmp_path)
    proposal = _proposal(instance, owner, "fallback")
    publisher = instance.activation_publisher()
    if reason == "epoch_changed":
        original = publisher._activate_locked

        def changed_epoch(*args, **kwargs):
            result = original(*args, **kwargs)
            instance._recovered = replace(instance._recovered)
            return result

        monkeypatch.setattr(publisher, "_activate_locked", changed_epoch)
    else:
        monkeypatch.setattr(
            instance_module, "prepared_generation_for_handoff", lambda *a, **kw: None
        )
    monkeypatch.setattr(instance, "activation_publisher", lambda: publisher)
    calls = _count_recovery(monkeypatch)
    receipt = _activate(instance, proposal)
    assert receipt.status == "accepted" and len(calls) == 1
    assert instance.accepted_coordinate().git_oid == receipt.accepted_coordinate.git_oid
    assert instance._recovered.head.sequence == 1
    _assert_reopened_parity(instance)


@pytest.mark.parametrize("artifact", ["generation_note", "serving_manifest"])
def test_handoff_rechecks_published_artifacts_and_recovers_missing_state(
    tmp_path, monkeypatch, artifact
):
    instance, owner = initialize_local(tmp_path)
    proposal = _proposal(instance, owner, "missing-publication")
    publisher = instance.activation_publisher()
    activate = publisher._activate_locked

    def remove_published_artifact(*args, **kwargs):
        result = activate(*args, **kwargs)
        assert result.status == "accepted"
        if artifact == "generation_note":
            instance._ledger._git(
                ["notes", f"--ref={NOTE_REFS['generation']}", "remove", result.accepted.git_oid]
            )
        else:
            (publisher.publication_directory / SERVING_MANIFEST_FILE).unlink()
        return result

    monkeypatch.setattr(publisher, "_activate_locked", remove_published_artifact)
    monkeypatch.setattr(instance, "activation_publisher", lambda: publisher)
    calls = _count_recovery(monkeypatch)
    receipt = _activate(instance, proposal)
    assert receipt.status == "accepted" and len(calls) == 1
    assert instance.accepted_coordinate().git_oid == receipt.accepted_coordinate.git_oid
    assert instance._ledger.read_generation_note(receipt.accepted_coordinate.git_oid) is not None
    _assert_reopened_parity(instance)


@pytest.mark.parametrize("repair_fails", [False, True])
def test_post_cas_publication_failure_preserves_error_or_exposes_failed_repair(
    tmp_path, monkeypatch, repair_fails
):
    instance, owner = initialize_local(tmp_path)
    proposal = _proposal(instance, owner, "publication-failure")
    before = instance.accepted_coordinate()
    original_error = RuntimeError("publication interrupted after main CAS")

    def fail_publication(*args, **kwargs):
        raise original_error

    monkeypatch.setattr(activation_module, "publish_serving_manifest", fail_publication)
    calls = _count_recovery(monkeypatch)
    if repair_fails:

        def fail_recovery(*args, **kwargs):
            calls.append(kwargs)
            raise ValueError("recovery also failed")

        monkeypatch.setattr(instance_module, "recover_instance", fail_recovery)
    with pytest.raises(ValueError if repair_fails else RuntimeError) as caught:
        _activate(instance, proposal)
    if repair_fails:
        assert str(caught.value) == "recovery also failed"
        assert caught.value.__context__ is original_error
        assert instance.accepted_coordinate() == before
    else:
        assert caught.value is original_error
    assert len(calls) == 1 and instance._ledger.read_main() != before.git_oid
    if not repair_fails:
        assert instance.accepted_coordinate().git_oid == instance._ledger.read_main()
        _assert_reopened_parity(instance)


def test_callback_holds_activation_lock_and_refresh_cannot_overtake_it(tmp_path, monkeypatch):
    instance, owner = initialize_local(tmp_path)
    proposal = _proposal(instance, owner, "concurrent")
    publisher = instance.activation_publisher()
    original_activate = publisher.activate
    entered, release, refresh_started = (threading.Event() for _ in range(3))
    order, errors = [], []

    def wrapped_activate(bundle, projection, *, on_completed, **kwargs):
        def callback(result):
            descriptor = os.open(instance._ledger.path / "playbill-activation.lock", os.O_RDWR)
            try:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(descriptor)
            entered.set()
            assert release.wait(10), "callback was never released"
            on_completed(result)
            order.append("installed")

        return original_activate(bundle, projection, on_completed=callback, **kwargs)

    monkeypatch.setattr(publisher, "activate", wrapped_activate)
    monkeypatch.setattr(instance, "activation_publisher", lambda: publisher)
    original_refresh = instance._refresh_locked

    def refresh_locked(**kwargs):
        order.append("refresh")
        return original_refresh(**kwargs)

    monkeypatch.setattr(instance, "_refresh_locked", refresh_locked)

    def capture(fn):
        try:
            fn()
        except BaseException as exc:
            errors.append(exc)

    writer = threading.Thread(
        target=lambda: capture(lambda: _activate(instance, proposal)), daemon=True
    )

    def refresh():
        refresh_started.set()
        instance.refresh()

    reader = threading.Thread(target=lambda: capture(refresh), daemon=True)
    writer.start()
    try:
        assert entered.wait(10), "activation did not reach callback"
        reader.start()
        assert refresh_started.wait(10)
        acquired = instance._state_lock.acquire(blocking=False)
        if acquired:
            instance._state_lock.release()
        assert not acquired, "handoff released state exclusion before callback completed"
        assert order == []
    finally:
        release.set()
        writer.join(10)
        if reader.ident is not None:
            reader.join(10)
    assert not writer.is_alive() and not reader.is_alive()
    assert not errors and order == ["installed", "refresh"]
    _assert_reopened_parity(instance)
