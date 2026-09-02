"""One client/daemon authoring-contract compatibility law."""

from __future__ import annotations

from typing import Protocol

from cruxible_client import __version__
from cruxible_client.authoring.sdk_types import IncompatibleDaemonVersion
from cruxible_client.contracts.authoring.models import AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST


class _VersionProbe(Protocol):
    def _version_info(self) -> tuple[str, str | None]: ...


def check_daemon_compatibility(client: _VersionProbe) -> None:
    """Refuse when the credential-free daemon probe reports another wire digest."""

    daemon_version, daemon_digest = client._version_info()
    expected = AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST
    if daemon_digest != expected:
        raise IncompatibleDaemonVersion(
            client_version=__version__,
            daemon_version=daemon_version,
            expected_snapshot_digest=expected,
            actual_snapshot_digest=daemon_digest if daemon_digest is not None else "missing",
        )


__all__ = ["check_daemon_compatibility"]
