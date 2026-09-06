"""Publication barrier wiring and authority, without contacting a mirror."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from cruxible_client import contracts
from cruxible_client.contracts.ledger_mirror import PlaybillLedgerMirrorUnset
from cruxible_core.errors import PermissionDeniedError
from cruxible_core.playbill.ledger_mirror import LedgerMirrorStateV1
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.permissions import PermissionMode, request_permission_scope
from cruxible_core.server.playbill_request_models import PlaybillLedgerPublishRequest


def _state(tmp_path):
    return LedgerMirrorStateV1(
        url=str(tmp_path / "unused-test-mirror.git"),
        status="pending",
        attempted_at="2026-09-05T00:00:00Z",
        requested_sequence=9,
        attempted_sequence=8,
        published_sequence=8,
        wait_sequence=8,
        published_main_oid="a" * 40,
        published_refs={"refs/heads/main": "a" * 40},
    )


@pytest.mark.parametrize("timeout", [-1, 61, True, "1", float("nan"), float("inf")])
def test_publish_request_rejects_unbounded_or_coerced_wait(timeout):
    with pytest.raises(ValidationError):
        PlaybillLedgerPublishRequest(timeout=timeout)


def test_publish_request_defaults_and_zero_wait(tmp_path):
    assert PlaybillLedgerPublishRequest().timeout == 60
    assert PlaybillLedgerPublishRequest(timeout=0).timeout == 0
    with pytest.raises(ValidationError):
        PlaybillLedgerPublishRequest(timeout=0, url=str(tmp_path / "not-configurable-here"))


def test_runtime_barrier_preserves_own_acknowledgment_with_newer_request(monkeypatch, tmp_path):
    state = _state(tmp_path)
    calls = []
    instance = SimpleNamespace(
        ledger_mirror_url=lambda: state.url,
        publish_ledger_mirror=lambda **kwargs: calls.append(kwargs) or state,
    )
    monkeypatch.setattr(
        playbill_api, "get_playbill_manager", lambda: SimpleNamespace(get=lambda _: instance)
    )
    with request_permission_scope(PermissionMode.GOVERNED_WRITE):
        receipt = playbill_api.playbill_ledger_publish("inst_test", timeout=0)
    assert calls == [{"timeout": 0}]
    assert receipt.status == "pending"
    assert receipt.wait_sequence == receipt.published_sequence == 8
    assert receipt.requested_sequence == 9
    assert receipt.attempted_sequence == 8
    assert receipt.published_main_oid == "a" * 40
    assert receipt.published_refs == state.published_refs
    assert receipt.published_refs is not state.published_refs


def test_runtime_publish_refuses_read_only_before_loading_instance(monkeypatch):
    def forbidden():
        pytest.fail("instance was loaded before permission check")

    monkeypatch.setattr(playbill_api, "get_playbill_manager", forbidden)
    with request_permission_scope(PermissionMode.READ_ONLY), pytest.raises(PermissionDeniedError):
        playbill_api.playbill_ledger_publish("inst_test")


def test_runtime_publish_requires_preconfigured_destination(monkeypatch):
    instance = SimpleNamespace(ledger_mirror_url=lambda: None)
    monkeypatch.setattr(
        playbill_api, "get_playbill_manager", lambda: SimpleNamespace(get=lambda _: instance)
    )
    with request_permission_scope(PermissionMode.GOVERNED_WRITE):
        with pytest.raises(PlaybillLedgerMirrorUnset):
            playbill_api.playbill_ledger_publish("inst_test")


def test_http_publish_forwards_bounded_wait_and_returns_full_status(
    playbill_http, monkeypatch, tmp_path
):
    client, instance_id, _ = playbill_http
    state = _state(tmp_path)
    receipt = playbill_api._mirror_receipt(instance_id, url=state.url, state=state)
    calls = []

    def publish(instance_id, *, timeout):
        calls.append((instance_id, timeout))
        return receipt

    monkeypatch.setattr(playbill_api, "playbill_ledger_publish", publish)
    path = f"/api/v1/{instance_id}/playbill/ledger/publish"
    result = client.post(path, json={"timeout": 0})
    assert result.status_code == 200, result.text
    assert contracts.PlaybillLedgerMirrorV1.model_validate(result.json()) == receipt
    assert calls == [(instance_id, 0)]
    assert client.post(path, json={"timeout": 61}).status_code == 422
    assert calls == [(instance_id, 0)]
