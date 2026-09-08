"""Disposable exact-byte derivations shared by served proposal evaluations."""

from __future__ import annotations

import copy
import threading
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager, nullcontext

from cruxible_client.contracts.canonical import semantic_projection
from cruxible_client.contracts.errors import PlaybillError
from cruxible_core.playbill.derived_state import SnapshotTree
from cruxible_core.playbill.proposals import (
    EvaluatedTreeState,
    advance_member_delta,
    advance_tree_members,
    advance_tree_state,
    build_tree_state,
    detach_tree_state,
)


class EvaluationStateCache:
    """Retain one semantic tree and its incrementally maintained dependency index.

    No coordinate or caller-supplied state authorizes reuse: exact semantic bytes
    do. The existing cold builder remains the oracle. We retain no law results,
    body checks, approvals or query facts. Returned states are detached because
    frozen dataclasses may still contain mutable mappings and nested models.

    Bounds account for retained input bytes/path text, not Python heap usage.
    Oversized trees use the cold path and discard the previous retained tree.
    """

    def __init__(
        self,
        *,
        max_members: int = 16_384,
        max_input_bytes: int = 32 * 1024 * 1024,
        build_context: Callable[[object], AbstractContextManager[None]] | None = None,
    ):
        if max_members < 0 or max_input_bytes < 0:
            raise ValueError("evaluation cache limits must be nonnegative")
        self._build_context = build_context or (lambda key: nullcontext())
        self._max_members = max_members
        self._max_input_bytes = max_input_bytes
        self._tree: dict[str, bytes] | None = None
        self._state: EvaluatedTreeState | None = None
        self._lock = threading.Lock()

    def derive(self, tree: Mapping[str, bytes]) -> EvaluatedTreeState:
        if isinstance(tree, SnapshotTree):
            if tree._evaluation is not None:
                return self._derive_snapshot(tree)
            with self._build_context(("evaluation", id(tree))):
                return self._derive_snapshot(tree)
        # This is the same projection the cold builder consumes. History and
        # candidate cards are not dependencies of this particular derivation.
        projected = semantic_projection(tree)
        retain = (
            self._max_members > 0
            and self._max_input_bytes > 0
            and len(projected) <= self._max_members
            and sum(len(path.encode("utf-8")) + len(body) for path, body in projected.items())
            <= self._max_input_bytes
        )
        with self._lock:
            if not retain:
                self._tree = None
                self._state = None
                return build_tree_state(projected)
            if self._tree is None or self._state is None:
                state = build_tree_state(projected)
            elif projected == self._tree:
                return copy.deepcopy(self._state)
            else:
                try:
                    advanced = advance_tree_members(
                        self._state, previous_tree=self._tree, tree=projected
                    )
                    state = advance_tree_state(self._state, tree=projected, advanced=advanced)
                except (PlaybillError, ValueError):
                    # Incremental parsing can encounter a duplicate identity
                    # before a later malformed member. Let the cold oracle
                    # determine the same refusal and precedence as before.
                    state = build_tree_state(projected)
            # Publish only after complete derivation and detachment succeed.
            detached = copy.deepcopy(state)
            self._tree = projected
            self._state = state
            return detached

    def _derive_snapshot(self, tree: SnapshotTree) -> EvaluatedTreeState:
        # Accepted and candidate snapshots keep separate roots. No candidate can
        # evict the accepted derivation. Returned row mappings detach on access.
        if (
            not self._max_members
            or not self._max_input_bytes
            or tree._semantic_members > self._max_members
            or tree._semantic_bytes > self._max_input_bytes
        ):
            return build_tree_state(tree)
        with tree._lock:
            if tree._evaluation is None:
                if tree._evaluation_seed is not None:
                    parent, edits = tree._evaluation_seed
                    try:
                        advanced = advance_member_delta(parent, edits)
                        state = advance_tree_state(parent, tree=tree, advanced=advanced)
                    except (PlaybillError, ValueError):
                        state = build_tree_state(tree)
                elif tree._parent is None or (
                    not tree._parent._accepted and tree._parent._evaluation is None
                ):
                    state = build_tree_state(tree)
                else:
                    parent = self._derive_snapshot(tree._parent)
                    try:
                        advanced = advance_tree_members(
                            parent, previous_tree=tree._parent, tree=tree
                        )
                        state = advance_tree_state(parent, tree=tree, advanced=advanced)
                    except (PlaybillError, ValueError):
                        state = build_tree_state(tree)
                tree._evaluation = state
            return detach_tree_state(tree._evaluation)

    def clear(self) -> None:
        with self._lock:
            self._tree = None
            self._state = None
