"""Reviewer judgments use the served measurement book, without interpreting reasons."""

from datetime import timedelta

import pytest

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.get_reads import GetRequest
from cruxible_client.contracts.procedures.artifacts import (
    AcceptedProcedure,
    procedure_artifact_digest,
    procedure_path,
)
from cruxible_client.contracts.procedures.dispositions import ProcedureResolutionDispositionRequest
from cruxible_client.contracts.procedures.graph import compute_procedure_definition_digest
from cruxible_client.contracts.procedures.readings import ProcedureReadingsRequest
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.procedures.resolution import (
    ProcedureResolutionBook,
    resolution_book_at_head,
    resolution_contract_partition_id,
)
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.procedures.measurements import (
    _journal,
    measurement_activation_basis,
    service_list_playbill_procedure_readings,
)
from cruxible_core.service.procedures.resolution_dispositions import (
    ResolutionDispositionRefused,
    service_dispose_procedure_resolution,
)
from cruxible_core.storage.cas import BodyAccessContext
from tests.test_procedures.test_procedure_measurement_readings import (
    OBSERVE_AT,
    RECORD_AT,
    _actor,
    _measure,
    _world,
)
from tests.test_procedures.test_procedure_owned_contracts import _activate_procedure


@pytest.fixture
def measured(tmp_path):
    instance, owner, procedure = _world(tmp_path)
    first = _measure(instance, procedure, names=("rows-present",)).rows[0]
    assert first.resolution is not None
    return instance, owner, procedure, first.resolution


def _dispose(
    instance,
    procedure,
    resolution,
    verdict="upheld",
    reason="Checked the evidence.",
    actor="reviewer",
    at=None,
):
    return service_dispose_procedure_resolution(
        instance,
        name=procedure.identity.name,
        request=ProcedureResolutionDispositionRequest(
            resolution_id=resolution.resolution_id, verdict=verdict, reason=reason, at=at
        ),
        actor_context=_actor(instance, actor),
        recorded_at=RECORD_AT + timedelta(minutes=2),
    )


def _book(instance, procedure):
    basis = measurement_activation_basis(
        instance,
        accepted=AcceptedProcedure(
            path=procedure_path(procedure.identity.name),
            procedure=procedure,
            artifact_digest=procedure_artifact_digest(procedure).tagged,
        ),
        observation=instance.accepted_coordinate(),
    )
    activation = next(a for a in basis.activations if a.measurement_name == "rows-present")
    journal, stream = _journal(instance)
    view, head = resolution_book_at_head(journal, stream, activation, bodies=instance.body_store())
    return activation, journal, stream, view, head


def test_uphold_retry_overturn_get_history_and_remeasure_equal_the_fold(measured):
    instance, _, procedure, first = measured
    upheld = _dispose(instance, procedure, first)
    assert upheld.written_now and upheld.standing_resolution_id == first.resolution_id
    activation, journal, stream, view, head = _book(instance, procedure)
    retry = _dispose(instance, procedure, first)
    assert not retry.written_now and retry.disposition == upheld.disposition
    assert _book(instance, procedure)[4] == head
    assert view.last_disposition(first.resolution_id).reviewer_actor_context.actor_id == "reviewer"

    standing = _measure(instance, procedure, names=("rows-present",)).rows[0].resolution
    assert standing.resolution_id == first.resolution_id
    assert standing.last_disposition == upheld.disposition
    overturned = _dispose(instance, procedure, first, "overturned", "The evidence was misleading.")
    assert overturned.standing_resolution_id is None
    assert overturned.disposition.sequence == 2
    assert not _dispose(
        instance, procedure, first, "overturned", "The evidence was misleading."
    ).written_now

    listed = service_list_playbill_procedure_readings(
        instance,
        name=procedure.identity.name,
        request=ProcedureReadingsRequest(measurement_names=("rows-present",)),
        evaluation_time=RECORD_AT,
    )
    assert listed.contracts[0].resolution is None
    assert listed.contracts[0].status == "open"
    assert listed.contracts[0].review.last_disposition == overturned.disposition
    access = BodyAccessContext(principal_id="reviewer", can_read_body=True)
    for detail in ("summary", "history"):
        result = service_playbill_get(
            instance,
            request=GetRequest(
                ref=f"Procedure:{procedure.identity.name}",
                detail=detail,
                surface="cli",
                limit=1 if detail == "history" else None,
            ),
            access=access,
        )
        rows = (result.card if detail == "summary" else result.history).measurement_reviews
        row = next(r for r in rows if r.measurement_name == "rows-present")
        assert (
            row.latest_resolution_id == first.resolution_id and row.standing_resolution_id is None
        )
        assert row.last_disposition == overturned.disposition
        if detail == "history":
            assert row.history == (overturned.disposition,) and row.history_truncated
            assert row.history_total == 2

    activation, journal, stream, view, _ = _book(instance, procedure)
    replay = ProcedureResolutionBook((activation,))
    replay.replay(
        journal.all_records(stream, resolution_contract_partition_id(activation)),
        bodies=instance.body_store(),
    )
    assert (
        view.latest_non_overturned(activation.contract_id)
        == replay.latest_non_overturned(activation.contract_id)
        is None
    )
    assert (
        view.last_disposition(first.resolution_id) == replay.dispositions[first.resolution_id][-1]
    )
    assert tuple(reversed(view.review_history(activation.contract_id, limit=10))) == tuple(
        replay.dispositions[first.resolution_id]
    )
    # Detached values cannot corrupt the kept fold.
    view.last_disposition(first.resolution_id).reviewer_actor_context.actor_id = "tampered"
    assert view.last_disposition(first.resolution_id).reviewer_actor_context.actor_id == "reviewer"

    second = (
        _measure(
            instance,
            procedure,
            names=("rows-present",),
            at=OBSERVE_AT + timedelta(minutes=5),
            recorded=RECORD_AT + timedelta(minutes=5),
        )
        .rows[0]
        .resolution
    )
    assert second.sequence == 2 and second.resolution_id != first.resolution_id
    with pytest.raises(ResolutionDispositionRefused) as refused:
        _dispose(instance, procedure, first, "upheld")
    assert refused.value.error_code == "cruxible.resolution.not_latest"
    assert (
        _book(instance, procedure)[3].latest_non_overturned(activation.contract_id).resolution_id
        == second.resolution_id
    )


@pytest.mark.parametrize(
    "reason,actor", [("A different reason.", "reviewer"), ("Checked the evidence.", "owner")]
)
def test_same_verdict_with_different_meaning_refuses(measured, reason, actor):
    instance, _, procedure, first = measured
    _dispose(instance, procedure, first)
    with pytest.raises(ResolutionDispositionRefused) as refused:
        _dispose(instance, procedure, first, reason=reason, actor=actor)
    assert refused.value.error_code == "cruxible.resolution.already_disposed"


def test_unknown_and_unavailable_books_refuse(measured):
    instance, _, procedure, first = measured
    with pytest.raises(ResolutionDispositionRefused) as refused:
        _dispose(instance, procedure, first.model_copy(update={"resolution_id": "RSR-" + "0" * 32}))
    assert refused.value.error_code == "cruxible.resolution.unknown"
    _, _, _, view, _ = _book(instance, procedure)
    record = view.resolution_record(first.resolution_id)
    assert instance.body_store().erase(record.record.payload_digest)
    with pytest.raises(ResolutionDispositionRefused) as refused:
        _dispose(instance, procedure, first)
    assert refused.value.error_code == "cruxible.resolution.unavailable"


def test_replaced_contract_refuses_but_its_history_remains_readable(measured):
    instance, owner, procedure, first = measured
    old = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    definition = procedure.definition.model_copy(
        update={
            "budget": procedure.definition.budget.model_copy(
                update={"max_items": procedure.definition.budget.max_items - 1}
            )
        }
    )
    changed = procedure.model_copy(
        update={
            "definition": definition,
            "definition_digest": compute_procedure_definition_digest(definition).tagged,
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=procedure_artifact_digest(procedure).tagged
            ),
        }
    )
    _activate_procedure(
        instance, owner, changed, sequence=5, timestamp="2026-08-24T17:00:00.000000Z"
    )
    with pytest.raises(ResolutionDispositionRefused) as refused:
        _dispose(instance, procedure, first, at=old)
    assert refused.value.error_code == "cruxible.resolution.contract_closed"
    historical = service_playbill_get(
        instance,
        request=GetRequest(ref=f"Procedure:{procedure.identity.name}", at=old.git_oid),
        access=BodyAccessContext(principal_id="reviewer", can_read_body=True),
    )
    row = next(
        row for row in historical.card.measurement_reviews if row.measurement_name == "rows-present"
    )
    assert row.standing_resolution_id == first.resolution_id


def test_retired_contract_refuses(measured):
    instance, owner, procedure, first = measured
    retired = procedure.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                state="retired", predecessor_digest=procedure_artifact_digest(procedure).tagged
            )
        }
    )
    _activate_procedure(
        instance, owner, retired, sequence=5, timestamp="2026-08-24T17:00:00.000000Z"
    )
    with pytest.raises(ResolutionDispositionRefused) as refused:
        _dispose(instance, procedure, first)
    assert refused.value.error_code == "cruxible.resolution.contract_closed"


def test_overturn_does_not_extend_an_expired_measurement_window(measured):
    instance, _, procedure, first = measured
    _dispose(instance, procedure, first, "overturned")
    row = _measure(
        instance,
        procedure,
        names=("rows-present",),
        at=OBSERVE_AT + timedelta(days=100),
        recorded=RECORD_AT + timedelta(days=100),
    ).rows[0]
    assert row.status == "expired" and row.resolution is None


def test_compare_and_set_refuses_a_competing_judgment(measured, monkeypatch):
    from cruxible_client.contracts.errors import JournalConflictError
    from cruxible_core.service.procedures import resolution_dispositions as module

    instance, _, procedure, first = measured

    def conflict(*args, **kwargs):
        assert kwargs["expected_head"] is not None
        raise JournalConflictError("competing review")

    monkeypatch.setattr(module, "append_resolution_disposition", conflict)
    with pytest.raises(ResolutionDispositionRefused) as refused:
        _dispose(instance, procedure, first)
    assert refused.value.error_code == "cruxible.resolution.conflict"


@pytest.mark.parametrize("operation", ["summary", "history", "retry"])
def test_warm_get_and_exact_retry_do_flat_work_at_two_review_history_sizes(
    tmp_path, monkeypatch, operation
):
    from cruxible_core.procedures import resolution as module
    from cruxible_core.storage.cas import ContentAddressedBodyStore

    def work(size):
        instance, _, procedure = _world(tmp_path / str(size))
        first = _measure(instance, procedure, names=("rows-present",)).rows[0].resolution
        for sequence in range(size):
            _dispose(instance, procedure, first, "overturned" if sequence % 2 else "upheld")

        def request():
            if operation == "retry":
                return _dispose(instance, procedure, first, "overturned")
            return service_playbill_get(
                instance,
                request=GetRequest(
                    ref=f"Procedure:{procedure.identity.name}",
                    detail=operation,
                    limit=1 if operation == "history" else None,
                ),
                access=BodyAccessContext(principal_id="reviewer", can_read_body=True),
            )

        request()  # Consume outstanding completion/suffix changes before measuring.
        counts = {"stat": 0, "read": 0, "detach": 0}
        identity = ContentAddressedBodyStore.file_identity
        read = ContentAddressedBodyStore.read
        loads = module.pickle.loads

        def stat(*args, **kwargs):
            counts["stat"] += 1
            return identity(*args, **kwargs)

        def body_read(*args, **kwargs):
            counts["read"] += 1
            return read(*args, **kwargs)

        def detach(*args, **kwargs):
            counts["detach"] += 1
            return loads(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(ContentAddressedBodyStore, "file_identity", stat)
            patch.setattr(ContentAddressedBodyStore, "read", body_read)
            patch.setattr(module.pickle, "loads", detach)
            result = request()
        if operation == "retry":
            assert result.written_now is False
        else:
            rows = (result.card if operation == "summary" else result.history).measurement_reviews
            reviewed = next(row for row in rows if row.measurement_name == "rows-present")
            assert reviewed.history_total == size
        assert counts["stat"] == 0
        return counts

    assert work(2) == work(8)


@pytest.mark.parametrize("change", ["retire", "replace"])
def test_dispose_revalidates_contract_changed_after_writer_fence(measured, monkeypatch, change):
    from cruxible_core.service.procedures import resolution_dispositions as module

    instance, owner, procedure, first = measured
    activation, journal, stream, _, before = _book(instance, procedure)
    acquire = module._FencedWriter.acquire

    def close_after_fence(writer, *args):
        acquire(writer, *args)
        definition = procedure.definition
        if change == "replace":
            definition = definition.model_copy(
                update={
                    "budget": definition.budget.model_copy(
                        update={"max_items": definition.budget.max_items - 1}
                    )
                }
            )
        changed = procedure.model_copy(
            update={
                "definition": definition,
                "definition_digest": compute_procedure_definition_digest(definition).tagged,
                "lifecycle": ArtifactLifecycle(
                    state="retired" if change == "retire" else "live",
                    predecessor_digest=procedure_artifact_digest(procedure).tagged,
                ),
            }
        )
        _activate_procedure(
            instance, owner, changed, sequence=5, timestamp="2026-08-24T17:00:00.000000Z"
        )

    monkeypatch.setattr(module._FencedWriter, "acquire", close_after_fence)
    with pytest.raises(ResolutionDispositionRefused) as refused:
        _dispose(instance, procedure, first)
    assert refused.value.error_code == "cruxible.resolution.contract_closed"
    assert refused.value.http_status == 409
    assert journal.read_head(stream, resolution_contract_partition_id(activation)) == before


def test_disposition_append_keeps_the_accepted_head_hold(measured, monkeypatch):
    from contextlib import contextmanager

    from cruxible_core.service.procedures import resolution_dispositions as module

    instance, _, procedure, first = measured
    hold = instance.holding_accepted_head
    append = module.append_resolution_disposition
    holding = False

    @contextmanager
    def tracked_hold():
        nonlocal holding
        with hold() as head:
            holding = True
            try:
                yield head
            finally:
                holding = False

    def append_while_held(*args, **kwargs):
        assert holding
        return append(*args, **kwargs)

    monkeypatch.setattr(instance, "holding_accepted_head", tracked_hold)
    monkeypatch.setattr(module, "append_resolution_disposition", append_while_held)
    assert _dispose(instance, procedure, first).written_now
