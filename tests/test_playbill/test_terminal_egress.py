"""Execution-owned terminal egress, receipt, and item-closure laws."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import cruxible_core.service.playbill_procedure_runs as procedure_run_service
from cruxible_client.contracts.procedures.models import (
    InboxEgressNodeV3,
    ProcedureDefinitionV3,
    StateTapNodeV3,
)
from cruxible_client.contracts.procedures.results import ProcedureSettlementRefusalV1
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.exhaust import parse_journal_payload
from cruxible_core.playbill.procedures.egress import (
    TerminalEgressChildReceiptV1,
    TerminalEgressReceiptV1,
    TerminalEgressRequestV1,
    compute_effective_rung,
)
from cruxible_core.playbill.procedures.execution import (
    PreparedProcedureRunV1,
    ProcedureExecutor,
    ProcedureRunAdmissionV2,
    procedure_admission_digest,
)
from cruxible_core.playbill.procedures.terminal_services import ProcedureSettlementRefused
from tests.test_playbill.test_procedure_execution import (
    NOW,
    _accepted,
    _Authority,
    _budget,
    _Contracts,
    _digest,
    _fixture,
    _hard_caps,
    _pin,
    _prepare,
    _StateReader,
)


def _terminal_procedure():
    contract_in = _pin("contract-in", "Contract", "terminal-input")
    contract_out = _pin("contract-out", "Contract", "terminal-output")
    query = _pin("query", "QueryDefinition", "terminal-items")
    definition = ProcedureDefinitionV3(
        name="terminal-procedure",
        contract_in=contract_in,
        contract_out=contract_out,
        nodes=(
            StateTapNodeV3(
                node_id="read",
                query=query,
                parameters={},
                as_="rows",
                next="inbox",
            ),
            InboxEgressNodeV3(node_id="inbox", input={"items": "$steps.rows.items"}),
        ),
        returns="rows",
        budget=_budget(items=100),
        hard_caps=_hard_caps(items=100),
        terminal_capability=1,
    )
    return _accepted(definition, pins=(contract_in, contract_out, query))


def _line_bound(prepared: PreparedProcedureRunV1) -> PreparedProcedureRunV1:
    provisional = prepared.admission.model_copy(
        update={
            "invocation_origin": "line",
            "line_spec_digest": _digest("line-spec"),
            "occurrence_id": _digest("occurrence"),
            "deployment_snapshot_digest": _digest("deployment"),
            "acquisition_policy_digest": _digest("acquisition-policy"),
            "sensitivity_policy_digest": _digest("sensitivity-policy"),
            "mandate_coordinate_digest": _digest("mandate-coordinate"),
            "calibration_coordinate_digest": _digest("calibration-coordinate"),
            "admission_binding_digest": "sha256:" + "0" * 64,
        }
    )
    bound = provisional.model_copy(
        update={"admission_binding_digest": procedure_admission_digest(provisional)}
    )
    admission = ProcedureRunAdmissionV2.model_validate(bound.model_dump(mode="python"))
    return prepared.model_copy(update={"admission": admission})


class _InboxSink:
    def __init__(self) -> None:
        self.requests: list[TerminalEgressRequestV1] = []

    def deliver_terminal_egress(
        self, *, request: TerminalEgressRequestV1
    ) -> TerminalEgressReceiptV1:
        self.requests.append(request)
        return TerminalEgressReceiptV1(
            kind=request.kind,
            run_id=request.run_id,
            node_id=request.node_id,
            disposition="posted",
            children=tuple(
                TerminalEgressChildReceiptV1(
                    child_index=item.child_index,
                    item_key=item.item_key,
                    egress_digest=_digest(f"inbox-{item.child_index}"),
                )
                for item in request.items
            ),
        )


class _SettlementRefusingSink:
    def deliver_terminal_egress(self, *, request: TerminalEgressRequestV1) -> None:
        raise ProcedureSettlementRefused(
            "settlement_candidate_scope_mismatch",
            "Target paths differ from the admitted candidate.",
            details={"target_paths": ["claims/expected.json"]},
        )


def _payload(fixture, digest: str) -> object:
    return parse_journal_payload(
        fixture.bodies.read(
            digest,
            access=BodyAccessContext(principal_id="terminal-egress-test", can_read_body=True),
        )
    )


def test_terminal_without_a_sink_still_records_each_item_dependency(tmp_path) -> None:
    root = tmp_path / "no-sink"
    root.mkdir()
    fixture = _fixture(root)
    accepted = _terminal_procedure()
    prepared = _prepare(
        accepted,
        fixture,
        _StateReader({"items": [{"id": "one"}, {"id": "two"}]}),
    )

    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
    ).execute(prepared, accepted)

    assert result.status == "refused"
    assert result.refusal is not None and result.refusal.code == "terminal_not_available"
    records = fixture.journal.all_records(fixture.stream, "runs")
    assert [item.record.event_kind for item in records].count("item_dependencies") == 2
    terminal = next(item for item in records if item.record.event_kind == "terminal_egress")
    payload = _payload(fixture, terminal.record.payload_digest)
    assert isinstance(payload, dict)
    assert payload["verdict"] == ("dependencies_bound_egress_pending")


def test_terminal_sink_delivery_is_receipted_after_item_dependencies(tmp_path) -> None:
    root = tmp_path / "with-sink"
    root.mkdir()
    fixture = _fixture(root)
    accepted = _terminal_procedure()
    prepared = _line_bound(
        _prepare(
            accepted,
            fixture,
            _StateReader({"items": [{"id": "one"}, {"id": "two"}]}),
            run_id="line-run",
        )
    )
    admission = prepared.admission
    rung = compute_effective_rung(
        procedure_terminal_capability=1,
        requested_terminal_rung=1,
        selector_privacies={},
        taint_labels=(),
        mandate_grants={},
        calibration_caps=(),
        evaluation_time=NOW,
        procedure_definition_digest=admission.definition_digest,
        line_spec_digest=admission.line_spec_digest or "",
        sensitivity_policy_digest=admission.sensitivity_policy_digest or "",
        mandate_coordinate_digest=admission.mandate_coordinate_digest or "",
        calibration_coordinate_digest=admission.calibration_coordinate_digest or "",
    )
    sink = _InboxSink()

    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        effective_rung=rung,
        egress_sink=sink,
    ).execute(prepared, accepted)

    assert result.status == "succeeded"
    assert len(sink.requests) == 1 and len(sink.requests[0].items) == 2
    records = fixture.journal.all_records(fixture.stream, "runs")
    kinds = [item.record.event_kind for item in records]
    assert kinds.index("item_dependencies") < kinds.index("terminal_egress")
    terminal = next(item for item in records if item.record.event_kind == "terminal_egress")
    payload = _payload(fixture, terminal.record.payload_digest)
    assert isinstance(payload, dict)
    assert payload["verdict"] == "delivered"
    assert payload["receipt"]["disposition"] == "posted"


def test_settlement_refusal_projects_as_its_dedicated_public_terminal(
    tmp_path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    root = tmp_path / "settlement-refusal"
    root.mkdir()
    fixture = _fixture(root)
    accepted = _terminal_procedure()
    prepared = _line_bound(
        _prepare(
            accepted,
            fixture,
            _StateReader({"items": [{"id": "one"}]}),
            run_id="line-settlement-refusal",
        )
    )
    admission = prepared.admission
    rung = compute_effective_rung(
        procedure_terminal_capability=1,
        requested_terminal_rung=1,
        selector_privacies={},
        taint_labels=(),
        mandate_grants={},
        calibration_caps=(),
        evaluation_time=NOW,
        procedure_definition_digest=admission.definition_digest,
        line_spec_digest=admission.line_spec_digest or "",
        sensitivity_policy_digest=admission.sensitivity_policy_digest or "",
        mandate_coordinate_digest=admission.mandate_coordinate_digest or "",
        calibration_coordinate_digest=admission.calibration_coordinate_digest or "",
    )

    result = ProcedureExecutor(
        journal=fixture.journal,
        bodies=fixture.bodies,
        run_index=fixture.run_index,
        fencing_token="writer",
        activation_authority=_Authority(accepted.artifact_digest),
        contract_validator=_Contracts(),
        effective_rung=rung,
        egress_sink=_SettlementRefusingSink(),  # type: ignore[arg-type]
    ).execute(prepared, accepted)

    assert result.status == "refused"
    assert result.refusal is not None
    assert result.refusal.code == "settlement_candidate_scope_mismatch"
    records = fixture.journal.all_records(fixture.stream, "runs")
    monkeypatch.setattr(
        procedure_run_service,
        "_records_for_run",
        lambda _instance, _run_id: records,
    )
    state = procedure_run_service._state_from_records(  # noqa: SLF001
        SimpleNamespace(body_store=lambda: fixture.bodies),  # type: ignore[arg-type]
        run_id=prepared.admission.run_id,
        receipt=result.receipt,
    )
    assert isinstance(state.terminal, ProcedureSettlementRefusalV1)
    assert state.terminal.code == "settlement_candidate_scope_mismatch"
    # The declared change, not the code restated: the settlement family carries
    # real instructions from the source-owned catalog.
    assert state.terminal.repair.hand_edit.required_change == (
        "resubmit_the_candidate_whose_scope_matches_its_admission"
    )


def test_the_mandate_term_names_whichever_source_reached_the_rung() -> None:
    """A tier-held rung is never attributed to a mandate that grants less."""

    common = {
        "procedure_terminal_capability": 3,
        "requested_terminal_rung": 3,
        "selector_privacies": {},
        "taint_labels": (),
        "mandate_grants": {},
        "calibration_caps": (),
        "evaluation_time": datetime(2026, 9, 3, tzinfo=UTC),
        "procedure_definition_digest": _digest("definition"),
        "line_spec_digest": _digest("line"),
        "sensitivity_policy_digest": _digest("sensitivity"),
        "mandate_coordinate_digest": _digest("mandate-coordinate"),
        "calibration_coordinate_digest": _digest("calibration"),
    }

    tier_wins = compute_effective_rung(
        **common,  # type: ignore[arg-type]
        procedure_mandate_rung=2,
        caller_tier_rung=3,
    )
    term = next(item for item in tier_wins.terms if item.term == "mandate_grant")
    assert term.rung == 3
    assert "authority tier holds rung 3" in term.reason
    assert "mandate grants rung 2" in term.reason

    mandate_wins = compute_effective_rung(
        **common,  # type: ignore[arg-type]
        procedure_mandate_rung=3,
        caller_tier_rung=0,
    )
    term = next(item for item in mandate_wins.terms if item.term == "mandate_grant")
    assert term.rung == 3
    assert term.reason == "The exact accepted Procedure mandate grants rung 3."
