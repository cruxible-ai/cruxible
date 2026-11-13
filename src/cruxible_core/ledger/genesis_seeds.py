"""The append-only history of genesis seed sets.

Generation zero of every instance holds one genesis seed set: the artifacts
creation writes beside the bootstrap policies and principals (Claim types,
Triggers, the builtin Provider). Their bytes live under
``governance/seed_artifacts/genesis/<path>``. A later release may change a
seed's bytes; an instance keeps the set it was created with, so opening it must
accept every set some release created and not only the newest one.

Rules for the history:

- Entries are never edited or removed once a published release has created
  instances under them. A changed seed adds entry N+1; creation always writes
  the newest entry.
- ``SEED_HISTORY_FROZEN_THROUGH`` is the highest ordinal a published release
  created instances under. Entries at or below it are frozen; entries above it
  are not yet published and may still be replaced. Raise it to the newest
  ordinal when a release is cut.
- The guardrail ``tests/test_guardrails/test_genesis_seed_history.py`` pins
  each entry's set digest and enforces both rules.

Nothing here migrates an instance between sets: open only checks that the set
an instance holds is one of these.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from cruxible_client.contracts.canonical import Sha256Value, typed_digest

#: The highest ordinal a published release created instances under. Entries at
#: or below it are frozen; raise it to the newest ordinal when a release is cut.
#: 0 means nothing is published yet, so entry 1 may still be replaced.
SEED_HISTORY_FROZEN_THROUGH = 0


@dataclass(frozen=True)
class GenesisSeed:
    """One seeded artifact: its ledger-tree path and the SHA-256 of its exact bytes."""

    path: str
    sha256: str

    def __post_init__(self) -> None:
        if not self.path or self.path.startswith("/") or ".." in self.path.split("/"):
            raise ValueError(f"genesis seed path must be a relative tree path: {self.path!r}")
        Sha256Value(self.sha256)


@dataclass(frozen=True)
class GenesisSeedSet:
    """One entry of the history: an ordinal, a short name, and the exact seed table."""

    ordinal: int
    name: str
    seeds: tuple[GenesisSeed, ...]

    def __post_init__(self) -> None:
        if self.ordinal < 1:
            raise ValueError("genesis seed set ordinals start at 1")
        if not self.name:
            raise ValueError("a genesis seed set needs a name")
        paths = [seed.path for seed in self.seeds]
        if paths != sorted(set(paths)):
            raise ValueError("genesis seed set paths must be unique and sorted")

    def table(self) -> dict[str, str]:
        """Path -> SHA-256 of the seed bytes."""

        return {seed.path: seed.sha256 for seed in self.seeds}

    def set_digest(self) -> str:
        """The digest that names this set's table (pinned by the guardrail)."""

        return typed_digest(
            Sha256Value,
            "cruxible-genesis-seed-set-v1",
            {"seeds": [{"path": seed.path, "sha256": seed.sha256} for seed in self.seeds]},
        ).tagged


@dataclass(frozen=True)
class SeedSetDifference:
    """How a held seed table differs from one known set, at its first differing path."""

    seed_set: GenesisSeedSet
    differing_paths: int
    first_path: str
    first_reason: Literal["missing", "unexpected", "different bytes"]


#: Every genesis seed set some build creates instances with, oldest first.
#: Append only; see the module docstring.
GENESIS_SEED_HISTORY: tuple[GenesisSeedSet, ...] = (
    GenesisSeedSet(
        ordinal=1,
        name="0.5.0",
        seeds=(
            GenesisSeed(
                "claim-types/semantic/alias.json",
                "072ad267e8c90f817528e15d9d6ccb182852154e7cfda8ca320918064c4ed14a",
            ),
            GenesisSeed(
                "claim-types/semantic/distinct_from.json",
                "db8e0d864ae122bf57fb3cdb891aba33819c7f0029326b7f83d82b17fca69cc6",
            ),
            GenesisSeed(
                "providers/cruxible-builtin.json",
                "8923dca4680ed083b6f6f3062f3c2ed2910ca3ed91d42cfc0d5358d0ddae760c",
            ),
            GenesisSeed(
                "triggers/evidence-sweep.json",
                "473ad17889ae652b598e7aecbba13d624e67d45c0234550632501e8fb6f18e46",
            ),
            GenesisSeed(
                "triggers/floor-refresh.json",
                "0fb93d991ae564c3748f70c021023a3ac50815a23dc21b314586140c8e9e9ba7",
            ),
            GenesisSeed(
                "triggers/prediction-anchor-retry.json",
                "37fc9529405131805d568c39a63f78ed4401583edc1308a9b26589eda850dea4",
            ),
        ),
    ),
)


def current_genesis_seed_set() -> GenesisSeedSet:
    """The newest set: the one creation writes."""

    return GENESIS_SEED_HISTORY[-1]


def held_seed_table(seeds: Mapping[str, bytes]) -> dict[str, str]:
    """Path -> SHA-256 of seed bytes an instance holds (one hash per seed)."""

    return {path: hashlib.sha256(content).hexdigest() for path, content in seeds.items()}


def known_genesis_seed_set(held: Mapping[str, str]) -> GenesisSeedSet | None:
    """The history entry whose table equals ``held`` exactly, or None."""

    for seed_set in reversed(GENESIS_SEED_HISTORY):
        if seed_set.table() == held:
            return seed_set
    return None


def closest_genesis_seed_set(held: Mapping[str, str]) -> SeedSetDifference:
    """The known set with the fewest differing paths (newest on a tie), and its first one."""

    best: SeedSetDifference | None = None
    for seed_set in reversed(GENESIS_SEED_HISTORY):
        table = seed_set.table()
        differing: dict[str, Literal["missing", "unexpected", "different bytes"]] = {}
        for path in table.keys() | held.keys():
            if path not in held:
                differing[path] = "missing"
            elif path not in table:
                differing[path] = "unexpected"
            elif held[path] != table[path]:
                differing[path] = "different bytes"
        if not differing:
            raise ValueError("the held seed table is a known set")
        first = min(differing)
        candidate = SeedSetDifference(
            seed_set=seed_set,
            differing_paths=len(differing),
            first_path=first,
            first_reason=differing[first],
        )
        if best is None or candidate.differing_paths < best.differing_paths:
            best = candidate
    assert best is not None
    return best


__all__ = [
    "GENESIS_SEED_HISTORY",
    "GenesisSeed",
    "GenesisSeedSet",
    "SEED_HISTORY_FROZEN_THROUGH",
    "SeedSetDifference",
    "closest_genesis_seed_set",
    "current_genesis_seed_set",
    "held_seed_table",
    "known_genesis_seed_set",
]
