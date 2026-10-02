"""Git-oid prefixes for ``at`` tests that never read as generation numbers."""

from __future__ import annotations

from cruxible_core.service.read_refusals import OID_PREFIX_MIN


def too_short_prefix(oid: str) -> str:
    """A prefix of ``oid`` too short to resolve, which ``at`` still reads as a prefix.

    An all-digit value of 11 or fewer characters is a generation number, so the
    prefix must hold a hex letter: the shortest such prefix of 8 to 11
    characters. When the first 11 characters are all digits (about 0.6% of
    oids), a letter-led 8-character value stands in: the too-short refusal is
    decided before any oid is matched.
    """

    for length in range(8, OID_PREFIX_MIN):
        if not oid[:length].isdigit():
            return oid[:length]
    return "a" + oid[1:8]
