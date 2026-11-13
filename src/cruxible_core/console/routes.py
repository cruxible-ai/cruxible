"""The daemon routes the console listener forwards: reads only, by method and path.

This is the console's own read allow-list (``src/api/requests.ts`` READ_ROUTES in
the console repository), ported entry for entry. Several reads are POSTs (``get``,
``get-batch``, ``query``, ``next``, ``since``, proposal review, Procedure
readings), so "GET only" is not the rule; this table is. Anything not on it is
refused by the listener with 403 and never reaches the daemon.

Routes that start runs, run Runbooks, record measurements or dispositions, or
evaluate Lines are absent on purpose. ``tests/test_console/test_console_routes.py``
sends every entry to a daemon fixed at the READ_ONLY capability ceiling and
checks that each one passes only READ_ONLY permission checks, so a route that
the daemon classifies as a write cannot stay on this list.
"""

from __future__ import annotations

import re
from urllib.parse import unquote

#: (method, path template). ``{name}`` matches exactly one non-empty path segment.
READ_ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", "/api/v1/{instance_id}/head"),
    ("GET", "/api/v1/{instance_id}/orient"),
    ("GET", "/api/v1/{instance_id}/kits"),
    ("GET", "/api/v1/{instance_id}/proposals"),
    ("GET", "/api/v1/{instance_id}/inbox"),
    ("GET", "/api/v1/{instance_id}/inbox/{item_id}"),
    ("GET", "/api/v1/{instance_id}/procedure-runs/{run_id}"),
    ("POST", "/api/v1/{instance_id}/get"),
    ("POST", "/api/v1/{instance_id}/get-batch"),
    ("POST", "/api/v1/{instance_id}/query"),
    ("POST", "/api/v1/{instance_id}/next"),
    ("POST", "/api/v1/{instance_id}/claims/read-batch"),
    ("POST", "/api/v1/{instance_id}/since"),
    ("POST", "/api/v1/{instance_id}/captures/read"),
    ("POST", "/api/v1/{instance_id}/proposals/{proposal_id}/review"),
    ("POST", "/api/v1/{instance_id}/procedures/{name}/readings"),
    ("GET", "/api/v1/{instance_id}/runbooks/{name}/preview"),
    ("GET", "/version"),
    ("GET", "/api/v1/{instance_id}/host"),
    ("GET", "/api/v1/{instance_id}/whoami"),
    ("GET", "/api/v1/{instance_id}/providers"),
)

#: Characters a forwarded path segment may hold: RFC 3986 unreserved, sub-delims,
#: ``:``, ``@`` and percent escapes. No ``?``/``#`` (the query string travels apart).
_SEGMENT = re.compile(r"^[A-Za-z0-9\-._~!$&'()*+,;=:@%]+$")
#: Escapes that would let one raw segment become several (or none) once decoded.
_SPLITTING_ESCAPE = re.compile(r"%(2[fF]|5[cC]|00)")


def _template_parts(template: str) -> tuple[str, ...]:
    return tuple(template.strip("/").split("/"))


_TEMPLATES: dict[str, list[tuple[str, ...]]] = {}
for _method, _template in READ_ROUTES:
    _TEMPLATES.setdefault(_method, []).append(_template_parts(_template))


def forwardable_path(raw_path: str) -> bool:
    """Whether a raw request path is plain enough to forward unchanged.

    The daemon routes on the decoded path, so a path is forwarded only when
    decoding cannot change its segments: no empty, ``.`` or ``..`` segment, no
    escaped slash, backslash or NUL, nothing outside the segment alphabet.
    """

    if not raw_path.startswith("/") or _SPLITTING_ESCAPE.search(raw_path):
        return False
    return all(
        _SEGMENT.match(part) and unquote(part) not in ("", ".", "..")
        for part in raw_path[1:].split("/")
    )


def is_read_route(method: str, raw_path: str) -> bool:
    """Whether ``method raw_path`` is one of the console's read routes."""

    if not forwardable_path(raw_path):
        return False
    parts = tuple(raw_path[1:].split("/"))
    for template in _TEMPLATES.get(method.upper(), ()):
        if len(template) == len(parts) and all(
            (want.startswith("{") and want.endswith("}")) or want == got
            for want, got in zip(template, parts)
        ):
            return True
    return False
