"""Isolated proposal-door publication ordering, without accepted-world setup."""

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import Mock

import pytest

from cruxible_client.contracts.errors import ProposalIntegrityError
from cruxible_core.compiler.compiler import PC_HR_COMPILER
from cruxible_core.indexes.projection import AcceptedProjectionCoordinate
from cruxible_core.proposals import proposals


@pytest.mark.parametrize("move_under_lock", [False, True])
def test_publication_integrity_check_finishes_before_activation_unlock(
    monkeypatch: pytest.MonkeyPatch, move_under_lock: bool
) -> None:
    coordinate = AcceptedProjectionCoordinate(
        instance_id="inst_publication_lock",
        repository_path="/tmp/publication-lock",
        git_object_format="sha1",
        git_oid="11" * 20,
        semantic_root="sha256:" + "22" * 32,
        generation_root="sha256:" + "33" * 32,
        compiler=PC_HR_COMPILER,
    )
    head = coordinate.git_oid
    review_locked = False
    activation_locked = False
    mirrored = False
    writes: list[str] = []

    @contextmanager
    def review_lock() -> Iterator[None]:
        nonlocal review_locked
        review_locked = True
        try:
            yield
        finally:
            review_locked = False

    @contextmanager
    def activation_lock() -> Iterator[None]:
        nonlocal activation_locked, head
        assert review_locked
        activation_locked = True
        try:
            yield
        finally:
            activation_locked = False
            # A competing acceptance wins immediately after this submission's
            # critical section. Its new head must not invalidate our result.
            head = "44" * 20

    def publish_notes(*_args: object, **_kwargs: object) -> None:
        nonlocal head
        assert review_locked and activation_locked
        assert writes == ["evaluation", "admission"]
        if move_under_lock:
            # A transport violating the lock contract remains an integrity error.
            head = "55" * 20

    def mirror() -> None:
        nonlocal mirrored
        assert not review_locked and not activation_locked
        mirrored = True

    transport = Mock()
    transport.object_format.return_value = "sha1"
    transport.read_main.side_effect = lambda: head
    transport.read_tree.return_value = {}
    transport.read_proposal_ref.return_value = None
    transport.create_proposal_commit.return_value = ("66" * 20, "77" * 20)
    transport.activation_lock.side_effect = activation_lock
    evidence = Mock()
    evidence.write_evaluation.side_effect = lambda _record: writes.append("evaluation")
    evidence.write_admission.side_effect = lambda _record: writes.append("admission")
    notes = Mock()
    notes.validate_and_snapshot.return_value = {}
    notes.publish.side_effect = publish_notes
    monkeypatch.setattr(proposals, "principal_registry_from_tree", Mock())
    monkeypatch.setattr(proposals, "validate_proposal_tree", Mock(return_value={}))
    monkeypatch.setattr(
        proposals,
        "evaluate_proposal_tree",
        Mock(
            return_value=proposals.CandidateEvaluation(
                tree={}, candidate=None, diagnostics=(), rebased=False
            )
        ),
    )
    service = proposals.ProposalService(
        transport,
        accepted=coordinate,
        bodies=Mock(),
        evidence=evidence,
        review_projection_lock=review_lock,
        note_index_provider=lambda: notes,
        ledger_publisher=mirror,
    )
    request = proposals.ProposalAdmissionRequest(
        target_ref="refs/proposals/owner/locking", proposed_base_oid=coordinate.git_oid
    )
    if move_under_lock:
        with pytest.raises(ProposalIntegrityError, match="changed accepted main"):
            service.submit(
                actor=proposals.AuthenticatedActor(actor_id="owner"),
                request=request,
                candidate_tree={},
                timestamp="2026-09-07T12:00:00.000000Z",
            )
        assert not mirrored
    else:
        result = service.submit(
            actor=proposals.AuthenticatedActor(actor_id="owner"),
            request=request,
            candidate_tree={},
            timestamp="2026-09-07T12:00:00.000000Z",
        )
        assert result.admission.candidate_commit_oid == "66" * 20
        assert mirrored
    assert head == "44" * 20
    assert writes == ["evaluation", "admission"]
