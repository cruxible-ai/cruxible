"""MCP audit is a thin, read-only delegate over the shared audit service."""

from __future__ import annotations

from cruxible_client import contracts
from cruxible_core.mcp import handlers


def _result(evaluation_time: str) -> contracts.PlaybillAuditResult:
    return contracts.PlaybillAuditResult(
        coordinate=contracts.PlaybillAcceptedCoordinate(
            git_oid="1" * 64,
            semantic_root="sha256:" + "2" * 64,
            generation_root="sha256:" + "3" * 64,
            compiler_digest="sha256:" + "4" * 64,
        ),
        generation=7,
        evaluation_time=evaluation_time,
        operational_input_head_digest="sha256:" + "5" * 64,
        audited_through_generation=7,
        rows=[],
        coverage=contracts.PlaybillAuditCoverage(
            access_permitted=True,
            declared_scope=contracts.PlaybillAuditScope(),
            covered_claims=[],
            candidate_claim_count=0,
            returned_claim_count=0,
            omitted_claim_count=0,
            omission_reasons=[],
        ),
        result_digest="sha256:" + "6" * 64,
    )


def test_mcp_audit_delegates_exact_scope_budget_and_profile(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen: dict[str, object] = {}

    def stub(instance_id: str, *, request: dict[str, object]):  # type: ignore[no-untyped-def]
        seen.update(instance_id=instance_id, request=request)
        return _result("2026-08-26T18:00:00+00:00")

    monkeypatch.setattr(handlers, "_get_client", lambda: None)
    monkeypatch.setattr("cruxible_core.runtime.playbill_api.playbill_audit", stub)
    result = handlers.handle_playbill_audit(
        "inst",
        evaluation_time="2026-08-26T18:00:00+00:00",
        access_profile=None,
        claim_type_identities=["ClaimType:status"],
        subject_kinds=["work_item"],
        max_rows=9,
        max_bytes=4096,
        cursor=None,
    )

    assert result.rows == []
    request = seen["request"]
    assert isinstance(request, dict)
    assert request["scope"] == {
        "tag": "playbill-audit-scope-v1",
        "claim_type_identities": ["ClaimType:status"],
        "subject_kinds": ["work_item"],
    }
    assert request["budget"] == {
        "tag": "playbill-audit-budget-v1",
        "max_rows": 9,
        "max_bytes": 4096,
    }
    assert request["access_profile"]["profile_id"] == "mcp-audit"  # type: ignore[index]
