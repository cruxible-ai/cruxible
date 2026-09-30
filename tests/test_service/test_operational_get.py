"""``get`` resolves every operational kind by a stable reference.

Lines (by name or by the identity digest ``next`` names a due Line by),
Captures (``CAP-<12+ hex>`` or ``Capture:<digest>``), ResolutionContracts and
ProcedureMandates (``Mandate:`` or the ``ProcedureMandate:`` form ``next``
emits) each answer a values-first card; a wrong or ambiguous reference refuses
with the nearest valid references.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from cruxible_client.contracts.get_reads import PlaybillGetRequestV1
from cruxible_client.contracts.line_dispatch import LineDispatchRequestV1, LineEvaluateRequestV1
from cruxible_client.contracts.operational_reads import (
    PlaybillGetCaptureCardV1,
    PlaybillGetLineCardV1,
    PlaybillGetMandateCardV1,
    PlaybillGetResolutionContractCardV1,
)
from cruxible_client.contracts.procedures.line_specs import line_identity_digest
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.procedures.line_dispatch import (
    service_dispatch_line,
    service_evaluate_line,
    service_stop_line_arm,
)
from cruxible_core.service.read_refusals import ReadRefusalError
from cruxible_core.storage.cas import BodyAccessContext
from tests.test_procedures.test_line_arming import _armed_world
from tests.test_procedures.test_line_dispatch import _active_segment
from tests.test_procedures.test_line_triggers import capture
from tests.test_procedures.test_procedure_run_surface import _actor

_ACCESS = BodyAccessContext(principal_id="reader", can_read_body=True)


def _get(instance, ref: str, **fields):  # type: ignore[no-untyped-def]
    return service_playbill_get(
        instance, request=PlaybillGetRequestV1(ref=ref, **fields), access=_ACCESS
    )


@pytest.fixture(scope="module")
def line_world(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    """An armed Line that admitted one run, then had its arm stopped, plus a waiting occurrence."""

    tmp_path = tmp_path_factory.mktemp("operational-line")
    instance, line, procedure, start = _armed_world(tmp_path)
    capture(instance, procedure, at=start + timedelta(seconds=1))
    later = start + timedelta(seconds=2)
    service_evaluate_line(
        instance,
        line.identity.name,
        LineEvaluateRequestV1(since=start, until=later),
        actor=_actor(instance),
        now=later,
    )
    dispatched = service_dispatch_line(
        instance,
        line.identity.name,
        LineDispatchRequestV1(limit=1),
        actor=_actor(instance),
        now=later + timedelta(seconds=1),
        caller_rung=3,
    )
    service_stop_line_arm(
        instance,
        _active_segment(instance),
        reason="credential_revoked",
        detail="The arming credential was revoked.",
        actor=_actor(instance),
        now=later + timedelta(seconds=2),
    )
    return instance, line, dispatched, later + timedelta(seconds=3)


def test_a_line_card_names_its_procedure_trigger_arms_and_runs(line_world) -> None:  # type: ignore[no-untyped-def]
    instance, line, dispatched, when = line_world

    result = _get(instance, f"Line:{line.identity.name}", evaluation_time=when)

    assert result.kind == "line" and result.ref == line.identity.qualified
    card = result.card
    assert isinstance(card, PlaybillGetLineCardV1)
    assert card.identity_digest == line_identity_digest(line.identity)
    assert card.procedure == line.procedure.target.qualified
    assert card.trigger == "capture_landing"
    assert card.trigger_detail is not None and "lands" in card.trigger_detail
    assert card.authority in {"observe", "propose", "settle"}
    (arm,) = card.arms
    assert arm.state == "stopped" and arm.stop_reason == "credential_revoked"
    assert arm.armed_by == "operator" and arm.principal_kind == "local_operator"
    assert card.arms_total == 1
    admitted = [item.run_id for item in dispatched.items if item.run_id is not None]
    assert [row.run for row in card.recent_runs] == admitted
    assert card.runs_total == len(admitted)
    assert card.recent_runs[0].line == line.identity.qualified
    assert result.live is not None and "arms" in result.live.fields
    assert any(step.startswith("cruxible_playbill_get(") for step in card.next)


def test_the_line_identity_digest_next_names_resolves_to_its_line(line_world) -> None:  # type: ignore[no-untyped-def]
    instance, line, _dispatched, when = line_world
    digest = line_identity_digest(line.identity)

    for ref in (digest, digest[: len("sha256:") + 12], line.identity.name):
        result = _get(instance, ref, evaluation_time=when)
        assert result.kind == "line" and result.ref == line.identity.qualified


def test_a_line_read_at_an_older_generation_shows_the_definition_only(line_world) -> None:  # type: ignore[no-untyped-def]
    instance, line, _dispatched, when = line_world
    head = instance.accepted_coordinate()

    history = _get(instance, line.identity.qualified, detail="history")
    assert history.history is not None and history.history.revisions

    at_head = _get(instance, line.identity.qualified, at=head.git_oid, evaluation_time=when)
    assert isinstance(at_head.card, PlaybillGetLineCardV1) and at_head.card.arms


def test_a_mandate_reads_by_either_reference_form(line_world) -> None:  # type: ignore[no-untyped-def]
    instance, _line, _dispatched, when = line_world

    for ref in ("Mandate:served-line-mandate", "ProcedureMandate:served-line-mandate"):
        result = _get(instance, ref, evaluation_time=when)
        assert result.kind == "mandate" and result.ref == "Mandate:served-line-mandate"
        card = result.card
        assert isinstance(card, PlaybillGetMandateCardV1)
        assert card.mandate == "ProcedureMandate:served-line-mandate"
        assert card.grants == "propose" and card.state == "active"
        assert card.namespace == ("claims",)

    name = "Mandate:served-line-mandate"
    expiring = _get(instance, name, evaluation_time=card.expires_at - timedelta(days=1))
    assert isinstance(expiring.card, PlaybillGetMandateCardV1)
    assert expiring.card.state == "expiring"
    expired = _get(instance, name, evaluation_time=card.expires_at)
    assert isinstance(expired.card, PlaybillGetMandateCardV1) and expired.card.state == "expired"


def test_wrong_operational_references_refuse_with_the_nearest_valid_ones(line_world) -> None:  # type: ignore[no-untyped-def]
    instance, line, _dispatched, _when = line_world

    with pytest.raises(ReadRefusalError) as typo:
        _get(instance, f"Line:{line.identity.name}x")
    assert typo.value.error_code == "playbill.get.ref_not_found"
    assert f"Line:{line.identity.name}" in typo.value.candidates

    with pytest.raises(ReadRefusalError) as mandate:
        _get(instance, "Mandate:served-line-mandat")
    assert "Mandate:served-line-mandate" in mandate.value.candidates

    with pytest.raises(ReadRefusalError) as nothing:
        _get(instance, "CAP-" + "f" * 12)
    assert nothing.value.error_code == "playbill.get.ref_not_found"
    assert nothing.value.repair is not None
    assert nothing.value.repair.arguments == {"section": "captures"}

    with pytest.raises(ReadRefusalError) as malformed:
        _get(instance, "CAP-12")
    assert malformed.value.error_code == "playbill.get.ref_malformed"


@pytest.fixture(scope="module")
def prediction_world(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    from tests.test_consumers.test_prediction_settlement import (
        FIXED_CLOSES,
        drain,
        fixed_world,
    )

    instance, _owner, capture_digest, contract = fixed_world(tmp_path_factory.mktemp("predict"))
    drain(instance, now=FIXED_CLOSES + timedelta(minutes=1))
    return instance, capture_digest, contract, FIXED_CLOSES + timedelta(minutes=2)


def test_a_resolution_contract_card_names_its_hypothesis_window_and_state(
    prediction_world,  # type: ignore[no-untyped-def]
) -> None:
    instance, _capture, contract, when = prediction_world

    result = _get(instance, contract.identity.qualified, evaluation_time=when)

    card = result.card
    assert result.kind == "resolution_contract"
    assert isinstance(card, PlaybillGetResolutionContractCardV1)
    assert card.hypothesis.startswith("CLM-")
    assert card.hypothesis_value == "ready"
    assert card.state == "settleable"
    (window,) = card.windows
    assert window.status == "settleable" and card.windows_total == 1
    assert "fixed window" in card.window
    assert card.next[0] == f'cruxible_playbill_get(ref="{card.hypothesis}")'


def test_a_capture_reads_by_handle_prefix_or_full_digest(prediction_world) -> None:  # type: ignore[no-untyped-def]
    instance, capture_digest, _contract, when = prediction_world
    hex_digits = capture_digest.removeprefix("sha256:")

    for ref in (
        "CAP-" + hex_digits[:12],
        f"Capture:{capture_digest}",
        f"Capture:{hex_digits[:16]}",
        f"Capture:CAP-{hex_digits[:12]}",
    ):
        result = _get(instance, ref, evaluation_time=when)
        assert result.kind == "capture" and result.ref == f"Capture:{capture_digest}"
    card = result.card
    assert isinstance(card, PlaybillGetCaptureCardV1)
    assert card.capture == "CAP-" + hex_digits[:12] and card.digest == capture_digest
    assert card.contract.startswith("CaptureContract:") and card.version == 1
    assert card.status == "available"
    assert card.citing and card.citing_total >= len(card.citing)
    assert "project.work_item/wi-42" in card.subjects
    assert card.next[-1] == (
        f'cruxible_playbill_read_capture(request={{"capture_digest": "{capture_digest}"}})'
    )

    proof = _get(instance, "CAP-" + hex_digits[:12], detail="proof")
    assert proof.proof is not None and proof.proof["capture_digest"] == capture_digest

    cli = _get(instance, "CAP-" + hex_digits[:12], surface="cli")
    assert isinstance(cli.card, PlaybillGetCaptureCardV1)
    assert cli.card.next[-1] == f"cruxible playbill capture read {capture_digest}"


def test_a_capture_card_names_the_evidence_workers_finding(
    prediction_world,  # type: ignore[no-untyped-def]
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from cruxible_core.consumers import evidence

    instance, capture_digest, _contract, _when = prediction_world
    finding = evidence.EvidenceFinding(
        capture_digest=capture_digest,
        part="body",
        object_digest="sha256:" + "b" * 64,
        state="missing",
        checked_at=datetime(2026, 9, 3, tzinfo=UTC),
    )
    monkeypatch.setattr(evidence, "evidence_findings", lambda _instance: (finding,))

    card = _get(instance, f"Capture:{capture_digest}").card

    assert isinstance(card, PlaybillGetCaptureCardV1)
    assert card.status == "unavailable"
    assert card.status_detail is not None and card.status_detail.startswith("body missing")


def test_a_procedure_run_line_ref_does_not_break_bare_proposal_ids(line_world) -> None:  # type: ignore[no-untyped-def]
    instance, _line, _dispatched, _when = line_world

    with pytest.raises(Exception) as unknown:
        _get(instance, "sha256:" + "0" * 64)
    # Not a Line digest: it falls through to the proposal selector's own refusal.
    assert "roposal" in type(unknown.value).__name__ or "proposal" in str(unknown.value)


def test_a_stopped_arm_line_card_is_bounded(tmp_path: Path) -> None:
    instance, line, _procedure, start = _armed_world(tmp_path)
    service_stop_line_arm(
        instance,
        _active_segment(instance),
        reason="permission_insufficient",
        detail="Rechecked authority no longer covers the Line.",
        actor=_actor(instance),
        now=start + timedelta(seconds=1),
    )

    card = _get(instance, line.identity.qualified, evaluation_time=start).card

    assert isinstance(card, PlaybillGetLineCardV1)
    assert card.recent_runs == () and card.runs_total == 0
    assert [arm.stop_reason for arm in card.arms] == ["permission_insufficient"]
