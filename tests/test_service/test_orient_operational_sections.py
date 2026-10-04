"""orient pages every operational family, counts them in the map, and never inlines them.

``lines``, ``captures``, ``capture_contracts``, ``predictions`` and ``mandates``
(``runs`` is covered with the run reads) are compact typed rows, paged, and
each page suggests a ``get`` of its first row that resolves.
"""

from __future__ import annotations

from typing import Any

import pytest

from cruxible_client.contracts.get_reads import GetRequest
from cruxible_core.service.discovery.get import service_playbill_get
from cruxible_core.service.discovery.orient import service_playbill_orient
from cruxible_core.service.list_pages import ListCursorMismatch
from cruxible_core.storage.cas import BodyAccessContext
from tests.test_service.test_operational_get import line_world, prediction_world  # noqa: F401

_ACCESS = BodyAccessContext(principal_id="reader", can_read_body=True)


def _suggested_get(instance: Any, call: str) -> None:
    """Run an MCP-rendered ``get`` suggestion against the service."""

    assert call.startswith('cruxible_get(ref="'), call
    ref = call.split('ref="', 1)[1].split('"', 1)[0]
    service_playbill_get(instance, request=GetRequest(ref=ref), access=_ACCESS)


def test_the_lines_and_mandates_sections_carry_arm_state_and_validity(line_world) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    instance, line, _dispatched, when = line_world

    lines = service_playbill_orient(instance, section="lines", evaluation_time=when, surface="mcp")
    assert lines.section == "lines" and lines.lines is not None
    (row,) = lines.lines
    assert row.line == line.identity.qualified and row.arm == "stopped"
    assert row.trigger == "capture_landing" and row.lifecycle == "live"
    _suggested_get(instance, lines.next[0])

    mandates = service_playbill_orient(
        instance, section="mandates", evaluation_time=when, surface="mcp"
    )
    assert mandates.mandates is not None
    (mandate,) = mandates.mandates
    assert mandate.mandate == "ProcedureMandate:served-line-mandate"
    assert mandate.state == "active" and mandate.grants == "propose"
    assert mandates.next[0] == 'cruxible_get(ref="Mandate:served-line-mandate")'
    _suggested_get(instance, mandates.next[0])

    contracts = service_playbill_orient(instance, section="capture_contracts", surface="mcp")
    assert contracts.capture_contracts
    assert all(item.contract.startswith("CaptureContract:") for item in contracts.capture_contracts)
    _suggested_get(instance, contracts.next[0])


def test_the_default_map_counts_operational_families_without_inlining_them(line_world) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    instance, _line, dispatched, when = line_world

    answer = service_playbill_orient(instance, evaluation_time=when, surface="cli")

    assert answer.artifacts is not None
    counts = answer.artifacts
    assert counts.lines == 1 and counts.mandates == 1 and counts.capture_contracts >= 1
    assert (
        counts.runs == len([item for item in dispatched.items if item.run_id])
        and counts.running == 0
    )
    for section in ("runs", "lines", "captures", "capture_contracts", "predictions", "mandates"):
        assert getattr(answer, section) is None, section
    assert "cruxible orient --section lines" in answer.next


def test_the_predictions_and_captures_sections(prediction_world) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    instance, capture_digest, contract, when = prediction_world

    predictions = service_playbill_orient(
        instance, section="predictions", evaluation_time=when, surface="mcp"
    )
    assert predictions.predictions is not None
    (row,) = predictions.predictions
    assert row.contract == contract.identity.qualified
    assert (row.open, row.settleable, row.resolved) == (0, 1, 0)
    assert row.window == "fixed" and row.hypothesis.startswith("CLM-")
    _suggested_get(instance, predictions.next[0])

    captures = service_playbill_orient(instance, section="captures", surface="mcp")
    assert captures.captures
    handles = [item.capture for item in captures.captures]
    assert "CAP-" + capture_digest.removeprefix("sha256:")[:12] in handles
    observed = [item.observed_at for item in captures.captures]
    assert observed == sorted(observed, reverse=True)
    _suggested_get(instance, captures.next[0])


def test_captures_page_by_key_and_refuse_a_foreign_cursor(prediction_world) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    instance, _capture, _contract, _when = prediction_world
    whole = service_playbill_orient(instance, section="captures")
    assert whole.captures is not None
    if len(whole.captures) < 2:
        pytest.skip("the world cites a single Capture")

    walked = []
    cursor = None
    while True:
        page = service_playbill_orient(instance, section="captures", limit=1, cursor=cursor)
        assert page.captures is not None
        walked.extend(item.capture for item in page.captures)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert walked == [item.capture for item in whole.captures]

    first = service_playbill_orient(instance, section="captures", limit=1)
    with pytest.raises(ListCursorMismatch):
        service_playbill_orient(instance, section="runs", cursor=first.next_cursor)


def test_the_lines_section_says_its_arm_state_is_live(line_world) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    instance, _line, _dispatched, when = line_world

    answer = service_playbill_orient(instance, section="lines", evaluation_time=when)

    assert answer.live is not None and answer.live.fields[0] == "lines.arm"
