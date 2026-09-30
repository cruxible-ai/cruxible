"""A local operator request authenticated by a MAC, so the bootstrap secret never travels.

A local lifecycle command (``server status``, ``restart``, ``stop``) that has no
bearer token configured reads its daemon's bootstrap secret from the state root
and signs each request with it instead of sending it. The MAC is keyed by the
secret and covers the method, the path and query, a digest of the body, a fresh
nonce and a timestamp, under its own domain. The daemon recomputes it, checks
the timestamp against its clock, and refuses a nonce it has already seen. A
relay without the secret cannot produce one, a process that took over the
endpoint receives nothing it could reuse, and the daemon's boot id in the
signed form keeps a request from outliving the process image it was made for.
"""

from __future__ import annotations

import hashlib
import hmac

OPERATOR_MAC_HEADER = "X-Cruxible-Operator-Mac"
OPERATOR_NONCE_HEADER = "X-Cruxible-Operator-Nonce"
OPERATOR_TIMESTAMP_HEADER = "X-Cruxible-Operator-Timestamp"
#: The daemon process-image boot id the request is signed for.
OPERATOR_BOOT_HEADER = "X-Cruxible-Operator-Boot"

#: How far the signed timestamp may sit from the daemon's clock, in seconds.
OPERATOR_MAC_MAX_SKEW_SECONDS = 60

_DOMAIN = b"cruxible-operator-request-v1"


def operator_request_mac(
    secret: str,
    *,
    method: str,
    path: str,
    query: str,
    body: bytes,
    nonce: str,
    timestamp: str,
    boot_id: str,
) -> str:
    """The hex HMAC-SHA256 over one request's exact canonical form.

    ``boot_id`` is the unpredictable boot id of the daemon image the request is
    for, so a request captured before an in-place restart (which also clears
    the daemon's replay cache) is refused by the image after it.
    """

    canonical = b"\n".join(
        (
            _DOMAIN,
            method.upper().encode("ascii"),
            path.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256(body).hexdigest().encode("ascii"),
            nonce.encode("ascii"),
            timestamp.encode("ascii"),
            boot_id.encode("ascii"),
        )
    )
    return hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()


__all__ = [
    "OPERATOR_BOOT_HEADER",
    "OPERATOR_MAC_HEADER",
    "OPERATOR_MAC_MAX_SKEW_SECONDS",
    "OPERATOR_NONCE_HEADER",
    "OPERATOR_TIMESTAMP_HEADER",
    "operator_request_mac",
]
