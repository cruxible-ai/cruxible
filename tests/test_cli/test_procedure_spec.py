"""Procedure text reads retain the complete authored form."""

from types import SimpleNamespace

from cruxible_client.contracts.get_reads import GetProcedureCard
from cruxible_client.contracts.procedures.authored import ProcedureSpec
from cruxible_core.cli.commands.playbill import _emit_get_text


def test_procedure_get_text_keeps_every_authored_step(capsys):
    literal = "x" * 320
    spec = ProcedureSpec.model_validate(
        {
            "procedure": "readable",
            "input": {},
            "returns": {"from": "last"},
            "steps": [
                {"project": "first", "fields": {"value": literal}},
                {"guard": 'first.value != ""', "else": "halt(empty)"},
                {"project": "last", "fields": {"value": "$first.value"}},
            ],
        }
    )
    card = GetProcedureCard(procedure="readable", inputs={}, runnable="direct", authored=spec)
    _emit_get_text(SimpleNamespace(card=card, kind="procedure"))
    shown = capsys.readouterr().out
    assert literal in shown
    assert '"project": "first"' in shown
    assert '"project": "last"' in shown
    assert '"value": "$first.value"' in shown
    assert '"else": "halt(empty)"' in shown
    assert '"from": "last"' in shown


def test_procedure_run_accepts_explicit_slot_bindings(tmp_path):
    from cruxible_core.cli.commands.playbill import run_procedure

    input_file = tmp_path / "input.json"
    input_file.write_text("{}")
    with run_procedure.make_context(
        "run", ["test.slotted", str(input_file), "--bind", "compute=test-provider/build"]
    ) as context:
        assert context.params["bind_values"] == ("compute=test-provider/build",)
