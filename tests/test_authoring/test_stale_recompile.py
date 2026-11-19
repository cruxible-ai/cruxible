"""Recompilation agrees with fresh lowering at the head and never lowers at the old base."""

from pathlib import Path

import pytest

from cruxible_client.contracts.errors import ProposalStale
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.authoring.preflight import compute_preflight
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalResult
from cruxible_core.service.change_preview import preview_guards
from cruxible_core.service.proposals.proposals import _proposal_result
from cruxible_core.service.proposals.recompile import recompile_proposal
from tests.core_support._support import initialize_local
from tests.test_authoring.test_authoring_change_set_intents import (
    TIMESTAMP,
    _accept,
    _change_set,
    _claim,
    _coordinator,
    _create,
    _seed,
)


def test_recompile_matches_fresh_at_head_and_preserves_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, owner = initialize_local(tmp_path)
    _seed(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    intent = coordinator.create(
        actor=actor, payload=_change_set(_claim()), canonical_timestamp=TIMESTAMP
    ).intent
    submitted = coordinator.submit(intent.intent_id, actor=actor)
    assert submitted.status.proposal_id is not None
    source = _proposal_result(instance, submitted.status.proposal_id)
    moving = coordinator.create(
        actor=actor, payload=_change_set(_create("elsewhere")), canonical_timestamp=TIMESTAMP
    ).intent
    _accept(instance, owner, coordinator, moving.intent_id, actor)
    head = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    # Recompilation must never consult the stale lowering base. Reference pins
    # (absent in this case) may independently anchor to historical coordinates.
    original = instance.immutable_tree_at

    def tree_at(oid: str):  # type: ignore[no-untyped-def]
        assert oid != intent.base_coordinate.git_oid
        return original(oid)

    monkeypatch.setattr(instance, "immutable_tree_at", tree_at)
    with preview_guards(True):
        oracle = compute_preflight(
            instance, intent=intent.model_copy(update={"base_coordinate": head}), actor=actor
        )
    from cruxible_core.authoring.store import AuthoringIntentStore

    def population(*args: object, **kwargs: object) -> None:
        pytest.fail("recompilation enumerated the intent population")

    with monkeypatch.context() as bounded:
        for name in ("_intent_directories", "_finished_receipts", "_recover_creating_directories"):
            bounded.setattr(AuthoringIntentStore, name, population)
        result = recompile_proposal(instance, source)
    assert result is not None and isinstance(result.proposal, ProposalResult)
    assert oracle.evaluation is not None
    assert result.proposal.candidate is not None and oracle.evaluation.candidate is not None
    assert result.proposal.candidate.members == oracle.evaluation.candidate.members
    assert result.proposal.admission.admitted_at == intent.canonical_timestamp
    assert result.proposal.admission.readmits is not None
    assert result.proposal.admission.readmits.source_proposal_id == source.admission.proposal_id
    withdrawal = instance.proposal_evidence().read_withdrawal(source.admission.proposal_id)
    assert (
        withdrawal is not None and withdrawal.superseded_by == result.proposal.admission.proposal_id
    )
    assert (
        instance.proposal_evidence().read_admission(source.admission.proposal_id)
        == source.admission
    )


@pytest.mark.parametrize("failure", ("missing", "invalid", "disk_full", "permission"))
def test_unavailable_input_is_stale_but_storage_faults_propagate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    import errno

    from pydantic import ValidationError

    from cruxible_client.contracts.artifacts import ArtifactIdentity
    from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.authoring.store import AuthoringIntentStoreError

    instance, owner = initialize_local(tmp_path)
    _seed(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    intent = coordinator.create(
        actor=actor, payload=_change_set(_claim()), canonical_timestamp=TIMESTAMP
    ).intent
    submitted = coordinator.submit(intent.intent_id, actor=actor)
    assert submitted.status.proposal_id is not None
    source = _proposal_result(instance, submitted.status.proposal_id)

    error: Exception
    if failure == "missing":
        error = AuthoringIntentStoreError("pruned")
    elif failure == "invalid":
        try:
            ArtifactIdentity(kind="invalid kind", name="example")
        except ValidationError as exc:
            error = exc
    else:
        error = OSError(errno.ENOSPC if failure == "disk_full" else errno.EACCES, failure)

    def missing(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(
        type(AuthoringIntentCoordinator.for_instance(instance).store), "get", missing
    )
    before = instance.proposal_evidence().list_admissions()
    if isinstance(error, OSError):
        with pytest.raises(OSError) as fault:
            recompile_proposal(instance, source)
        assert fault.value is error
    else:
        with pytest.raises(ProposalStale) as refusal:
            recompile_proposal(instance, source)
        assert refusal.value.reason == "input_unavailable"
    assert instance.proposal_evidence().list_admissions() == before
    assert instance.proposal_evidence().read_withdrawal(source.admission.proposal_id) is None
