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
