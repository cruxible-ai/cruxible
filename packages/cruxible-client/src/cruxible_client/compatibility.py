"""One client/daemon compatibility law.

The credential-free ``/version`` probe reports the daemon's package version and
its authoring contract snapshot digest. The law:

- same digest: every call proceeds;
- different digest, same ``major.minor`` release series (a 0.5.0 client and a
  0.5.1 daemon): reads proceed, and the transport refuses every write with
  :class:`IncompatibleDaemonVersion` before sending it;
- different series, or no digest reported: the probe refuses outright.

Patch releases keep the read wire compatible; the authoring snapshot pins what
a write may send, so only writes need it to match.
"""

from __future__ import annotations

import os
import re
from typing import Protocol

from cruxible_client import __version__
from cruxible_client.authoring.sdk_types import IncompatibleDaemonVersion
from cruxible_client.contracts.authoring.models import AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST


class _VersionProbe(Protocol):
    def _version_info(self) -> tuple[str, str | None]: ...


#: Development escape hatch: set to 1 to talk to a daemon whose contract differs.
SKIP_VERSION_CHECK_ENV = "CRUXIBLE_SKIP_VERSION_CHECK"

_SERIES = re.compile(r"^(\d+)\.(\d+)(?:\D|$)")


def _release_series(version: str) -> tuple[int, int] | None:
    match = _SERIES.match(version.strip())
    return None if match is None else (int(match.group(1)), int(match.group(2)))


def check_daemon_compatibility(client: _VersionProbe) -> None:
    """Probe ``/version`` once and apply the compatibility law.

    Refuses when the daemon is in another ``major.minor`` series or reports no
    authoring digest. Within one series a differing digest is recorded on the
    client, whose transport then refuses writes (never reads) before sending
    them. ``CRUXIBLE_SKIP_VERSION_CHECK=1`` skips it (development only).
    """

    if os.environ.get(SKIP_VERSION_CHECK_ENV, "").strip().lower() in {"1", "true", "yes", "on"}:
        return
    daemon_version, daemon_digest = client._version_info()
    expected = AUTHORING_SDK_CONTRACT_SNAPSHOT_DIGEST
    if daemon_digest == expected:
        _record_write_refusal(client, None)
        return
    series = _release_series(__version__)
    reads_compatible = (
        daemon_digest is not None
        and series is not None
        and series == _release_series(daemon_version)
    )
    refusal = IncompatibleDaemonVersion(
        client_version=__version__,
        daemon_version=daemon_version,
        expected_snapshot_digest=expected,
        actual_snapshot_digest=daemon_digest if daemon_digest is not None else "missing",
        daemon_label=_daemon_label(client),
        reads_compatible=reads_compatible,
    )
    if not reads_compatible:
        raise refusal
    _record_write_refusal(client, refusal)


def require_authoring_compatibility(client: object) -> None:
    """Raise the recorded write refusal, for a caller about to act locally first.

    The transport already refuses each write before sending it; a command that
    generates keys or writes files before its first write calls this so a
    refused write leaves nothing behind.
    """

    refusal = getattr(getattr(client, "_client", None), "write_refusal", None)
    if isinstance(refusal, IncompatibleDaemonVersion):
        raise refusal


def _record_write_refusal(client: object, refusal: IncompatibleDaemonVersion | None) -> None:
    transport = getattr(client, "_client", None)
    if transport is not None and hasattr(transport, "write_refusal"):
        transport.write_refusal = refusal


def _daemon_label(client: object) -> str | None:
    """Where the daemon is (``unix:<socket>`` or its URL), when the client knows."""

    target = getattr(getattr(client, "_client", None), "_target", None)
    return target if isinstance(target, str) else None


__all__ = [
    "SKIP_VERSION_CHECK_ENV",
    "check_daemon_compatibility",
    "require_authoring_compatibility",
]
