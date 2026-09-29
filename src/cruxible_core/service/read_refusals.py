"""Coded refusals for the read verbs: a wrong name names the nearest right ones.

A read never answers a wrong name with an empty result or a server fault. It
refuses with a stable code, the candidates it could have meant, and the one
operation that repairs the call. The message carries all three, so a caller
that only sees prose still sees the repair.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable, Mapping
from typing import Any

from cruxible_client.contracts.repairs import RepairOperationV1
from cruxible_core.errors import CoreError


class ReadRefusalError(CoreError):
    """A read refused for a reason the caller can repair."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 400,
        candidates: Iterable[str] = (),
        repair: RepairOperationV1 | None = None,
        repair_line: str | None = None,
        context: Mapping[str, Any] | None = None,
    ) -> None:
        self.error_code = code
        self.http_status = http_status
        self.candidates = tuple(candidates)
        self.repair = repair
        self.context: dict[str, Any] = dict(context or {})
        if self.candidates:
            self.context["candidates"] = list(self.candidates)
        if repair_line is not None:
            self.context["repair_line"] = repair_line
        text = f"{code}: {message}"
        if self.candidates:
            text += f"; nearest: {', '.join(self.candidates)}"
        if repair_line is not None:
            text += f". {repair_line}"
        super().__init__(text)


def nearest(value: str, names: Iterable[str], *, limit: int = 5) -> tuple[str, ...]:
    """The accepted names a mistyped or shortened one most likely meant.

    A name matches on its whole spelling or on its last segment, so a typo in a
    short predicate (``adoption_stat``) still finds ``dev.roadmap_item.adoption_state``.
    """

    ordered = sorted(set(names))
    by_leaf: dict[str, list[str]] = {}
    for name in ordered:
        leaf = re.split(r"[./:]", name)[-1]
        by_leaf.setdefault(leaf, []).append(name)
    exact_leaf = [name for name in ordered if re.split(r"[./:]", name)[-1] == value]
    close = difflib.get_close_matches(value, ordered, n=limit, cutoff=0.6)
    close_leaf = [
        name
        for leaf in difflib.get_close_matches(value, list(by_leaf), n=limit, cutoff=0.7)
        for name in by_leaf[leaf]
    ]
    return tuple(dict.fromkeys([*exact_leaf, *close, *close_leaf]))[:limit]


__all__ = ["ReadRefusalError", "nearest"]
