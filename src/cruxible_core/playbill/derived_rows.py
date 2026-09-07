"""Detached parsed views over immutable, structurally shared canonical rows.

Pydantic's frozen models still expose mutable nested values and ``__dict__``.
Only canonical bytes are retained; a lookup parses the requested row into a
caller-owned value. Updating a row never clones unrelated parsed objects.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, MutableMapping
from dataclasses import dataclass
from typing import Generic, TypeVar

from cruxible_client._persistent import MapMutation, PersistentMap

T = TypeVar("T")


@dataclass(frozen=True)
class CanonicalRows(Mapping[str, T], Generic[T]):
    rows: Mapping[str, bytes]
    encode: Callable[[T], bytes]
    decode: Callable[[bytes], T]

    @classmethod
    def build(
        cls,
        values: Mapping[str, T],
        *,
        encode: Callable[[T], bytes],
        decode: Callable[[bytes], T],
    ) -> CanonicalRows[T]:
        return cls(
            PersistentMap({key: encode(value) for key, value in values.items()}), encode, decode
        )

    def __getitem__(self, key: str) -> T:
        return self.decode(self.rows[key])

    def __iter__(self) -> Iterator[str]:
        return iter(self.rows)

    def __len__(self) -> int:
        return len(self.rows)

    def __contains__(self, key: object) -> bool:
        return key in self.rows

    def mutate(self) -> CanonicalRowMutation[T]:
        return CanonicalRowMutation(MapMutation(self.rows), self.encode, self.decode)


class CanonicalRowMutation(MutableMapping[str, T], Generic[T]):
    def __init__(
        self,
        rows: MapMutation[bytes],
        encode: Callable[[T], bytes],
        decode: Callable[[bytes], T],
    ) -> None:
        self.rows = rows
        self.encode = encode
        self.decode = decode

    def __getitem__(self, key: str) -> T:
        return self.decode(self.rows[key])

    def __setitem__(self, key: str, value: T) -> None:
        self.rows[key] = self.encode(value)

    def __delitem__(self, key: str) -> None:
        del self.rows[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.rows)

    def __len__(self) -> int:
        return len(self.rows)

    def __contains__(self, key: object) -> bool:
        return key in self.rows

    def finish(self) -> CanonicalRows[T]:
        return CanonicalRows(self.rows.finish(), self.encode, self.decode)
