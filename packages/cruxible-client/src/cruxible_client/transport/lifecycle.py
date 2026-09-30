"""A narrow client for lifecycle operations across authoring-contract skew."""

from __future__ import annotations

import secrets
import time
from collections.abc import Generator

import httpx

from cruxible_client import contracts
from cruxible_client.contracts.operator_mac import (
    OPERATOR_BOOT_HEADER,
    OPERATOR_MAC_HEADER,
    OPERATOR_NONCE_HEADER,
    OPERATOR_TIMESTAMP_HEADER,
    operator_request_mac,
)
from cruxible_client.transport.http import CruxibleClient


class OperatorRequestSigner(httpx.Auth):
    """Sign each request with a MAC keyed by the daemon's bootstrap secret.

    The secret itself is never sent: the request carries the MAC, a fresh
    nonce and a timestamp, which only a daemon holding the same secret can
    verify, and which it accepts once.
    """

    requires_request_body = True

    def __init__(self, secret: str, *, boot_id: str) -> None:
        self._secret = secret
        self._boot_id = boot_id

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        nonce = secrets.token_hex(16)
        timestamp = str(int(time.time()))
        request.headers[OPERATOR_NONCE_HEADER] = nonce
        request.headers[OPERATOR_TIMESTAMP_HEADER] = timestamp
        request.headers[OPERATOR_BOOT_HEADER] = self._boot_id
        request.headers[OPERATOR_MAC_HEADER] = operator_request_mac(
            self._secret,
            method=request.method,
            path=request.url.path,
            query=request.url.query.decode("ascii"),
            body=request.content,
            nonce=nonce,
            timestamp=timestamp,
            boot_id=self._boot_id,
        )
        yield request


class DaemonLifecycleClient:
    """Expose only daemon lifecycle endpoints, never governed instance reads.

    Composition deliberately keeps this from being a CruxibleClient subtype:
    callers needing instance operations must obtain a compatibility-checked
    client separately. The underlying transport still handles authentication
    and typed lifecycle responses in one place.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        socket_path: str | None = None,
        token: str | None = None,
        operator_secret: str | None = None,
        operator_boot_id: str | None = None,
    ) -> None:
        """``operator_secret`` signs each request with the daemon's bootstrap secret,
        for the daemon image ``operator_boot_id``, instead of sending any
        credential; the secret never goes on the wire."""

        if token is not None and operator_secret is not None:
            raise ValueError("configure a bearer token or an operator secret, not both")
        if (operator_secret is None) != (operator_boot_id is None):
            raise ValueError("an operator secret signs only for a named daemon boot id")
        self._transport = CruxibleClient(base_url=base_url, socket_path=socket_path, token=token)
        if operator_secret is not None and operator_boot_id is not None:
            self._transport._client._client.auth = OperatorRequestSigner(
                operator_secret, boot_id=operator_boot_id
            )

    def version(self) -> str:
        return self._transport.version()

    def daemon_identity(self) -> tuple[str, str | None]:
        return self._transport.daemon_identity()

    def server_info(self) -> contracts.ServerInfoResult:
        return self._transport.server_info()

    def server_restart(self) -> contracts.ServerRestartResult:
        return self._transport.server_restart()

    def server_stop(self) -> contracts.ServerStopResult:
        return self._transport.server_stop()

    def close(self) -> None:
        self._transport.close()
