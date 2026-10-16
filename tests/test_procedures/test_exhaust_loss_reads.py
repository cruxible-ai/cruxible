"""Best-effort payload loss is a typed read result and never permission to replay effects."""

from pathlib import Path

import pytest

from cruxible_client.contracts.errors import CasError
from cruxible_core.exhaust.payloads import UnavailablePayload, read_payload
from cruxible_core.procedures.run_index import ProcedureRunIndex
from cruxible_core.service.discovery.runs import run_rows
from cruxible_core.service.procedures.procedure_runs import (
    _records_for_run,
    load_playbill_procedure_run_grain,
    service_get_playbill_procedure_run,
)
from cruxible_core.service.proposals.proposal_egress import _fold_partition
from cruxible_core.storage.cas import BodyAccessContext
from tests.test_procedures.test_procedure_settlement_delivery import run_settle, settle_world

_ACCESS = BodyAccessContext(principal_id="test", can_read_body=True)


@pytest.mark.parametrize("damage", ["missing", "truncated"])
@pytest.mark.parametrize(
    "event", ["admission_bound", "node_fired", "terminal_egress", "attempt_finalized"]
)
def test_run_read_and_recovery_report_lost_payload(tmp_path: Path, event: str, damage: str):
    instance, root, line = settle_world(tmp_path)
    state = run_settle(instance, root, line)
    assert state.status == "succeeded", state.terminal
    records = _records_for_run(instance, state.run_id)
    stored = next(item for item in records if item.record.event_kind == event)
    bodies = instance.body_store()
    path = bodies._path(stored.record.payload_digest)
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"truncated")

    result = read_payload(bodies, stored.record.payload_digest, access=_ACCESS)
    assert result == UnavailablePayload(stored.record.payload_digest)
    with pytest.raises(CasError):
        bodies.read(stored.record.payload_digest, access=_ACCESS)
    reread = service_get_playbill_procedure_run(instance, run_id=state.run_id)
    assert reread.status == "operational_failed"
    assert reread.terminal.code == "replay_material_unavailable"
    assert reread.terminal.details["availability"] == "unavailable"
    assert reread.receipt is None and reread.result is None
    grain = load_playbill_procedure_run_grain(instance, run_id=state.run_id)
    assert not grain.finalized and not grain.node_verdicts
    rows, _ = run_rows(instance, limit=10)
    row = next(item for item in rows if item.run == state.run_id)
    if event in {"admission_bound", "attempt_finalized"}:
        assert row.status == "operational_failed"
    fold = _fold_partition(records, bodies=bodies, access=_ACCESS)[state.run_id]
    assert fold.unavailable
    assert fold.unresolved() == fold.resolved_unfinalized() == {}
    index = ProcedureRunIndex(tmp_path / "run-index.sqlite")
    index.rebuild_run(state.run_id, records, bodies=bodies)
    assert index.get(state.run_id).status == "failed"


def test_missing_admission_does_not_authorize_repeating_an_admitted_event(tmp_path):
    from datetime import timedelta

    from cruxible_client.contracts.line_dispatch import LineDispatchRequest, LineEvaluateRequest
    from cruxible_core.service.procedures.line_dispatch import (
        service_dispatch_line,
        service_evaluate_line,
    )
    from cruxible_core.service.procedures.procedure_runs import (
        _TRIGGER_CHAINS,
        _accepted_line_by_reference,
        _line_admissions,
        _trigger_admissions,
        _UnavailableLineAdmission,
        line_triggers,
    )
    from tests.test_procedures.test_manual_line_runs import manual_run
    from tests.test_procedures.test_procedure_source_runs import NOW, _actor, _run
    from tests.test_procedures.test_trigger_capture_inputs import event_from, world

    instance, root, line = world(tmp_path)
    actor = _actor(instance)
    event = event_from(_run(instance, root)[0])
    now = NOW + timedelta(seconds=2)
    service_evaluate_line(
        instance,
        line.identity.name,
        LineEvaluateRequest(since=NOW, until=now),
        actor=actor,
        now=now,
    )
    (dispatched,) = service_dispatch_line(
        instance, line.identity.name, LineDispatchRequest(), actor=actor, now=now, caller_rung=2
    ).items
    assert dispatched.status == "admitted"
    records = _records_for_run(instance, dispatched.run_id)
    admission = next(row for row in records if row.record.event_kind == "admission_bound")
    instance.body_store()._path(admission.record.payload_digest).unlink()
    repeated = manual_run(instance, line, at=now + timedelta(seconds=1), event=event)
    assert repeated.status == "operational_failed"
    assert repeated.run_id == dispatched.run_id
    assert repeated.terminal.code == "replay_material_unavailable"
    assert len(_records_for_run(instance, dispatched.run_id)) == len(records)
    accepted = _accepted_line_by_reference(
        instance, coordinate=instance.accepted_coordinate(), reference=line.identity.name
    )
    (admitted,) = _line_admissions(instance, accepted, occurrence_id=admission.record.occurrence_id)
    assert isinstance(admitted, _UnavailableLineAdmission)
    assert admitted.run_id == dispatched.run_id
    _TRIGGER_CHAINS.clear()
    for trigger in line_triggers(instance, accepted, coordinate=instance.accepted_coordinate()):
        (prior,) = _trigger_admissions(instance, accepted, trigger.trigger.identity)
        assert isinstance(prior, _UnavailableLineAdmission)
        (cached,) = _trigger_admissions(instance, accepted, trigger.trigger.identity)
        assert cached is prior


def test_missing_inbox_posting_does_not_break_surviving_disposition(tmp_path):
    from cruxible_client.contracts.line_inbox import InboxDismissed
    from cruxible_client.contracts.projection import AcceptedCoordinate
    from cruxible_core.exhaust.line_inbox import InboxUnavailable, LineInboxStore
    from cruxible_core.service.procedures.line_inbox import (
        service_get_inbox_item,
        service_list_inbox,
    )
    from tests.test_procedures.test_line_inbox_delivery import _entries, inbox_world
    from tests.test_procedures.test_manual_line_runs import manual_run
    from tests.test_procedures.test_procedure_source_runs import NOW, _actor

    instance, root, line = inbox_world(tmp_path, items={"items": [{"note": "review"}]})
    first = manual_run(instance, line, at=NOW, root=root)
    assert first.status == "succeeded"
    (entry,) = _entries(instance)
    store = LineInboxStore(instance)
    store.record_disposition(
        entry.envelope.item_id,
        InboxDismissed(actor_id="owner", decided_at=NOW, reason="seen"),
        actor=_actor(instance),
        now=NOW,
        coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
    )
    (posting, disposition) = store._journal(create=False).select_records(
        store.stream, partition_id="inbox"
    )
    path = instance.body_store()._path(posting.record.payload_digest)
    original = path.read_bytes()
    path.unlink()
    store.path.unlink()  # A rebuildable index must tolerate the lost posting.
    for read in (
        lambda: LineInboxStore(instance).unread_by_line(),
        lambda: service_list_inbox(instance, limit=1),
        lambda: service_get_inbox_item(instance, item=entry.envelope.item_id),
        lambda: store.record_disposition(
            entry.envelope.item_id,
            InboxDismissed(actor_id="owner", decided_at=NOW, reason="again"),
            actor=_actor(instance),
            now=NOW,
            coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        ),
    ):
        with pytest.raises(InboxUnavailable) as refused:
            read()
        assert refused.value.context["availability"] == "unavailable"
        assert refused.value.payload.digest == posting.record.payload_digest
    assert len(store._journal(create=False).select_records(store.stream, partition_id="inbox")) == 2
    path.write_bytes(original)
    store.path.unlink()  # Explicit repair replays both surviving transitions.
    assert LineInboxStore(instance).unread_by_line() == ()
    assert service_get_inbox_item(instance, item=entry.envelope.item_id).item.status == "dismissed"
