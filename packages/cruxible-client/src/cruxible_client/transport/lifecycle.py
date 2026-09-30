"""A narrow client for lifecycle operations across authoring-contract skew."""

from __future__ import annotations

from cruxible_client import contracts
from cruxible_client.transport.http import CruxibleClient


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
    ) -> None:
        self._transport = CruxibleClient(base_url=base_url, socket_path=socket_path, token=token)

    def version(self) -> str:
        return self._transport.version()

    def daemon_identity(self) -> tuple[str, str | None]:
        return self._transport.daemon_identity()

    def operator_proof(self, challenge: str) -> str | None:
        return self._transport.operator_proof(challenge)

    def server_info(self) -> contracts.ServerInfoResult:
        return self._transport.server_info()

    def server_restart(self) -> contracts.ServerRestartResult:
        return self._transport.server_restart()

    def server_stop(self) -> contracts.ServerStopResult:
        return self._transport.server_stop()

    def close(self) -> None:
        self._transport.close()
