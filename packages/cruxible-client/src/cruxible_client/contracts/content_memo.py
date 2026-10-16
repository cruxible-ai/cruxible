"""Bounded per-process memos for pure parses and digests of immutable artifacts.

A governed artifact's parse is a pure function of its path, its codec and its
exact bytes, and each of its digests is a pure function of the parsed value.
Within one write or one Line run the same few ClaimTypes, CaptureContracts,
Providers, Subjects and Claims are parsed and digested dozens of times by
different laws and indexes. These memos make each of those a lookup:

- ``ParseMemo`` keys a parse by ``(path, codec, bytes)``: the content itself,
  never a caller's object. A parsed value whose whole model tree is sealable
  (see ``seal``) is sealed once, and that one immutable value is handed to
  every caller. Any other value is kept pickled and every caller gets its own
  detached copy. A refused parse is never remembered, so it refuses again
  exactly as before.
- ``seal`` makes a model tree deeply immutable and remembers which objects it
  sealed. Pydantic's ``frozen`` is shallow: a frozen model's dict and list
  fields can still change in place. Sealing replaces every such container, at
  every depth, with a ``FrozenDict`` or ``FrozenList`` that refuses in-place
  change, and it seals nothing when any model in the tree is not frozen,
  carries extra or private state, or holds a value whose immutability it
  cannot prove. A sealed object therefore cannot change, so nothing derived
  from it can go stale.
- ``sealed_memo`` remembers one pure derivation (a digest, a canonical
  encoding) per sealed object. The lookup is the object's identity checked
  against a weak reference: one dict probe, never a re-canonicalization. Every
  object that is not sealed -- built in code and never sealed, ``model_copy``'d,
  deep-copied, unpickled -- is derived afresh on every call. Copies are never
  sealed, and a copy's containers are plain mutable ones again. ``sealed_derive``
  is the same memo for a derivation that also takes arguments other than the
  object: they join its key, which holds immutable values only.
- ``ValueMemo`` keys a pure function by a key made of immutable values only
  (strings, numbers, tuples of them), never by a model a caller holds.

Every memo is bounded: parses by entries and bytes (least-recently-used), and a
sealed object's derivations live exactly as long as the object (a weak
reference drops them). All are safe to share across threads: every access is
one short critical section or one atomic dict operation.
"""

from __future__ import annotations

import datetime
import decimal
import functools
import pickle
import threading
import uuid
import weakref
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from enum import Enum
from typing import Any, Generic, NoReturn, TypeVar

from pydantic import BaseModel

_V = TypeVar("_V")
_A = TypeVar("_A")

_MISSING = object()


def _refuse(*_args: object, **_kwargs: object) -> NoReturn:
    raise TypeError("a sealed artifact cannot change in place; copy it first")


class FrozenDict(dict[Any, Any]):
    """A dict inside a sealed artifact: reads as a dict, refuses in-place change.

    Every copy (``copy``, ``deepcopy``, ``dict.copy``, pickling) is a plain,
    mutable ``dict`` again.
    """

    __slots__ = ()

    __setitem__ = _refuse
    __delitem__ = _refuse
    __ior__ = _refuse
    clear = _refuse
    pop = _refuse
    popitem = _refuse
    setdefault = _refuse
    update = _refuse

    def __copy__(self) -> dict[Any, Any]:
        return dict(self)

    def __deepcopy__(self, memo: dict[int, object]) -> dict[Any, Any]:
        result: dict[Any, Any] = {}
        memo[id(self)] = result
        for key, value in self.items():
            result[deepcopy(key, memo)] = deepcopy(value, memo)
        return result

    def __reduce__(self) -> tuple[type[dict[Any, Any]], tuple[dict[Any, Any]]]:
        return (dict, (dict(self),))


class FrozenList(list[Any]):
    """A list inside a sealed artifact: reads as a list, refuses in-place change.

    Every copy (``copy``, ``deepcopy``, ``list.copy``, pickling) is a plain,
    mutable ``list`` again.
    """

    __slots__ = ()

    __setitem__ = _refuse
    __delitem__ = _refuse
    __iadd__ = _refuse
    __imul__ = _refuse
    append = _refuse
    clear = _refuse
    extend = _refuse
    insert = _refuse
    pop = _refuse
    remove = _refuse
    reverse = _refuse
    sort = _refuse

    def __copy__(self) -> list[Any]:
        return list(self)

    def __deepcopy__(self, memo: dict[int, object]) -> list[Any]:
        result: list[Any] = []
        memo[id(self)] = result
        result.extend(deepcopy(value, memo) for value in self)
        return result

    def __reduce__(self) -> tuple[type[list[Any]], tuple[list[Any]]]:
        return (list, (list(self),))


# Value types whose instances cannot change (an Enum member is a singleton).
_IMMUTABLE_LEAVES: tuple[type, ...] = (
    str,
    int,
    float,
    bytes,
    datetime.date,
    datetime.time,
    datetime.timedelta,
    datetime.tzinfo,
    decimal.Decimal,
    uuid.UUID,
    Enum,
)


class _Unsealable(Exception):
    """A value in the tree cannot be proven immutable; nothing is sealed."""


class _Sealing:
    """One sealing pass: builds frozen containers aside, then commits all at once."""

    def __init__(self) -> None:
        self.models: dict[int, BaseModel] = {}
        self.assignments: list[tuple[BaseModel, str, object]] = []

    def value(self, value: object) -> object:
        kind = type(value)
        if kind is str or kind is int or kind is bool or value is None:
            return value
        if isinstance(value, BaseModel):
            self.model(value)
            return value
        if kind is dict or kind is FrozenDict:
            items: dict[Any, Any] = value  # type: ignore[assignment]
            return FrozenDict({self.key(key): self.value(item) for key, item in items.items()})
        if kind is list or kind is FrozenList:
            members: list[Any] = value  # type: ignore[assignment]
            return FrozenList(self.value(item) for item in members)
        if kind is tuple:
            original: tuple[Any, ...] = value  # type: ignore[assignment]
            frozen = tuple(self.value(item) for item in original)
            if all(new is old for new, old in zip(frozen, original, strict=True)):
                return original
            return frozen
        if kind is frozenset:
            elements: frozenset[Any] = value  # type: ignore[assignment]
            for element in elements:
                if self.value(element) is not element:
                    raise _Unsealable(f"a frozenset holds a mutable {type(element).__name__}")
            return value
        if isinstance(value, _IMMUTABLE_LEAVES):
            return value
        raise _Unsealable(f"cannot prove a {kind.__name__} immutable")

    def key(self, key: object) -> object:
        if self.value(key) is not key:
            raise _Unsealable("a mapping key is mutable")
        return key

    def model(self, model: BaseModel) -> None:
        if id(model) in self.models or _sealed_entry(model) is not None:
            return
        cls = type(model)
        if cls.model_config.get("frozen") is not True:
            raise _Unsealable(f"{cls.__name__} is not a frozen model")
        if model.__pydantic_extra__ or model.__pydantic_private__:
            raise _Unsealable(f"{cls.__name__} carries extra or private state")
        self.models[id(model)] = model
        state = model.__dict__
        for name in cls.model_fields:
            if name not in state:
                continue
            current = state[name]
            frozen = self.value(current)
            if frozen is not current:
                self.assignments.append((model, name, frozen))

    def commit(self) -> None:
        for model, name, frozen in self.assignments:
            # The one sanctioned write: the sealing pass, before the value is handed out.
            model.__dict__[name] = frozen
        for model in self.models.values():
            _register(model)


class _Seal:
    __slots__ = ("ref", "derived")

    def __init__(self, ref: weakref.ref[BaseModel]) -> None:
        self.ref = ref
        self.derived: dict[object, object] = {}


_SEALED: dict[int, _Seal] = {}


def _forget(key: int, ref: weakref.ref[BaseModel]) -> None:
    entry = _SEALED.get(key)
    if entry is not None and entry.ref is ref:
        _SEALED.pop(key, None)


def _register(model: BaseModel) -> None:
    key = id(model)
    _SEALED[key] = _Seal(weakref.ref(model, functools.partial(_forget, key)))


def _sealed_entry(value: object) -> _Seal | None:
    entry = _SEALED.get(id(value))
    if entry is None or entry.ref() is not value:
        return None
    return entry


def seal(value: object) -> bool:
    """Make ``value``'s whole model tree deeply immutable, or change nothing.

    Returns whether ``value`` is now sealed. Sealing is all-or-nothing: if any
    model in the tree is not frozen, carries extra or private state, or holds a
    value whose immutability cannot be proven, nothing changes and nothing is
    sealed. Only the owner of a freshly built value seals it, before handing it
    out: sealing swaps the tree's dict and list containers for ones that refuse
    in-place change.
    """

    if not isinstance(value, BaseModel):
        return False
    if _sealed_entry(value) is not None:
        return True
    sealing = _Sealing()
    try:
        sealing.model(value)
    except _Unsealable:
        return False
    sealing.commit()
    return True


def is_sealed(value: object) -> bool:
    """Whether ``value`` is an object ``seal`` sealed (its copies never are)."""

    return _sealed_entry(value) is not None


def sealed_memo(derive: Callable[[_A], _V]) -> Callable[[_A], _V]:
    """Remember ``derive(value)`` once per sealed ``value``; derive afresh otherwise.

    ``derive`` must be a pure function of the value's content and return an
    immutable result (a frozen digest value, a string, a tuple of them): every
    caller receives the one remembered result.
    """

    name = f"{derive.__module__}.{derive.__qualname__}"

    @functools.wraps(derive)
    def memoized(value: _A) -> _V:
        entry = _sealed_entry(value)
        if entry is None:
            return derive(value)
        found = entry.derived.get(name, _MISSING)
        if found is _MISSING:
            found = derive(value)
            entry.derived[name] = found
        return found  # type: ignore[return-value]

    return memoized


def sealed_derive(value: object, key: tuple[object, ...], derive: Callable[[], _V]) -> _V:
    """Remember ``derive()`` once per sealed ``value`` under ``key``; derive afresh otherwise.

    For a pure derivation of ``value`` that also depends on other arguments:
    ``key`` names the derivation (its first element, a string no other caller
    uses) and those arguments, and holds immutable values only -- strings,
    numbers, ``None`` and tuples of them, never a model or a container a caller
    could change. ``derive`` must return an immutable result: every caller gets
    the one remembered value.
    """

    entry = _sealed_entry(value)
    if entry is None:
        return derive()
    found = entry.derived.get(key, _MISSING)
    if found is _MISSING:
        found = derive()
        entry.derived[key] = found
    return found  # type: ignore[return-value]


class _Shared:
    """A sealed parse: the one immutable value every caller receives."""

    __slots__ = ("value",)

    def __init__(self, value: object) -> None:
        self.value = value


#: How many times its content's size a sealed parse is weighed at, for its parsed
#: model tree (the pickle of one ran from two to four times the JSON it parsed).
_SEALED_WEIGHT = 4


class _RequestCarrier:
    """Strong ownership until the outermost request exits, independent of LRU budgets."""

    def __init__(self) -> None:
        self.parses: dict[tuple[object, ...], _Shared | bytes] = {}
        self.derived: dict[tuple[object, ...], object] = {}


_REQUEST_CARRIER: ContextVar[_RequestCarrier | None] = ContextVar(
    "artifact_request_carrier", default=None
)


@contextmanager
def artifact_request() -> Iterator[None]:
    """Carry exact immutable parses and derivations across all artifact kinds.

    Nested pipeline stages join their caller. There is no process-wide growth:
    the outermost synchronous request releases everything, including oversized
    artifacts that the bounded process memos deliberately cannot retain.
    """
    if _REQUEST_CARRIER.get() is not None:
        yield
        return
    token = _REQUEST_CARRIER.set(_RequestCarrier())
    try:
        yield
    finally:
        _REQUEST_CARRIER.reset(token)


def request_derive(
    key: tuple[object, ...],
    derive: Callable[[], _V],
    *,
    retain: Callable[[_V], bool] | None = None,
) -> _V:
    """One pure immutable derivation per exact value key in the owning request.

    Callers own the namespace (the first key element) and must supply immutable
    keys and results. With no explicit request, compute without retention.
    Refusals are never retained.
    """
    carrier = _REQUEST_CARRIER.get()
    if carrier is None:
        return derive()
    found = carrier.derived.get(key, _MISSING)
    if found is _MISSING:
        found = derive()
        if retain is None or retain(found):
            carrier.derived[key] = found
    return found  # type: ignore[return-value]


class ParseMemo(Generic[_V]):
    """``(path, codec, exact bytes) -> parsed artifact``, bounded by entries and bytes."""

    def __init__(
        self, *, capacity: int, max_bytes: int, max_entry_bytes: int | None = None
    ) -> None:
        self._entries: OrderedDict[tuple[str, object, bytes], tuple[_Shared | bytes, int]] = (
            OrderedDict()
        )
        self._capacity = capacity
        self._max_bytes = max_bytes
        # One entry may take at most this much of the budget: a sixteenth unless
        # a memo of few large records (a change-set receipt) says otherwise.
        self._max_entry_bytes = max_bytes // 16 if max_entry_bytes is None else max_entry_bytes
        self._bytes = 0
        self._lock = threading.Lock()

    def parse(self, content: bytes, path: str, codec: object, parse: Callable[[], _V]) -> _V:
        if type(content) is not bytes or type(path) is not str:
            return parse()
        key = (path, codec, content)
        try:
            hash(key)
        except TypeError:
            return parse()
        carrier = _REQUEST_CARRIER.get()
        request_key = (self, *key)
        stored = None if carrier is None else carrier.parses.get(request_key)
        if stored is not None:
            if isinstance(stored, _Shared):
                return stored.value  # type: ignore[return-value]
            return pickle.loads(stored)  # type: ignore[no-any-return]
        with self._lock:
            found = self._entries.get(key)
            if found is not None:
                self._entries.move_to_end(key)
        if found is not None:
            stored = found[0]
            if carrier is not None:
                carrier.parses[request_key] = stored
            if isinstance(stored, _Shared):
                return stored.value  # type: ignore[return-value]
            return pickle.loads(stored)  # type: ignore[no-any-return]
        value = parse()
        stored_value: _Shared | bytes
        if seal(value):
            # A sealed value is shared, never copied, so it is never pickled:
            # it is weighed as its content's size several times over instead.
            weight = len(content) * (1 + _SEALED_WEIGHT) + len(path)
            stored_value = _Shared(value)
        else:
            try:
                pickled = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
            except (pickle.PicklingError, TypeError, AttributeError):
                return value
            weight = len(content) + len(path) + len(pickled)
            stored_value = pickled
        if carrier is not None:
            carrier.parses[request_key] = stored_value
        if weight > self._max_entry_bytes:
            return value
        with self._lock:
            if key not in self._entries:
                self._entries[key] = (stored_value, weight)
                self._bytes += weight
            while self._entries and (
                len(self._entries) > self._capacity or self._bytes > self._max_bytes
            ):
                _old_key, (_old_value, old_weight) = self._entries.popitem(last=False)
                self._bytes -= old_weight
        return value

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._bytes = 0


class ValueMemo(Generic[_V]):
    """A value derived from a key of immutable values (its equality is the content's)."""

    def __init__(self, *, capacity: int) -> None:
        self._entries: OrderedDict[Any, _V] = OrderedDict()
        self._capacity = capacity
        self._lock = threading.Lock()

    def derive(self, key: Any, derive: Callable[[], _V]) -> _V:
        try:
            hash(key)
        except TypeError:
            return derive()
        with self._lock:
            found = self._entries.get(key, _MISSING)
            if found is not _MISSING:
                self._entries.move_to_end(key)
                return found  # type: ignore[return-value]
        value = derive()
        with self._lock:
            self._entries[key] = value
            self._entries.move_to_end(key)
            while len(self._entries) > self._capacity:
                self._entries.popitem(last=False)
        return value

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


__all__ = [
    "FrozenDict",
    "FrozenList",
    "artifact_request",
    "request_derive",
    "ParseMemo",
    "ValueMemo",
    "is_sealed",
    "seal",
    "sealed_derive",
    "sealed_memo",
]
