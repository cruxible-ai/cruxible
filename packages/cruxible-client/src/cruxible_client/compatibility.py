"""One client/daemon authoring-contract compatibility law."""

from __future__ import annotations

import os
from typing import Protocol

from cruxible_client import __version__
from cruxible_client.authoring.sdk_types import IncompatibleDaemonVersion
from cruxible_client.contracts.authoring.models import AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST


class _VersionProbe(Protocol):
    def _version_info(self) -> tuple[str, str | None]: ...


#: Development escape hatch: set to 1 to talk to a daemon whose contract differs.
SKIP_VERSION_CHECK_ENV = "CRUXIBLE_SKIP_VERSION_CHECK"


def check_daemon_compatibility(client: _VersionProbe) -> None:
    """Refuse when the credential-free daemon probe reports another wire digest.

    What can break a call is the authoring contract, so the digest decides: a
    different contract snapshot digest refuses before the first real request;
    a different package version with the same digest proceeds silently. One
    ``/version`` request; callers cache the checked client per process.
    ``CRUXIBLE_SKIP_VERSION_CHECK=1`` skips it (development only).
    """

    if os.environ.get(SKIP_VERSION_CHECK_ENV, "").strip().lower() in {"1", "true", "yes", "on"}:
        return
    daemon_version, daemon_digest = client._version_info()
    expected = AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST
    if daemon_digest != expected:
        raise IncompatibleDaemonVersion(
            client_version=__version__,
            daemon_version=daemon_version,
            expected_snapshot_digest=expected,
            actual_snapshot_digest=daemon_digest if daemon_digest is not None else "missing",
            daemon_label=_daemon_label(client),
        )


def _daemon_label(client: object) -> str | None:
    """Where the daemon is (``unix:<socket>`` or its URL), when the client knows."""

    target = getattr(getattr(client, "_client", None), "_target", None)
    return target if isinstance(target, str) else None


__all__ = ["SKIP_VERSION_CHECK_ENV", "check_daemon_compatibility"]
