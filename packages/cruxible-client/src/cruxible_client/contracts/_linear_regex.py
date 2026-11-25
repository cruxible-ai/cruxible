"""A linear-time full matcher for kit recipe parameter patterns.

``kit build`` (in the daemon) and ``kit adopt`` (in the CLI) check caller-supplied
values against a publisher's ``pattern``. Python's ``re`` backtracks, so some
patterns (``(a?){30}a{30}``, ``a*a*a*...b``) take time exponential or high
polynomial in the value. This module compiles the pattern -- parsed by the stdlib
parser ``re.compile`` uses -- into a Thompson NFA and simulates it over a
deduplicated set of states, so a match costs at most ``states x len(value)``
steps whatever the pattern.

Supported: literals, ``.``, character classes (ranges, negation, ``\\d``
``\\w`` ``\\s`` and their negations), alternation, groups, greedy and lazy
repeats (bounded repeats are expanded), ``^`` ``$`` ``\\A`` ``\\Z`` ``\\b``
``\\B``. Refused: backreferences, lookarounds, conditionals, atomic groups,
possessive repeats and flags that change matching (``(?i)``, ``(?s)``,
``(?m)``, ``(?a)``). The expanded NFA is capped at :data:`MAX_STATES`.

For full-match success, greedy and lazy repeats are equivalent, so the result
equals ``re.fullmatch(pattern, value) is not None`` on every supported pattern.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

#: Most NFA states one compiled pattern may hold (after bounded repeats expand).
MAX_STATES = 2048

_CHAR, _SPLIT, _EMPTY, _ASSERT, _MATCH = range(5)

#: Flags that only change parsing, not matching: allowed.
_HARMLESS_FLAGS = re.UNICODE | re.VERBOSE


class LinearPatternError(ValueError):
    """A pattern the linear matcher refuses: names the construct or the budget."""


@dataclass(slots=True)
class _State:
    kind: int
    test: Callable[[str], bool] | None = None
    at: Callable[[str, int], bool] | None = None
    out: int = -1
    out1: int = -1


@dataclass(frozen=True, slots=True)
class LinearPattern:
    """A compiled pattern: its NFA states and start state."""

    states: tuple[_State, ...]
    start: int

    def fullmatch(self, text: str) -> bool:
        states = self.states
        current = self._closure([self.start], text, 0)
        for index, char in enumerate(text):
            stepped = [
                state.out
                for state in (states[number] for number in current)
                if state.kind == _CHAR and state.test is not None and state.test(char)
            ]
            if not stepped:
                return False
            current = self._closure(stepped, text, index + 1)
        return any(states[number].kind == _MATCH for number in current)

    def _closure(self, seeds: list[int], text: str, position: int) -> list[int]:
        """Every state reachable from ``seeds`` by empty moves at ``position``, once each."""

        states = self.states
        seen: set[int] = set()
        found: list[int] = []
        stack = list(reversed(seeds))
        while stack:
            number = stack.pop()
            if number in seen:
                continue
            seen.add(number)
            state = states[number]
            if state.kind == _SPLIT:
                stack.append(state.out1)
                stack.append(state.out)
            elif state.kind == _EMPTY:
                stack.append(state.out)
            elif state.kind == _ASSERT:
                if state.at is not None and state.at(text, position):
                    stack.append(state.out)
            else:
                found.append(number)
        return found


@lru_cache(maxsize=256)
def compile_linear(pattern: str) -> LinearPattern:
    """``pattern`` as a :class:`LinearPattern`; :class:`LinearPatternError` names
    the construct it refuses (or the state budget), ``re.error`` a syntax error."""

    re.compile(pattern)
    parser: Any = importlib.import_module("re._parser")
    parsed = parser.parse(pattern)
    flags = int(parsed.state.flags)
    if flags & ~_HARMLESS_FLAGS:
        raise LinearPatternError("a flag that changes matching ((?i), (?s), (?m), (?a))")
    builder = _Builder()
    start, holes = builder.sequence(parsed)
    match = builder.add(_State(_MATCH))
    builder.patch(holes, match)
    return LinearPattern(tuple(builder.states), start)


def linear_fullmatch(pattern: str, text: str) -> bool:
    """Whether ``pattern`` matches all of ``text``, in time linear in ``text``."""

    return compile_linear(pattern).fullmatch(text)


_Hole = tuple[int, str]


class _Builder:
    def __init__(self) -> None:
        self.states: list[_State] = []

    def add(self, state: _State) -> int:
        if len(self.states) >= MAX_STATES:
            raise LinearPatternError(
                f"more than {MAX_STATES} matcher states (a large bounded repeat); "
                "use a smaller bound or an unbounded repeat"
            )
        self.states.append(state)
        return len(self.states) - 1

    def patch(self, holes: list[_Hole], target: int) -> None:
        for number, slot in holes:
            setattr(self.states[number], slot, target)

    def sequence(self, items: Any) -> tuple[int, list[_Hole]]:
        start = -1
        holes: list[_Hole] = []
        for op, argument in items:
            item_start, item_holes = self.item(str(op), argument)
            if start < 0:
                start = item_start
            else:
                self.patch(holes, item_start)
            holes = item_holes
        if start < 0:
            empty = self.add(_State(_EMPTY))
            return empty, [(empty, "out")]
        return start, holes

    def char(self, test: Callable[[str], bool]) -> tuple[int, list[_Hole]]:
        number = self.add(_State(_CHAR, test=test))
        return number, [(number, "out")]

    def item(self, name: str, argument: Any) -> tuple[int, list[_Hole]]:
        if name == "LITERAL":
            literal = chr(argument)
            return self.char(lambda char: char == literal)
        if name == "NOT_LITERAL":
            excluded = chr(argument)
            return self.char(lambda char: char != excluded)
        if name == "ANY":
            return self.char(lambda char: char != "\n")
        if name == "IN":
            return self.char(_class_test(argument))
        if name == "AT":
            number = self.add(_State(_ASSERT, at=_at_test(str(argument))))
            return number, [(number, "out")]
        if name == "SUBPATTERN":
            _group, add_flags, del_flags, sub_pattern = argument
            if (add_flags | del_flags) & ~_HARMLESS_FLAGS:
                raise LinearPatternError("a flag that changes matching ((?i), (?s), (?m), (?a))")
            return self.sequence(sub_pattern)
        if name == "BRANCH":
            return self.alternation(argument[1])
        if name in {"MAX_REPEAT", "MIN_REPEAT"}:
            low, high, sub_pattern = argument
            return self.repeat(int(low), high, sub_pattern)
        raise LinearPatternError(_REFUSED.get(name, f"an unsupported construct ({name})"))

    def alternation(self, alternatives: Any) -> tuple[int, list[_Hole]]:
        compiled = [self.sequence(alternative) for alternative in alternatives]
        start, holes = compiled[-1]
        for alt_start, alt_holes in reversed(compiled[:-1]):
            start = self.add(_State(_SPLIT, out=alt_start, out1=start))
            holes = alt_holes + holes
        return start, holes

    def optional(self, sub_pattern: Any) -> tuple[int, list[_Hole]]:
        sub_start, sub_holes = self.sequence(sub_pattern)
        split = self.add(_State(_SPLIT, out=sub_start))
        return split, [*sub_holes, (split, "out1")]

    def star(self, sub_pattern: Any) -> tuple[int, list[_Hole]]:
        sub_start, sub_holes = self.sequence(sub_pattern)
        split = self.add(_State(_SPLIT, out=sub_start))
        self.patch(sub_holes, split)
        return split, [(split, "out1")]

    def repeat(self, low: int, high: Any, sub_pattern: Any) -> tuple[int, list[_Hole]]:
        maxrepeat = getattr(importlib.import_module("re._constants"), "MAXREPEAT")
        unbounded = high == maxrepeat
        if low > MAX_STATES or (not unbounded and int(high) > MAX_STATES):
            raise LinearPatternError(f"more than {MAX_STATES} matcher states (a large repeat)")
        if low == 0 and not unbounded and int(high) == 0:
            # ``x{0}`` matches only the empty string, but ``x`` must still be a
            # pattern this matcher runs: ``(?i:a){0}`` refuses like ``(?i:a)``.
            # Compiled into a scratch builder so the unreachable states cost nothing.
            _Builder().sequence(sub_pattern)
            empty = self.add(_State(_EMPTY))
            return empty, [(empty, "out")]
        parts: list[tuple[int, list[_Hole]]] = [self.sequence(sub_pattern) for _ in range(low)]
        if unbounded:
            parts.append(self.star(sub_pattern))
        else:
            optional_parts = int(high) - low
            # ``x{0,3}`` as ``(x(x(x)?)?)?``: each optional copy only after the one before.
            tail: tuple[int, list[_Hole]] | None = None
            for _ in range(optional_parts):
                sub_start, sub_holes = self.sequence(sub_pattern)
                split = self.add(_State(_SPLIT, out=sub_start))
                if tail is None:
                    tail = (split, [*sub_holes, (split, "out1")])
                else:
                    self.patch(sub_holes, tail[0])
                    tail = (split, [*tail[1], (split, "out1")])
            if tail is not None:
                parts.append(tail)
        if not parts:
            empty = self.add(_State(_EMPTY))
            return empty, [(empty, "out")]
        start, holes = parts[0]
        for part_start, part_holes in parts[1:]:
            self.patch(holes, part_start)
            holes = part_holes
        return start, holes


_REFUSED = {
    "GROUPREF": "a backreference",
    "GROUPREF_EXISTS": "a conditional group",
    "ASSERT": "a lookaround",
    "ASSERT_NOT": "a lookaround",
    "ATOMIC_GROUP": "an atomic group",
    "POSSESSIVE_REPEAT": "a possessive repeat",
}


def _is_word(char: str) -> bool:
    return char.isalnum() or char == "_"


_CATEGORIES: dict[str, Callable[[str], bool]] = {
    "CATEGORY_DIGIT": str.isdecimal,
    "CATEGORY_NOT_DIGIT": lambda char: not char.isdecimal(),
    "CATEGORY_SPACE": str.isspace,
    "CATEGORY_NOT_SPACE": lambda char: not char.isspace(),
    "CATEGORY_WORD": _is_word,
    "CATEGORY_NOT_WORD": lambda char: not _is_word(char),
}


def _class_test(items: Any) -> Callable[[str], bool]:
    negate = False
    literals: set[str] = set()
    ranges: list[tuple[str, str]] = []
    categories: list[Callable[[str], bool]] = []
    for op, argument in items:
        name = str(op)
        if name == "NEGATE":
            negate = True
        elif name == "LITERAL":
            literals.add(chr(argument))
        elif name == "RANGE":
            ranges.append((chr(argument[0]), chr(argument[1])))
        elif name == "CATEGORY" and str(argument) in _CATEGORIES:
            categories.append(_CATEGORIES[str(argument)])
        else:
            raise LinearPatternError(f"an unsupported character class item ({name})")
    frozen = frozenset(literals)
    bounds = tuple(ranges)
    tests = tuple(categories)

    def test(char: str) -> bool:
        inside = (
            char in frozen
            or any(low <= char <= high for low, high in bounds)
            or any(category(char) for category in tests)
        )
        return inside != negate

    return test


def _at_test(name: str) -> Callable[[str, int], bool]:
    if name in {"AT_BEGINNING", "AT_BEGINNING_STRING"}:
        return lambda text, position: position == 0
    if name == "AT_END":
        # ``$`` also holds just before a final newline, as in ``re``.
        return lambda text, position: (
            position == len(text) or (position == len(text) - 1 and text[position] == "\n")
        )
    if name == "AT_END_STRING":
        return lambda text, position: position == len(text)
    if name in {"AT_BOUNDARY", "AT_NON_BOUNDARY"}:
        want = name == "AT_BOUNDARY"

        def boundary(text: str, position: int) -> bool:
            if not text:
                # ``re`` (3.13) holds neither ``\b`` nor ``\B`` on an empty string.
                return False
            before = position > 0 and _is_word(text[position - 1])
            after = position < len(text) and _is_word(text[position])
            return (before != after) == want

        return boundary
    raise LinearPatternError(f"an unsupported anchor ({name})")
