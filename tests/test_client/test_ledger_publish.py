"""Client barrier transport preserves request and acknowledgment independently."""

import httpx
import pytest

from cruxible_client.transport.http import CruxibleClient


def test_publish_barrier_forwards_timeout_and_parses_acknowledgment(monkeypatch):
    with CruxibleClient(base_url="http://unused.invalid") as client:
        calls = []

        def post(path, *, json):
            calls.append((path, json))
            return httpx.Response(
                200,
                json={
                    "instance_id": "inst_test",
                    "mirror_url": "/tmp/unused.git",
                    "status": "publishing",
                    "requested_sequence": 3,
                    "attempted_sequence": 3,
                    "published_sequence": 2,
                    "wait_sequence": 2,
                    "published_refs": {"refs/heads/main": "a" * 40},
                },
                request=httpx.Request("POST", "http://unused.invalid" + path),
            )

        monkeypatch.setattr(client._client, "post", post)
        result = client.publish_playbill_ledger("inst_test", timeout=0)
        assert calls == [("/api/v1/inst_test/playbill/ledger/publish", {"timeout": 0})]
        assert result.status == "publishing"
        assert result.wait_sequence == result.published_sequence == 2
        assert result.published_refs == {"refs/heads/main": "a" * 40}


@pytest.mark.parametrize("timeout", [-1, 61, True, float("nan"), float("inf")])
def test_client_rejects_invalid_timeout_without_transport(monkeypatch, timeout):
    with CruxibleClient(base_url="http://unused.invalid") as client:
        monkeypatch.setattr(client._client, "post", lambda *a, **k: pytest.fail("transport called"))
        with pytest.raises(ValueError, match="between 0 and 60"):
            client.publish_playbill_ledger("inst_test", timeout=timeout)
