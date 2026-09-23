"""A draft submits in one request, and the daemon preflights it exactly once."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_client import Playbill
from cruxible_client.transport.http import CruxibleClient
from cruxible_core.authoring import coordinator as authoring_coordinator
from tests.test_server.test_playbill_change_set_surfaces import (
    _add_parity_claim,
    _claim_type,
    _shell,
)


def _playbill(http: TestClient, instance_id: str, workspace: Path) -> tuple[Playbill, list[str]]:
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]
    requests: list[str] = []
    send = http.request

    def recorded(method: str, url: str, *args: object, **kwargs: object) -> object:
        requests.append(f"{method} {url.split('/playbill/', 1)[-1]}")
        return send(method, url, *args, **kwargs)  # type: ignore[arg-type]

    http.request = recorded  # type: ignore[method-assign]
    workspace.mkdir(exist_ok=True)
    pb = Playbill._from_client(transport, instance_id=instance_id, workspace=workspace)
    return pb, requests


def _count_preflights(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    compute = authoring_coordinator.compute_preflight

    def counted(instance, *, intent, **kwargs):
        calls.append(intent.intent_id)
        return compute(instance, intent=intent, **kwargs)

    monkeypatch.setattr(authoring_coordinator, "compute_preflight", counted)
    return calls


def test_draft_submit_is_one_request_with_one_preflight(
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    http, instance_id, _key = playbill_http
    pb, requests = _playbill(http, instance_id, tmp_path / "world")
    preflights = _count_preflights(monkeypatch)
    draft = pb.changes(rationale="Open the parity slot and state its first value.")
    draft.subject(_shell())
    draft.claim_type(_claim_type())
    _add_parity_claim(draft)
    requests.clear()

    submitted = draft.submit()

    assert requests == ["POST authoring/submit"]
    assert preflights == [submitted.intent_id]
    assert not submitted.refused, submitted.diagnostics
    assert submitted._candidate_status is not None
    assert submitted._candidate_status.proposal_id is not None
    assert submitted._raw["change_set_claim_identities"]
    # The bound preflight is the one this submit ran, lint included.
    assert submitted._preflight is not None
    assert submitted._preflight.verdict == "passed"
    assert submitted._preflight.certificate == submitted._raw["last_preflight"]["certificate"]


def test_staged_submit_costs_two_preflights_and_a_replay_costs_none(
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    http, instance_id, _key = playbill_http
    pb, requests = _playbill(http, instance_id, tmp_path / "world")
    preflights = _count_preflights(monkeypatch)

    def draft():
        changes = pb.changes(rationale="Open the parity slot and state its first value.")
        changes.subject(_shell())
        changes.claim_type(_claim_type())
        _add_parity_claim(changes)
        return changes

    first = draft()
    requests.clear()
    staged = first.prepare().submit()
    staged_requests, staged_preflights = list(requests), len(preflights)
    second = draft()
    requests.clear()
    preflights.clear()
    # The same content again resolves to the same intent, already submitted at
    # this head: one request, no preflight, the same proposal.
    once = second.submit()

    assert once._candidate_status is not None and staged._candidate_status is not None
    assert once._candidate_status.proposal_id == staged._candidate_status.proposal_id
    assert once._candidate_status.state == staged._candidate_status.state
    assert (len(staged_requests), staged_preflights) == (3, 2), staged_requests
    assert (requests, len(preflights)) == (["POST authoring/submit"], 0)


def test_a_refused_one_call_submit_reports_what_prepare_would(
    playbill_http: tuple[TestClient, str, Path],
    tmp_path: Path,
) -> None:
    http, instance_id, _key = playbill_http
    pb, _requests = _playbill(http, instance_id, tmp_path / "world")
    # A Claim whose Subject and ClaimType exist nowhere refuses at preflight.
    lone = pb.changes(rationale="State a value for a slot that was never opened.")
    _add_parity_claim(lone)
    prepared = lone.prepare()
    assert prepared.refused

    refused = lone.submit()

    assert refused.refused
    assert refused.diagnostics == prepared.diagnostics
    assert refused._candidate_status is not None
    assert refused._candidate_status.proposal_id is None
