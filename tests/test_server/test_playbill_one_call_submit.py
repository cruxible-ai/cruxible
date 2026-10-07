"""A draft submits in one request, and the daemon preflights it exactly once."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_client import Cruxible
from cruxible_client.transport.http import CruxibleClient
from cruxible_core.authoring import coordinator as authoring_coordinator
from tests.test_server.test_playbill_change_set_surfaces import (
    _add_parity_claim,
    _claim_type,
    _shell,
)


def _playbill(http: TestClient, instance_id: str, workspace: Path) -> tuple[Cruxible, list[str]]:
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]
    requests: list[str] = []
    send = http.request

    def recorded(method: str, url: str, *args: object, **kwargs: object) -> object:
        requests.append(f"{method} {url.split(f'/{instance_id}/', 1)[-1]}")
        return send(method, url, *args, **kwargs)  # type: ignore[arg-type]

    http.request = recorded  # type: ignore[method-assign]
    workspace.mkdir(exist_ok=True)
    pb = Cruxible._from_client(transport, instance_id=instance_id, workspace=workspace)
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
    assert (
        submitted._preflight.certificate.model_dump(mode="json")
        == submitted._raw["last_preflight"]["certificate"]
    )


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


def _change_set_input() -> dict[str, object]:
    from cruxible_client.contracts.authoring.inputs import (
        ChangeSetInput,
        ClaimTypeInput,
        SubjectInput,
    )

    return ChangeSetInput(
        kind="change_set",
        members=(
            SubjectInput(kind="subject", subject=_shell()),
            ClaimTypeInput(kind="claim_type", claim_type=_claim_type()),
        ),
        rationale="Define the parity Subject and its ClaimType.",
    ).model_dump(mode="json")


def test_a_tagless_input_submits_in_one_call_and_a_dry_run_saves_nothing(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    """`authoring submit PAYLOAD` is compile and submit; `--dry-run` is its preflight alone."""

    http, instance_id, _key = playbill_http
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]
    payload = _change_set_input()

    previewed = transport.preview_authoring_input(instance_id, input=payload)

    assert previewed.verdict == "passed", previewed.frontier
    assert transport.list_pending_authoring_intents(instance_id).intents == []

    submitted = transport.submit_authoring_input(instance_id, input=payload)

    assert submitted.status.proposal_id is not None
    assert submitted.preflight is not None and submitted.preflight.verdict == "passed"
    assert str(submitted.intent["semantic_identity"]).startswith("ChangeSet:")
    # The preview ran the same checks over the same lowered payload.
    assert submitted.preflight.certificate.payload_digest == previewed.certificate.payload_digest


def test_a_refused_dry_run_returns_every_refusal_and_saves_no_intent(
    playbill_http: tuple[TestClient, str, Path],
) -> None:
    from cruxible_client.contracts.authoring.inputs import ChangeSetInput
    from tests.test_server.test_playbill_change_set_surfaces import _claim_input

    http, instance_id, _key = playbill_http
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]
    # A Claim whose Subject and ClaimType exist nowhere refuses at preflight.
    lone = ChangeSetInput(
        kind="change_set",
        members=(_claim_input(),),
        rationale="State a value for a slot that was never opened.",
    ).model_dump(mode="json")

    previewed = transport.preview_authoring_input(instance_id, input=lone)

    assert previewed.verdict == "refused"
    assert previewed.frontier.diagnostics
    assert transport.list_pending_authoring_intents(instance_id).intents == []
    # The submit refuses with the same diagnostics, and keeps the intent it made.
    refused = transport.submit_authoring_input(instance_id, input=lone)
    assert refused.status.proposal_id is None
    assert refused.preflight is not None
    assert [item.code for item in refused.preflight.frontier.diagnostics] == [
        item.code for item in previewed.frontier.diagnostics
    ]


def test_a_dry_run_takes_no_intent_id(playbill_http: tuple[TestClient, str, Path]) -> None:
    http, instance_id, _key = playbill_http

    response = http.post(
        f"/api/v1/{instance_id}/authoring/submit",
        json={
            "tag": "playbill-authoring-input-submit-request-v1",
            "input": _change_set_input(),
            "intent_id": "AIT-" + "1" * 32,
            "dry_run": True,
        },
    )

    assert response.status_code == 422
