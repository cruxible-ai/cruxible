"""CLI audit delegates one deterministic read request."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.main import cli


def test_cli_audit_delegates_scope_and_renders_empty_patrol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class StubClient:
        def audit_playbill(
            self, _instance_id: str, **values: object
        ) -> contracts.PlaybillAuditResult:
            calls.append(values)
            return contracts.PlaybillAuditResult(
                coordinate=contracts.PlaybillAcceptedCoordinate(
                    git_oid="1" * 64,
                    semantic_root="sha256:" + "2" * 64,
                    generation_root="sha256:" + "3" * 64,
                    compiler_digest="sha256:" + "4" * 64,
                ),
                generation=7,
                evaluation_time=str(values["evaluation_time"]),
                operational_input_head_digest="sha256:" + "5" * 64,
                audited_through_generation=7,
                rows=[],
                coverage=contracts.PlaybillAuditCoverage(
                    access_permitted=True,
                    declared_scope=contracts.PlaybillAuditScope(
                        claim_type_identities=list(values["claim_type_identities"]),
                        subject_kinds=list(values["subject_kinds"]),
                    ),
                    covered_claims=[],
                    candidate_claim_count=0,
                    returned_claim_count=0,
                    omitted_claim_count=0,
                    omission_reasons=[],
                ),
                result_digest="sha256:" + "6" * 64,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://audit.example.test",
            "--instance-id",
            "inst",
            "playbill",
            "audit",
            "--claim-type",
            "ClaimType:status",
            "--subject-kind",
            "work_item",
            "--max-rows",
            "9",
            "--max-bytes",
            "4096",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "No Claims in the visible audit scope." in result.output
    assert calls[0]["claim_type_identities"] == ("ClaimType:status",)
    assert calls[0]["subject_kinds"] == ("work_item",)
    assert calls[0]["max_rows"] == 9
    assert calls[0]["max_bytes"] == 4096
