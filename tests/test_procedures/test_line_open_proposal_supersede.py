"""A Line run supersedes its own open proposal, or keeps it when nothing changed.

Maintainer ruling (dev.roadmap_item/line-pending-proposal-dedupe, 2026-10-13):
a Line run's change equals its open proposal's when it carries the same
statements on the same evidence content, the same retired Claims and the same
created Subjects. Equal: the open proposal is kept untouched and the run reports
it still current. Different: the run's own proposal is submitted and the open
one withdrawn as superseded by it. The decision is made at prepare and
journaled, so recovery reproduces it; an open proposal that settled first is
left alone. Settle-mode Lines are unaffected.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.procedures.line_specs import line_identity_digest
from cruxible_core.exhaust.line_dispatch import LineDispatchStore, dispatch_root
from cruxible_core.exhaust.records import parse_journal_payload
from cruxible_core.procedures.proposal_delivery import ProposalTerminalEgressSink
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.documents import service_inspect_playbill_proposal
from cruxible_core.service.procedures.procedure_runs import (
    _records_for_run,
    service_get_playbill_procedure_run,
)
from cruxible_core.service.proposals.proposal_egress import service_recover_proposal_egress
from cruxible_core.service.proposals.proposals import (
    proposal_still_open,
    service_withdraw_playbill_proposal,
)
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._knowledge_loop_support import accept_proposal
from tests.test_procedures import test_procedure_source_runs as fixtures
from tests.test_procedures.test_procedure_proposal_delivery import (
    NOW,
    _Crash,
    _crash_after,
    _proposal_refs,
    proposal_world,
    run_line,
)

_ACCESS = BodyAccessContext(principal_id="test", can_read_body=True)


def _first_proposal(instance: PlaybillInstance, root: Path, line: Any) -> str:
    first = run_line(instance, root, line)
    assert first.status == "succeeded", first.terminal
    (egress,) = first.terminal_egress
    assert egress.proposal_id is not None
    # The first run of a Line has no open proposal to weigh.
    assert egress.line_open_proposal is None
    return egress.proposal_id


def _change_the_source(root: Path) -> None:
    (root / fixtures.RELATIVE_PATH).write_bytes(
        canonical_bytes({**fixtures.ADVISORY, "severity": "critical"})
    )


def _prepared_payloads(instance: PlaybillInstance, run_id: str) -> list[dict[str, Any]]:
    return [
        payload
        for stored in _records_for_run(instance, run_id)
        if stored.record.event_kind == "terminal_egress"
        for payload in (
            parse_journal_payload(
                instance.body_store().read(stored.record.payload_digest, access=_ACCESS)
            ),
        )
        if isinstance(payload, dict) and payload.get("verdict") == "prepared"
    ]


def _crash_in_delivery(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Kill the process at the sink, after the preparation was journaled."""

    saved: dict[str, str] = {}

    def crash(self, **kwargs):  # type: ignore[no-untyped-def]
        saved["run_id"] = kwargs["request"].run_id
        raise _Crash()

    monkeypatch.setattr(ProposalTerminalEgressSink, "deliver_terminal_egress", crash)
    return saved


def test_an_identical_rerun_keeps_the_open_proposal_as_still_current(tmp_path: Path) -> None:
    instance, _owner, root, line, _procedure = proposal_world(tmp_path)
    first = _first_proposal(instance, root, line)
    refs = _proposal_refs(instance)

    # The same file read again: a new Capture and new Claim ids, the same change.
    second = run_line(instance, root, line, at=NOW + timedelta(minutes=10))

    assert second.status == "succeeded", second.terminal
    (egress,) = second.terminal_egress
    assert egress.verdict == "delivered"
    assert egress.proposal_id is None and egress.operation_key is None
    assert egress.line_open_proposal is not None
    assert egress.line_open_proposal.outcome == "still_current"
    assert egress.line_open_proposal.proposal_id == first
    # The open proposal is untouched: still open, no closing record, no new proposal.
    assert _proposal_refs(instance) == refs
    assert proposal_still_open(instance, first)
    assert instance.proposal_evidence().read_withdrawal(first) is None
    # The run reads back the same outcome after the fact.
    assert second.run_id is not None
    again = service_get_playbill_procedure_run(instance, run_id=second.run_id)
    assert again.terminal_egress == second.terminal_egress


def test_a_changed_rerun_supersedes_the_open_proposal_with_a_link(tmp_path: Path) -> None:
    instance, _owner, root, line, _procedure = proposal_world(tmp_path)
    first = _first_proposal(instance, root, line)

    _change_the_source(root)
    second = run_line(instance, root, line, at=NOW + timedelta(minutes=10))

    assert second.status == "succeeded", second.terminal
    (egress,) = second.terminal_egress
    assert egress.proposal_id is not None and egress.proposal_id != first
    assert egress.line_open_proposal is not None
    assert egress.line_open_proposal.outcome == "superseded"
    assert egress.line_open_proposal.proposal_id == first
    # The earlier proposal is withdrawn as superseded by the new one, by the
    # Line run's own identity; the new one is open and reviewed afresh.
    record = instance.proposal_evidence().read_withdrawal(first)
    assert record is not None
    assert record.kind == "withdrawn"
    assert record.superseded_by == egress.proposal_id
    assert record.actor_id == fixtures._actor(instance).actor_id
    assert not proposal_still_open(instance, first)
    assert proposal_still_open(instance, egress.proposal_id)
    # The Line now tracks its new open proposal and what it superseded.
    tracked = LineDispatchStore(instance).open_proposal(
        line_identity_digest(line.identity), egress.node_id
    )
    assert tracked is not None
    assert (tracked.proposal_id, tracked.superseded) == (egress.proposal_id, first)

    # A third identical run keeps the new proposal.
    third = run_line(instance, root, line, at=NOW + timedelta(minutes=20))
    (kept,) = third.terminal_egress
    assert kept.line_open_proposal is not None
    assert (kept.line_open_proposal.outcome, kept.line_open_proposal.proposal_id) == (
        "still_current",
        egress.proposal_id,
    )


@pytest.mark.parametrize("settlement", ["withdrawn", "accepted"])
def test_an_open_proposal_settled_before_recovery_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settlement: str
) -> None:
    instance, owner, root, line, _procedure = proposal_world(tmp_path)
    first = _first_proposal(instance, root, line)
    _change_the_source(root)
    with monkeypatch.context() as patch:
        saved = _crash_in_delivery(patch)
        with pytest.raises(_Crash):
            run_line(instance, root, line, at=NOW + timedelta(minutes=10))
    (prepared,) = _prepared_payloads(instance, saved["run_id"])
    assert prepared["prepared"]["line_proposal"]["decision"] == "supersede"
    assert prepared["prepared"]["line_proposal"]["open_proposal_id"] == first

    # The open proposal settles before the new one lands.
    if settlement == "withdrawn":
        service_withdraw_playbill_proposal(
            instance,
            proposal_id=first,
            actor_id=fixtures._actor(instance).actor_id,
            reason="Withdrawn by its author.",
            withdrawn_at="2026-09-12T12:15:00.000000Z",
        )
    else:
        accept_proposal(
            instance, owner, service_inspect_playbill_proposal(instance, proposal_id=first)
        )

    recovered = service_recover_proposal_egress(instance, recorded_at=NOW + timedelta(hours=1))
    assert recovered == {saved["run_id"]: "delivered"}
    state = service_get_playbill_procedure_run(instance, run_id=saved["run_id"])
    (egress,) = state.terminal_egress
    assert egress.proposal_id is not None and egress.proposal_id != first
    # Nothing was withdrawn: the settled proposal keeps its own ending.
    assert egress.line_open_proposal is None
    record = instance.proposal_evidence().read_withdrawal(first)
    if settlement == "withdrawn":
        assert record is not None and record.superseded_by is None
    else:
        assert record is None


def test_recovery_reproduces_the_supersede_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner, root, line, _procedure = proposal_world(tmp_path)
    first = _first_proposal(instance, root, line)
    _change_the_source(root)
    with monkeypatch.context() as patch:
        # The new proposal is created at the door, then the process dies.
        _crash_after(patch, after_submit=True)
        with pytest.raises(_Crash):
            run_line(instance, root, line, at=NOW + timedelta(minutes=10))
    assert len(_proposal_refs(instance)) == 2
    assert instance.proposal_evidence().read_withdrawal(first) is None

    recovered = service_recover_proposal_egress(instance, recorded_at=NOW + timedelta(hours=1))
    ((run_id, disposition),) = recovered.items()
    assert disposition == "delivered"
    state = service_get_playbill_procedure_run(instance, run_id=run_id)
    (egress,) = state.terminal_egress
    assert egress.proposal_id is not None
    assert egress.line_open_proposal is not None
    assert (egress.line_open_proposal.outcome, egress.line_open_proposal.proposal_id) == (
        "superseded",
        first,
    )
    record = instance.proposal_evidence().read_withdrawal(first)
    assert record is not None and record.superseded_by == egress.proposal_id
    # Still exactly the two proposals: recovery redrove the same door.
    assert len(_proposal_refs(instance)) == 2
    assert service_recover_proposal_egress(instance, recorded_at=NOW + timedelta(hours=2)) == {}


def test_recovery_reproduces_a_still_current_decision_made_at_prepare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner, root, line, _procedure = proposal_world(tmp_path)
    first = _first_proposal(instance, root, line)
    refs = _proposal_refs(instance)
    with monkeypatch.context() as patch:
        saved = _crash_in_delivery(patch)
        with pytest.raises(_Crash):
            run_line(instance, root, line, at=NOW + timedelta(minutes=10))
    (prepared,) = _prepared_payloads(instance, saved["run_id"])
    decision = prepared["prepared"]["line_proposal"]
    assert (decision["decision"], decision["open_proposal_id"]) == ("still_current", first)
    # Deciding again now would propose (the open proposal is gone); recovery
    # reproduces the journaled decision instead.
    service_withdraw_playbill_proposal(
        instance,
        proposal_id=first,
        actor_id=fixtures._actor(instance).actor_id,
        reason="Withdrawn by its author.",
        withdrawn_at="2026-09-12T12:15:00.000000Z",
    )

    recovered = service_recover_proposal_egress(instance, recorded_at=NOW + timedelta(hours=1))
    assert recovered == {saved["run_id"]: "delivered"}
    state = service_get_playbill_procedure_run(instance, run_id=saved["run_id"])
    (egress,) = state.terminal_egress
    assert egress.proposal_id is None
    assert egress.line_open_proposal is not None
    assert egress.line_open_proposal.outcome == "still_current"
    assert _proposal_refs(instance) == refs


def test_a_settle_mode_line_never_weighs_or_supersedes_its_proposals(tmp_path: Path) -> None:
    from tests.test_procedures.test_procedure_settlement_delivery import (
        run_settle,
        settle_world,
    )

    # A false condition falls back to an ordinary proposal, every run.
    instance, root, line = settle_world(tmp_path, only_subject="someone-else")
    first = run_settle(instance, root, line)
    (root / fixtures.RELATIVE_PATH).write_bytes(
        canonical_bytes({**fixtures.ADVISORY, "severity": "critical"})
    )
    second = run_settle(instance, root, line, now=NOW + timedelta(minutes=10))

    proposals = []
    for state in (first, second):
        assert state.status == "succeeded", state.terminal
        (egress,) = state.terminal_egress
        assert egress.settle_outcome == "proposed" and egress.proposal_id is not None
        assert egress.line_open_proposal is None
        proposals.append(egress.proposal_id)
    assert proposals[0] != proposals[1]
    # Both fallback proposals stay open; nothing was withdrawn or tracked.
    for proposal_id in proposals:
        assert proposal_still_open(instance, proposal_id)
        assert instance.proposal_evidence().read_withdrawal(proposal_id) is None
    if dispatch_root(instance).is_dir():
        assert (
            LineDispatchStore(instance).open_proposal(
                line_identity_digest(line.identity), egress.node_id
            )
            is None
        )
