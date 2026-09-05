"""The grammar of a ledger mirror URL, and the two refusals it can produce.

Client-side because the descriptor is: `PlaybillDescriptor.mirror_url` validates
through this function, so a URL that could never be pushed to cannot be written
into an instance in the first place, and every door -- init, `ledger set-mirror`,
a hand-edited descriptor a daemon reopens -- refuses it identically.
"""

from __future__ import annotations

import re
from typing import Final

from cruxible_client.contracts.errors import PlaybillFormatError

MIRROR_URL_MAX_LENGTH: Final = 2048

LEDGER_MIRROR_CREDENTIAL_ENV: Final = "CRUXIBLE_PLAYBILL_MIRROR_TOKEN"
"""The one place a mirror credential is read from: the daemon's own environment.

Named rather than discovered. It is used as the HTTP Basic password under the
``x-access-token`` username, which is what every hosted Git forge accepts for a
scoped token, and it is never written to configuration of any kind.
"""

_HTTPS_RE: Final = re.compile(r"^https://[A-Za-z0-9._~@-]+(?::[0-9]{1,5})?(?:/[^\s]*)?$")
_SSH_RE: Final = re.compile(
    r"^ssh://(?:[A-Za-z0-9._~+-]+@)?[A-Za-z0-9._~-]+(?::[0-9]{1,5})?(?:/[^\s]*)?$"
)
_SCP_RE: Final = re.compile(r"^[A-Za-z0-9._~+-]+@[A-Za-z0-9._~-]+:[^\s]+$")
_FILE_RE: Final = re.compile(r"^file:///[^\s]*$")
_ABSOLUTE_PATH_RE: Final = re.compile(r"^/[^\s:]*$")


class PlaybillLedgerMirrorUrlInvalid(PlaybillFormatError):
    """A proposed mirror URL is not a transport this daemon will push to."""

    error_code = "playbill.ledger.mirror_url_invalid"

    def __init__(self, detail: str) -> None:
        self.repair_commands = ("cruxible playbill ledger set-mirror <url>",)
        super().__init__(f"{self.error_code}: {detail}")


class PlaybillLedgerMirrorUnset(PlaybillFormatError):
    """This instance publishes its ledger nowhere, so there is no URL to print."""

    error_code = "playbill.ledger.mirror_unset"

    def __init__(self) -> None:
        self.repair_commands = ("cruxible playbill ledger set-mirror <url>",)
        super().__init__(
            f"{self.error_code}: this instance has no ledger mirror; set one with "
            "`cruxible playbill ledger set-mirror <url>`"
        )


def validate_mirror_url(value: str) -> str:
    """Refuse anything but a plain, credential-free remote a daemon may push to.

    An allowlist rather than a deny-list, because a Git remote is not merely an
    address: ``ext::`` hands Git a shell command to run, and a leading dash
    reaches ``git push`` as an option rather than a URL. Both are code execution
    in the daemon, reachable by whoever may write operational configuration.
    Only four shapes pass -- HTTPS, SSH, an absolute local path, and its
    ``file://`` spelling -- and each is matched whole.

    HTTPS carrying userinfo is refused rather than accepted-and-stripped. A URL
    with a token in it is a secret that has already been written down, and
    ``ledger clone-url`` prints this string back to anyone who may read the
    instance. Plain HTTP is refused for the same reason: the credential the
    daemon supplies would cross the wire in the clear.
    """

    if value != value.strip() or not value:
        raise PlaybillLedgerMirrorUrlInvalid("mirror URL must be nonblank and already normalized")
    if len(value) > MIRROR_URL_MAX_LENGTH:
        raise PlaybillLedgerMirrorUrlInvalid(
            f"mirror URL exceeds {MIRROR_URL_MAX_LENGTH} characters"
        )
    if any(character.isspace() or ord(character) < 0x20 for character in value):
        raise PlaybillLedgerMirrorUrlInvalid("mirror URL must not contain whitespace or controls")
    if value.startswith("-"):
        raise PlaybillLedgerMirrorUrlInvalid("mirror URL must not begin with an option dash")
    if value.startswith("http://"):
        raise PlaybillLedgerMirrorUrlInvalid(
            "mirror URL must be https, ssh, or a local path; plain http would send the "
            "daemon's credential in the clear"
        )
    if _HTTPS_RE.fullmatch(value) is not None:
        if "@" in value:
            raise PlaybillLedgerMirrorUrlInvalid(
                "mirror URL must not embed credentials; the daemon reads its token from "
                f"{LEDGER_MIRROR_CREDENTIAL_ENV}"
            )
        return value
    if _SSH_RE.fullmatch(value) is not None or _SCP_RE.fullmatch(value) is not None:
        authority = value.removeprefix("ssh://").split("/")[0]
        if authority.count("@") > 1 or ":" in authority.split("@")[0]:
            raise PlaybillLedgerMirrorUrlInvalid(
                "mirror URL must not embed credentials; SSH authenticates as the daemon itself"
            )
        return value
    if _FILE_RE.fullmatch(value) is not None or _ABSOLUTE_PATH_RE.fullmatch(value) is not None:
        if ".." in value.split("/"):
            raise PlaybillLedgerMirrorUrlInvalid("local mirror path must not traverse upwards")
        return value
    raise PlaybillLedgerMirrorUrlInvalid(
        "mirror URL must be https://, ssh://, user@host:path, file:///path, or an absolute path"
    )


__all__ = [
    "LEDGER_MIRROR_CREDENTIAL_ENV",
    "MIRROR_URL_MAX_LENGTH",
    "PlaybillLedgerMirrorUnset",
    "PlaybillLedgerMirrorUrlInvalid",
    "validate_mirror_url",
]
