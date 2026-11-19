"""Records written before the public rename keep their ``playbill.`` codes.

A ledger evaluation note is immutable, so a refusal recorded before codes moved
to ``cruxible.`` still carries ``playbill.``. Reading it must neither fail nor
rewrite its bytes, and code-keyed logic must see the same code either way.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.diagnostics import CompilerDiagnostic, normalize_code
from cruxible_client.contracts.proposal_models import ProposalEvaluationRecord
from cruxible_client.contracts.query.results import QueryRefusal
from cruxible_core.proposals.proposal_notes import evaluation_bytes

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pre_rename_evaluation_note.json"


def test_a_pre_rename_evaluation_note_reads_and_keeps_its_bytes() -> None:
    raw = FIXTURE.read_bytes()
    record = ProposalEvaluationRecord.model_validate_json(raw)

    assert [item.code for item in record.diagnostics] == [
        "playbill.claim.subject_unresolved",
        "playbill.authoring.capture_contract_missing",
    ]
    assert evaluation_bytes(record) == raw
    assert [normalize_code(item.code) for item in record.diagnostics] == [
        "cruxible.claim.subject_unresolved",
        "cruxible.authoring.capture_contract_missing",
    ]


def test_new_codes_are_cruxible_and_normalize_to_themselves() -> None:
    diagnostic = CompilerDiagnostic(
        code="cruxible.claim.subject_unresolved", severity="error", message="missing"
    )

    assert normalize_code(diagnostic.code) == diagnostic.code
    with pytest.raises(ValueError):
        CompilerDiagnostic(code="other.claim.subject_unresolved", severity="error", message="x")


@pytest.mark.parametrize("prefix", ["cruxible", "playbill"])
def test_query_refusals_read_either_prefix(prefix: str) -> None:
    refusal = QueryRefusal(code=f"{prefix}.query.budget_exceeded", message="over budget")

    assert normalize_code(refusal.code) == "cruxible.query.budget_exceeded"
    with pytest.raises(ValueError):
        QueryRefusal(code=f"{prefix}.claim.budget_exceeded", message="not a query code")
