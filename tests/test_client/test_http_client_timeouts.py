"""Regression tests for client timeout semantics (stranger-test F1/F2)."""

from __future__ import annotations

import httpx
import pytest

from cruxible_client.errors import ServerUnreachableError
from cruxible_client.http_client import CruxibleClient, _default_timeout


def test_default_timeout_is_generous_on_read_and_snappy_on_connect() -> None:
    t = _default_timeout()
    assert t.connect == 5.0
    assert t.read >= 120.0


def test_timeout_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRUXIBLE_CLIENT_TIMEOUT_S", "300")
    assert _default_timeout().read == 300.0


def test_read_timeout_message_admits_the_request_may_have_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = CruxibleClient(base_url="http://127.0.0.1:9")

    def raise_read_timeout(*args: object, **kwargs: object) -> httpx.Response:
        raise httpx.ReadTimeout("read timeout")

    monkeypatch.setattr(client._client._client, "post", raise_read_timeout)
    with pytest.raises(ServerUnreachableError) as exc_info:
        client._client.post("/anything")
    message = str(exc_info.value)
    assert "may still be running or may already have completed" in message
    assert "could not reach" in message
    assert "CRUXIBLE_CLIENT_TIMEOUT_S" in message


def test_connect_error_message_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    client = CruxibleClient(base_url="http://127.0.0.1:9")

    def raise_connect_error(*args: object, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(client._client._client, "get", raise_connect_error)
    with pytest.raises(ServerUnreachableError) as exc_info:
        client._client.get("/anything")
    assert "may still be running" not in str(exc_info.value)
