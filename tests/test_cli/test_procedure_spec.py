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
            "returns": "last",
            "steps": [
                {"project": "first", "fields": {"value": literal}},
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
