"""Guardrail: every accepted or operational reference ``next`` emits resolves in ``get``.

A queue row an agent cannot open is a dead end: before the operational-reads
batch, ``next`` named a stalled Line, a due Line (by its identity digest), an
unavailable Capture, a settleable ResolutionContract and an expiring mandate by
references ``get`` refused. Each ``next`` reason is classified here by what its
subject and related identities name; a new reason must be classified before it
ships. The live half builds real queues and opens every reference they carry.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, get_args

import pytest

from cruxible_client.contracts import NextReason
from cruxible_client.contracts.get_reads import GetRequest
from cruxible_client.contracts.line_dispatch import LineEvaluateRequest
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.discovery.next import NextRequest, service_playbill_next
from cruxible_core.service.procedures.line_dispatch import (
    service_evaluate_line,
    service_stop_line_arm,
)
from cruxible_core.storage.cas import BodyAccessContext

_ACCESS = BodyAccessContext(principal_id="reader", can_read_body=True)
_PROFILE = {"profile_id": "guardrail", "permitted_access_classes": ["instance", "public"]}

# What a reason's subject and related identities name. `get` reads accepted
# state and operational state; a workspace reason names things in the
# caller's working tree (a floor, a source file, a projection block), which
# are read with the workspace tools, not `get`.
_REFERENCE_FORMS: dict[str, Literal["get", "workspace"]] = {
    "claim_conflicted": "get",
    "claim_uncovered": "get",
    "claim_stale_evidence": "get",
    "citation_drifted": "workspace",
    "citation_source_unobserved": "workspace",
    "evidence_expiring": "get",
    "floor_invalid": "workspace",
    "projection_dirty": "workspace",
    "projection_backing_stale": "workspace",
    "claim_dependency_stale": "get",
    "claim_attestation_threshold_met": "get",
    "claim_contradicting_evidence_available": "get",
    "claim_new_evidence_supporting": "get",
    "claim_new_evidence_unreviewed": "get",
    "document_modified": "workspace",
    "workspace_binding_missing": "workspace",
    "unregistered_projection_block": "workspace",
    "projection_marker_invalid": "workspace",
    "proposal_stale": "get",
    "proposal_awaiting_approval": "get",
    "mandate_expiring": "get",
    "consumer_stalled": "get",
    "evidence_unavailable": "get",
    "prediction_settleable": "get",
    "prediction_window_unbindable": "get",
    "line_coverage_gap": "get",
    "line_work_pending": "get",
}


def test_every_next_reason_names_what_its_references_are_read_with() -> None:
    assert set(_REFERENCE_FORMS) == set(get_args(NextReason))


def _references(result) -> list[str]:  # type: ignore[no-untyped-def]
    refs: list[str] = []
    for item in result.items:
        for row in (item, *item.findings):
            if _REFERENCE_FORMS[row.reason] != "get":
                continue
            refs.append(row.subject_identity)
            refs.extend(row.related_identities)
    detail = result.status.line_dispatch.detail
    lines = detail.get("lines", []) if isinstance(detail, dict) else []
    refs.extend(str(line["line_identity_digest"]) for line in lines)
    return list(dict.fromkeys(refs))


def _assert_every_reference_resolves(instance, result) -> list[str]:  # type: ignore[no-untyped-def]
    refs = _references(result)
    for ref in refs:
        service_playbill_get(instance, request=GetRequest(ref=ref), access=_ACCESS)
    return refs


def _queue(instance, when: datetime):  # type: ignore[no-untyped-def]
    return service_playbill_next(
        instance,
        request=NextRequest(evaluation_time=when, access_profile=_PROFILE),
        caller_rung=0,
    )


def test_line_mandate_and_due_line_references_resolve(tmp_path: Path) -> None:
    from tests.test_procedures.test_line_arming import _armed_world
    from tests.test_procedures.test_line_dispatch import _active_segment
    from tests.test_procedures.test_line_triggers import capture
    from tests.test_procedures.test_procedure_run_surface import _actor

    instance, line, procedure, start = _armed_world(tmp_path)
    service_stop_line_arm(
        instance,
        _active_segment(instance),
        reason="credential_revoked",
        detail="revoked",
        actor=_actor(instance),
        now=start + timedelta(seconds=1),
    )
    capture(instance, procedure, at=start + timedelta(seconds=2))
    later = start + timedelta(seconds=3)
    service_evaluate_line(
        instance,
        line.identity.name,
        LineEvaluateRequest(since=start, until=later),
        actor=_actor(instance),
        now=later,
    )
    # A week before the Line mandate lapses, so next reports it expiring.
    result = _queue(instance, datetime(2026, 12, 28, tzinfo=UTC))

    reasons = {item.reason for item in result.items}
    assert {"consumer_stalled", "mandate_expiring"} <= reasons
    assert result.status.line_dispatch.state == "due"
    refs = _assert_every_reference_resolves(instance, result)
    assert line.identity.qualified in refs
    assert any(ref.startswith("ProcedureMandate:") for ref in refs)
    assert any(ref.startswith("sha256:") for ref in refs)


def test_an_enabled_lines_pending_work_reference_resolves(tmp_path: Path) -> None:
    from tests.test_procedures.test_line_arming import _armed_world
    from tests.test_procedures.test_line_triggers import capture
    from tests.test_procedures.test_procedure_run_surface import _actor

    instance, line, procedure, start = _armed_world(tmp_path)
    capture(instance, procedure, at=start + timedelta(seconds=2))
    later = start + timedelta(seconds=3)
    service_evaluate_line(
        instance,
        line.identity.name,
        LineEvaluateRequest(since=start, until=later),
        actor=_actor(instance),
        now=later,
    )

    result = _queue(instance, later + timedelta(seconds=1))

    pending = [item for item in result.items if item.reason == "line_work_pending"]
    assert [item.subject_identity for item in pending] == [line.identity.qualified]
    refs = _assert_every_reference_resolves(instance, result)
    assert line.identity.qualified in refs


def test_prediction_and_capture_references_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_consumers.test_prediction_settlement import FIXED_CLOSES, drain, fixed_world

    from cruxible_core.consumers.next import evidence

    instance, _owner, capture_digest, _contract = fixed_world(tmp_path)
    drain(instance, now=FIXED_CLOSES + timedelta(minutes=1))
    finding = evidence.EvidenceFinding(
        capture_digest=capture_digest,
        part="body",
        object_digest="sha256:" + "b" * 64,
        state="missing",
        checked_at=FIXED_CLOSES,
    )
    monkeypatch.setattr(evidence, "evidence_findings", lambda _instance: (finding,))

    result = _queue(instance, FIXED_CLOSES + timedelta(minutes=2))

    reasons = {item.reason for item in result.items}
    assert {"prediction_settleable", "evidence_unavailable"} <= reasons
    refs = _assert_every_reference_resolves(instance, result)
    assert f"Capture:{capture_digest}" in refs
    assert any(ref.startswith("ResolutionContract:") for ref in refs)
    assert any(ref.startswith("Claim:CLM-") for ref in refs)
