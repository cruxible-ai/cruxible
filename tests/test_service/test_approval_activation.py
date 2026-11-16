"""The approval that completes a candidate activates it in the same call."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.documents import render_document
from cruxible_core.governance.keys import generate_client_principal_key
from cruxible_core.service.authoring.documents import (
    service_activate_playbill_proposal,
    service_propose_playbill_principal_change,
    service_submit_playbill_approval,
)
from tests.core_support._support import client_material, initialize_local
from tests.test_ledger.test_activation import _sign
from tests.test_proposals.test_approval_policy import _submit_tree
from tests.test_proposals.test_proposals import DOCUMENT_PATH, _shell


def _document_proposal(instance, *, name: str, text: bytes = b"# Approve me\n"):  # type: ignore[no-untyped-def]
    body = instance.store_document_body(text)
    result = _submit_tree(
        instance,
        {
            **instance.tree_at(instance.accepted_coordinate().git_oid),
            DOCUMENT_PATH: render_document(_shell(body.digest)),
        },
        name=name,
    )
    assert result.candidate is not None
    return result


def _approve(instance, result, material, **options):  # type: ignore[no-untyped-def]
    assert result.candidate is not None
    signed = _sign(
        material,
        result.candidate.candidate_digest,
        result.candidate.candidate.parent_semantic_root,
    )
    return service_submit_playbill_approval(
        instance,
        proposal_id=result.admission.proposal_id,
        attestation=signed.attestation,
        authenticated_submitter=material.principal.principal_id,
        **options,
    )


def test_completing_approval_activates_as_the_submitter(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    result = _document_proposal(instance, name="approve-activates")
    before = instance.accepted_coordinate()

    receipt = _approve(
        instance,
        result,
        client_material(tmp_path, instance),
        activate_when_complete=True,
        may_activate=True,
    )

    assert receipt.not_activated is None and receipt.approvals_remaining == 0
    assert receipt.activation is not None
    assert receipt.activation.status == "accepted"
    assert receipt.activation.activated_by == "reviewer"
    assert instance.accepted_coordinate() != before


def test_held_or_unpermitted_approval_signs_only(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    reviewer = client_material(tmp_path, instance)
    held = _document_proposal(instance, name="held-approval")
    head = instance.accepted_coordinate()

    receipt = _approve(instance, held, reviewer, activate_when_complete=False, may_activate=True)

    assert receipt.activation is None and receipt.not_activated == "held"
    assert instance.accepted_coordinate() == head
    activated = service_activate_playbill_proposal(
        instance, proposal_id=held.admission.proposal_id, activated_by="owner"
    )
    assert activated.status == "accepted"

    # A fresh instance: the held document is now accepted in the first.
    other_root = tmp_path / "other"
    other_root.mkdir()
    other, _other_owner = initialize_local(other_root)
    unpermitted = _document_proposal(other, name="unpermitted")
    head = other.accepted_coordinate()
    receipt = _approve(
        other,
        unpermitted,
        client_material(other_root, other),
        activate_when_complete=True,
        may_activate=False,
    )
    assert receipt.activation is None and receipt.not_activated == "activation_not_permitted"
    assert other.accepted_coordinate() == head


def test_a_stale_candidate_refuses_approval_naming_the_changed_path(tmp_path: Path) -> None:
    """Both proposals write the one Document path: the first's acceptance overlaps the second.

    A stale candidate is never approvable (readmit-by-change-set-overlap-1024):
    the approval refuses, names what changed since its base, and records no
    signature.
    """

    import pytest

    from cruxible_client.contracts.errors import ProposalStale

    instance, _owner = initialize_local(tmp_path)
    reviewer = client_material(tmp_path, instance)
    first = _document_proposal(instance, name="first-of-two")
    second = _document_proposal(instance, name="second-of-two", text=b"# Other\n")
    assert _approve(
        instance, first, reviewer, activate_when_complete=True, may_activate=True
    ).activation

    with pytest.raises(ProposalStale) as refused:
        _approve(instance, second, reviewer, activate_when_complete=True, may_activate=True)

    assert refused.value.reason == "changed_since_base"
    assert DOCUMENT_PATH in str(refused.value)
    assert instance.proposal_evidence().read_approvals(second.candidate.candidate_digest) == ()


def test_a_corrupt_trigger_journal_never_fails_the_committed_activation(tmp_path: Path) -> None:
    """A kit-resume lookup fault after acceptance is isolated from the receipt.

    ``resume_kits_after_activation``'s initial waiting-row read opens and reads
    the trigger journal on first use; an unreadable or corrupt file there must
    not escape into the activation that already committed (it only defers the
    kit resume, logged, with the pending record kept).
    """
    from cruxible_core.triggers.journal import journal_path

    instance, _owner = initialize_local(tmp_path)
    result = _document_proposal(instance, name="activates-despite-corrupt-journal")
    path = journal_path(instance)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not a database")

    receipt = _approve(
        instance,
        result,
        client_material(tmp_path, instance),
        activate_when_complete=True,
        may_activate=True,
    )

    assert receipt.not_activated is None
    assert receipt.activation is not None
    assert receipt.activation.status == "accepted"


def test_lifecycle_candidate_counts_its_actors_own_signature(tmp_path: Path) -> None:
    instance, owner = initialize_local(tmp_path)
    newcomer = generate_client_principal_key(
        tmp_path / "newcomer-keys",
        principal_id="newcomer",
        kind="ordinary",
        forbidden_roots=(instance.root,),
    )
    registration = service_propose_playbill_principal_change(
        instance,
        principal=newcomer.principal,
        actor_id="owner",
        proposal_name="register-newcomer",
        timestamp="2026-08-16T17:02:00.000000Z",
    ).proposal
    head = instance.accepted_coordinate()

    voluntary = _approve(
        instance,
        registration,
        client_material(tmp_path, instance),
        activate_when_complete=True,
        may_activate=True,
    )
    assert voluntary.not_activated == "approvals_remaining"
    assert voluntary.approvals_remaining == 1
    # The outstanding signature is the proposer's own, and the receipt says so.
    assert voluntary.required_signers == ("owner",)
    assert voluntary.eligible_signers == ()
    assert instance.accepted_coordinate() == head

    completing = _approve(
        instance, registration, owner, activate_when_complete=True, may_activate=True
    )
    assert completing.activation is not None
    assert completing.activation.status == "accepted"


def test_activating_an_unsigned_lifecycle_change_names_its_proposer(tmp_path: Path) -> None:
    """Round 2: the approvals-missing refusal named other principals for a principal
    change, whose missing signature is its proposer's own."""

    from cruxible_client.contracts.errors import ApprovalRequirementUnmet

    instance, _owner = initialize_local(tmp_path)
    newcomer = generate_client_principal_key(
        tmp_path / "newcomer-keys",
        principal_id="newcomer",
        kind="ordinary",
        forbidden_roots=(instance.root,),
    )
    registration = service_propose_playbill_principal_change(
        instance,
        principal=newcomer.principal,
        actor_id="owner",
        proposal_name="register-newcomer",
        timestamp="2026-08-16T17:02:00.000000Z",
    ).proposal

    with pytest.raises(ApprovalRequirementUnmet) as refused:
        service_activate_playbill_proposal(
            instance, proposal_id=registration.admission.proposal_id, activated_by="owner"
        )

    assert refused.value.required_signers == ("owner",)
    assert refused.value.remaining == 1
    assert "owner must sign (a principal change needs its proposer's own signature)" in str(
        refused.value
    )
    assert refused.value.repair.arguments == {
        "proposal_id": refused.value.proposal_id,
        "signer_id": "owner",
    }


def test_a_compiler_upgrades_one_signature_may_be_its_authors() -> None:
    """With no independent requirement, a compiler upgrade needs one signature of any
    active ordinary principal, its author's included."""

    from types import SimpleNamespace

    from cruxible_core.service.authoring.documents import outstanding_approvals

    registry = SimpleNamespace(
        principals=[
            SimpleNamespace(principal_id=name, status="active", kind=kind)
            for name, kind in (("owner", "ordinary"), ("reviewer", "ordinary"), ("d", "daemon"))
        ]
    )
    upgrade = SimpleNamespace(
        approval_requirements=(),
        members=(SimpleNamespace(artifact_kind="compiler-upgrade"),),
    )

    outstanding = outstanding_approvals(
        (),
        upgrade,  # type: ignore[arg-type]
        creator="owner",
        principals=registry,  # type: ignore[arg-type]
        principal_lifecycle=False,
    )

    assert (outstanding.remaining, outstanding.independent) == (1, False)
    assert outstanding.eligible_signers == ("owner", "reviewer")


@pytest.mark.parametrize("may_activate", [False, True])
def test_a_provider_move_below_graph_write_proposes_and_does_not_activate(
    tmp_path: Path, may_activate: bool
) -> None:
    """Round 3: provider move (governed_write) activated whatever the policy let
    through; a request whose tier may not activate leaves it proposed."""

    from cruxible_core.service.change_preview import change_scope
    from cruxible_core.service.procedures.provider_builds import _submit

    instance, _owner = initialize_local(tmp_path)
    body = instance.store_document_body(b"# Moved\n")
    head = instance.accepted_coordinate()
    with change_scope(
        instance,
        dry_run=False,
        at=head.git_oid,
        kind="derived",
        operation="cruxible.provider.move",
        describe="moving a provider",
    ) as mode:
        status, proposal_id, approval, detail = _submit(
            instance,
            mode,
            {DOCUMENT_PATH: render_document(_shell(body.digest))},
            actor_id="owner",
            timestamp="2026-08-16T17:00:00.000000Z",
            may_activate=may_activate,
        )

    if may_activate:
        assert status == "accepted"
        assert instance.accepted_coordinate() != head
    else:
        assert (status, approval) == ("proposed", False)
        assert detail is not None
        assert f"someone at graph_write activates proposal {proposal_id}" in detail
        assert instance.accepted_coordinate() == head


class _CountingRegistry:
    """A principal registry that counts how many principal records were visited."""

    def __init__(self, size: int) -> None:
        from types import SimpleNamespace

        self.visited = 0
        self._records = [
            SimpleNamespace(principal_id=f"p{index:04d}", status="active", kind="ordinary")
            for index in range(size)
        ]

    @property
    def principals(self):  # type: ignore[no-untyped-def]
        for record in self._records:
            self.visited += 1
            yield record


@pytest.mark.parametrize("size", [10, 1000])
def test_nothing_outstanding_visits_no_principal(size: int) -> None:
    """Round 3 (cost): an ordinary candidate with nothing outstanding used to build and
    sort every ordinary principal; signers are listed only when a refusal or receipt
    names them."""

    from types import SimpleNamespace

    from cruxible_core.service.authoring.documents import outstanding_approvals

    registry = _CountingRegistry(size)
    ordinary = SimpleNamespace(
        approval_requirements=(), members=(SimpleNamespace(artifact_kind="document"),)
    )

    outstanding = outstanding_approvals(
        (),
        ordinary,  # type: ignore[arg-type]
        creator="owner",
        principals=registry,  # type: ignore[arg-type]
        principal_lifecycle=False,
    )

    assert outstanding.remaining == 0
    assert registry.visited == 0


def test_a_self_approved_write_lands_without_enumerating_principals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.service.authoring import documents
    from tests.core_support._write_support import KIND, seed_write_surface
    from tests.test_service.test_write_verbs import _set, _write

    instance, _owner = seed_write_surface(tmp_path)
    real = documents.outstanding_approvals
    visits: list[int] = []

    def counted(submissions, candidate, *, principals, **options):  # type: ignore[no-untyped-def]
        registry = _CountingRegistry(0)
        registry._records = list(principals.principals)
        result = real(submissions, candidate, principals=registry, **options)  # type: ignore[arg-type]
        visits.append(registry.visited)
        return result

    monkeypatch.setattr(documents, "outstanding_approvals", counted)
    outcome = _write(instance, _set(f"{KIND}/wi-1", "title", "self-approved"))

    assert outcome.status == "accepted", outcome
    assert visits and visits == [0] * len(visits)
