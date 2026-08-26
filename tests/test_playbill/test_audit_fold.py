"""Exact G9 audit ranking, coverage accounting, and purity laws."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.audit import AuditBudgetV1, AuditScopeV1, audit_row_order
from cruxible_core.playbill.consumption import consumption_aggregate
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.service.playbill_audit import (
    PlaybillAuditRequestV1,
    completed_audit_runs,
    service_playbill_audit,
)
from tests.test_playbill._support import initialize_local

NOW = datetime(2026, 8, 26, 18, 0, tzinfo=UTC)


def _actor() -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="human_user",
        actor_id="auditor",
        org_id="org-test",
        operation_id="op-audit",
        timestamp=NOW,
    )


def _request(*, permitted: bool = True) -> PlaybillAuditRequestV1:
    return PlaybillAuditRequestV1(
        evaluation_time=NOW,
        access_profile=CoverageAccessProfileV1(
            profile_id="test-audit",
            permitted_access_classes=("instance", "public") if permitted else ("public",),
        ),
    )


def test_empty_audit_is_byte_identical_idempotent_and_operational_only(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    accepted_before = instance.accepted_coordinate()
    first = service_playbill_audit(instance, request=_request(), actor_context=_actor())
    second = service_playbill_audit(instance, request=_request(), actor_context=_actor())

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.coverage.access_permitted
    assert first.rows == ()
    assert first.audited_through_generation == 0
    assert len(completed_audit_runs(instance)) == 1
    assert instance.accepted_coordinate() == accepted_before
    assert consumption_aggregate(instance).artifacts == ()
    assert instance.review_operational_store().events(family="consumption") == ()


def test_access_gate_precedes_counts_and_writes(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    result = service_playbill_audit(
        instance,
        request=_request(permitted=False),
        actor_context=_actor(),
    )

    assert not result.coverage.access_permitted
    assert result.coverage.candidate_claim_count == 0
    assert result.coverage.covered_claims == ()
    assert result.audited_through_generation is None
    assert instance.review_operational_store().events(family="audit") == ()


def test_failed_fold_writes_no_completed_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _owner = initialize_local(tmp_path)

    def fail(_instance):  # type: ignore[no-untyped-def]
        raise RuntimeError("rank fold failed")

    monkeypatch.setattr("cruxible_core.service.playbill_audit.consumption_aggregate", fail)
    with pytest.raises(RuntimeError, match="rank fold failed"):
        service_playbill_audit(instance, request=_request(), actor_context=_actor())
    assert completed_audit_runs(instance) == ()


def test_audit_scope_and_budget_are_closed_and_canonical() -> None:
    assert (
        AuditScopeV1(
            claim_type_identities=("ClaimType:project.work_item.status",),
            subject_kinds=("project.work_item",),
        ).tag
        == "playbill-audit-scope-v1"
    )
    with pytest.raises(ValueError, match="byte-sorted"):
        AuditScopeV1(subject_kinds=("z", "a"))
    with pytest.raises(ValueError):
        AuditBudgetV1(max_rows=0)


def test_rank_order_is_score_then_every_integer_factor_then_claim_path() -> None:
    class Row:
        def __init__(self, score: int, stake: int, weakness: int, stale: int, path: str) -> None:
            self.rank_score = score
            self.factors = type(
                "Factors",
                (),
                {"stake": stake, "weakness": weakness, "staleness": stale},
            )()
            self.claim_path = path

    rows = (
        Row(20, 2, 5, 2, "claims/z.yaml"),
        Row(20, 4, 1, 5, "claims/a.yaml"),
        Row(20, 4, 1, 5, "claims/b.yaml"),
        Row(21, 1, 1, 21, "claims/c.yaml"),
    )
    ordered = sorted(rows, key=audit_row_order)  # type: ignore[arg-type]
    assert [item.claim_path for item in ordered] == [
        "claims/c.yaml",
        "claims/a.yaml",
        "claims/b.yaml",
        "claims/z.yaml",
    ]
