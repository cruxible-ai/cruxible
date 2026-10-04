"""The write fence every exact preview runs behind (rule R12).

A preview takes the same path as the change it previews, up to the commit, and
writes nothing anywhere. The bodies it stores are held in memory
(``dry_run_bodies``) and its history reads never catch an index up on disk
(``detached_history_reads``); this fence is the third guard. Inside
``previewing()`` every write door that checks it raises instead of writing, so
a preview path that reached a commit by mistake fails loudly rather than
changing state. The doors: proposal publication, the instance descriptor,
ledger activation, the runtime credential store, the instance registry, the
Line dispatch journal and Provider installation.

The context is per call (a context variable), so a concurrent real write in
another request is unaffected.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_PREVIEWING: ContextVar[bool] = ContextVar("cruxible_previewing", default=False)


class PreviewWriteRefused(RuntimeError):
    """A preview reached a write door. Always a defect in the preview path."""

    error_code = "cruxible.preview.write_reached"

    def __init__(self, door: str) -> None:
        self.door = door
        super().__init__(
            f"{self.error_code}: a preview reached the {door} write; previews write nothing"
        )


@contextmanager
def previewing() -> Iterator[None]:
    """Run the enclosed preview with every fenced write door closed."""

    token = _PREVIEWING.set(True)
    try:
        yield
    finally:
        _PREVIEWING.reset(token)


def is_previewing() -> bool:
    return _PREVIEWING.get()


def refuse_write_while_previewing(door: str) -> None:
    """Called by a write door before its first write; raises inside a preview."""

    if _PREVIEWING.get():
        raise PreviewWriteRefused(door)


__all__ = [
    "PreviewWriteRefused",
    "is_previewing",
    "previewing",
    "refuse_write_while_previewing",
]
