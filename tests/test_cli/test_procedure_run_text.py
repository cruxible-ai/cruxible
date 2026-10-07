"""A refused `procedure run` or `line run` prints its code and repair in text mode."""

from types import SimpleNamespace

from cruxible_client.contracts.procedures.results import (
    ProcedureAdmissionRefusal,
    ProcedureNodeRefusal,
)
from cruxible_client.contracts.repairs import served_repair_for_refusal
from cruxible_core.cli.commands.playbill import _echo_run_outcome


def _echo(capsys, **state) -> str:  # type: ignore[no-untyped-def]
    values = {"result": None, "terminal": None, "next_operation": {"kind": "terminal"}, **state}
    _echo_run_outcome(SimpleNamespace(**values), "audit.sign-line")  # type: ignore[arg-type]
    return capsys.readouterr().out


def test_a_refused_line_run_names_the_code_and_the_runnable_repair(capsys) -> None:
    refusal = ProcedureAdmissionRefusal(
        code="line_mandate_required",
        message="This Line can propose or settle and has no mandate.",
        repair=served_repair_for_refusal("line_mandate_required"),
    )
    out = _echo(capsys, status="admission_refused", terminal=refusal)
    assert out == (
        "audit.sign-line: admission_refused\n"
        "Refused: line_mandate_required: This Line can propose or settle and has no mandate.\n"
        "Repair: cruxible authoring example procedure-mandate\n"
        "Next: terminal\n"
    )


def test_an_input_refusal_names_the_offending_field(capsys) -> None:
    refusal = ProcedureNodeRefusal(
        code="contract_input_refused",
        message="The Procedure node input contract refused its value.",
        node_id="procedure",
        details={"field_path": "count"},
        repair=served_repair_for_refusal("contract_input_refused"),
    )
    out = _echo(capsys, status="node_refused", terminal=refusal)
    assert "Refused: contract_input_refused:" in out
    assert "Field: count\n" in out
    assert "Repair: hand edit refusal/contract_input_refused:" in out


def test_a_successful_run_leads_with_its_result(capsys) -> None:
    out = _echo(capsys, status="succeeded", result={"count": 1}, next_operation={"kind": "done"})
    assert out == 'audit.sign-line: succeeded\nResult: {"count": 1}\nNext: done\n'
