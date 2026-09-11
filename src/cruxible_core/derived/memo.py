"""Bounded in-process memos that tolerate a concurrent invalidation.

Every read route is an ``async def`` handler and therefore serialized on the
event loop, but activation is not: ``activate_proposal`` is a sync handler, so
Starlette runs it in the anyio worker threadpool, and it calls
``PlaybillInstance.refresh()`` which clears the instance memos outright. A read
that finds an entry and then promotes it in a second statement can be preempted
between the two and raise ``KeyError`` on an otherwise valid read; an insert
that trims to capacity can find the memo emptied under it and ``popitem`` an
empty dict. Neither corrupts anything, but both surface as a server error where
a cold read was the correct answer.

The two helpers here make each memo access one critical section, so a lost race
reads as a miss and the caller simply does the work again. One process-wide lock
serves every memo: the operations under it are O(1) dictionary moves, and the
memos they guard are read a few dozen times per request.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import TypeVar

_MEMO_LOCK = threading.Lock()

KeyT = TypeVar("KeyT")
ValueT = TypeVar("ValueT")


def memo_get(memo: "OrderedDict[KeyT, ValueT]", key: KeyT) -> ValueT | None:
    """Read one entry and promote it, reporting a miss if it went away."""

    with _MEMO_LOCK:
        try:
            value = memo[key]
            memo.move_to_end(key)
        except KeyError:
            return None
        return value


def memo_put(
    memo: "OrderedDict[KeyT, ValueT]",
    key: KeyT,
    value: ValueT,
    *,
    capacity: int,
) -> None:
    """Insert one entry and evict the coldest, tolerating a concurrent clear."""

    with _MEMO_LOCK:
        memo[key] = value
        memo.move_to_end(key)
        while len(memo) > capacity:
            try:
                memo.popitem(last=False)
            except KeyError:
                # Emptied under the trim by a concurrent clear; nothing to evict.
                return


__all__ = ["memo_get", "memo_put"]
