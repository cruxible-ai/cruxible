"""Regression (review P2-2): live operational state beside an older ``at`` is announced."""

from __future__ import annotations

from typing import Any

from cruxible_client.contracts.get_reads import GetRequest
from cruxible_client.contracts.operational_reads import GetLineCard
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.discovery.orient import service_playbill_orient
from cruxible_core.storage.cas import BodyAccessContext
from tests.test_service.test_operational_credentials import credential_world  # noqa: F401

_ACCESS = BodyAccessContext(principal_id="reader", can_read_body=False)


def _get(instance: Any, ref: str, **fields: Any):  # type: ignore[no-untyped-def]
    return service_playbill_get(instance, request=GetRequest(ref=ref, **fields), access=_ACCESS)


def test_live_state_beside_a_historical_at_is_announced(credential_world) -> None:  # noqa: F811  # type: ignore[no-untyped-def]
    instance, line, run_id, when = credential_world
    history = instance.accepted_history()
    head, first = history[-1], history[0]

    runs = service_playbill_orient(instance, section="runs", at=first.oid)
    assert runs.runs and runs.generation == first.sequence
    assert runs.live is not None
    assert runs.live.as_of.generation == head.sequence
    assert runs.live.as_of.git_oid == head.oid[:12]

    lines = service_playbill_orient(instance, section="lines", evaluation_time=when)
    assert lines.live is not None and "lines.enablement" in lines.live.fields
    assert lines.lines is not None and lines.lines[0].enablement == "running"
    older = service_playbill_orient(instance, section="predictions", at=first.oid)
    assert older.live is not None and older.live.as_of.generation == head.sequence

    run = _get(instance, f"ProcedureRun:{run_id}", at=first.oid)
    assert run.live is not None and run.live.as_of.generation == head.sequence

    at_head = _get(instance, line.identity.qualified, evaluation_time=when)
    assert at_head.live is not None and at_head.live.as_of.generation == head.sequence
    assert "enablements" in at_head.live.fields
    assert isinstance(at_head.card, GetLineCard) and at_head.card.enablements
    definition = _get(instance, "Mandate:served-line-mandate", evaluation_time=when)
    assert definition.live is None

    mandates = service_playbill_orient(instance, section="mandates", at=first.oid)
    assert mandates.live is None
