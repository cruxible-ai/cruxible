"""The genesis seed-set history is append-only.

Every instance's generation zero holds one genesis seed set, and open accepts
any set in ``GENESIS_SEED_HISTORY`` (``ledger/genesis_seeds.py``). An instance
keeps the set it was created with, so a set some published release created
instances under must never change.

The rule, by ``SEED_HISTORY_FROZEN_THROUGH`` (the highest ordinal a published
release created instances under):

- Entries at or below it are frozen. After a release, a changed seed adds entry
  N+1 with the new table and a new pin below; entry N is never edited or removed.
- Entries above it are not published yet and may still be replaced together
  with their pin. Before 0.5.0 is cut the constant is 0, so entry 1 may still
  be replaced; cutting the release raises it to the newest ordinal.

``genesis_seed_history_pins.json`` beside this file pins each entry's set
digest by hand: never regenerate it wholesale.
"""

from __future__ import annotations

import hashlib
from importlib.resources import files
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from cruxible_core.ledger import genesis_seeds
from cruxible_core.ledger.bootstrap import genesis_seed_files
from cruxible_core.ledger.genesis_seeds import (
    GenesisSeed,
    GenesisSeedSet,
    current_genesis_seed_set,
    held_seed_table,
)

PIN_PATH = Path(__file__).with_name("genesis_seed_history_pins.json")
HISTORY_MODULE = "src/cruxible_core/ledger/genesis_seeds.py"


class SeedSetPin(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ordinal: int
    name: str
    set_digest: str


class SeedSetPins(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entries: tuple[SeedSetPin, ...]


def _pins() -> SeedSetPins:
    return SeedSetPins.model_validate_json(PIN_PATH.read_bytes())


def _checked_in_table() -> dict[str, str]:
    """Path -> SHA-256 of every seed file checked in under ``seed_artifacts/genesis``."""

    root = Path(str(files("cruxible_core.governance.seed_artifacts").joinpath("genesis")))
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _pin_line(entry: GenesisSeedSet) -> str:
    return SeedSetPin(
        ordinal=entry.ordinal, name=entry.name, set_digest=entry.set_digest()
    ).model_dump_json()


def _first_difference(table: dict[str, str], other: dict[str, str]) -> str:
    return min(path for path in table.keys() | other.keys() if table.get(path) != other.get(path))


def seed_history_problems(pins: SeedSetPins, checked_in: dict[str, str]) -> list[str]:
    """Everything that breaks the append-only rule, each with the instruction that fixes it."""

    history = genesis_seeds.GENESIS_SEED_HISTORY
    frozen_through = genesis_seeds.SEED_HISTORY_FROZEN_THROUGH
    problems: list[str] = []
    if [entry.ordinal for entry in history] != list(range(1, len(history) + 1)):
        problems.append(f"GENESIS_SEED_HISTORY ordinals must run 1..N in order ({HISTORY_MODULE})")
    if len({entry.name for entry in history}) != len(history):
        problems.append("GENESIS_SEED_HISTORY entry names must be unique")
    if not 0 <= frozen_through <= len(history):
        problems.append(
            f"SEED_HISTORY_FROZEN_THROUGH = {frozen_through} names no entry of the history"
        )

    newest = history[-1]
    following = newest.ordinal + 1
    if checked_in != newest.table():
        where = _first_difference(checked_in, newest.table())
        if newest.ordinal <= frozen_through:
            problems.append(
                f"The checked-in genesis seeds differ from history entry {newest.ordinal} "
                f"({newest.name}) at {where}, and a published release created instances under "
                f"it. Add entry {following} to GENESIS_SEED_HISTORY in {HISTORY_MODULE} with the "
                f"new table and pin it in {PIN_PATH.name}; do not edit entry {newest.ordinal}."
            )
        else:
            problems.append(
                f"The checked-in genesis seeds differ from history entry {newest.ordinal} "
                f"({newest.name}) at {where}. It is not published yet "
                f"(SEED_HISTORY_FROZEN_THROUGH = {frozen_through}): replace its table and its "
                f"pin, or add entry {following}."
            )

    pin_ordinals = [pin.ordinal for pin in pins.entries]
    for ordinal in sorted({o for o in pin_ordinals if pin_ordinals.count(o) > 1}):
        problems.append(
            f"{PIN_PATH.name} pins entry {ordinal} more than once: an entry has one pin, and a "
            "frozen entry's pin never changes."
        )
    pinned: dict[int, SeedSetPin] = {}
    for pin in pins.entries:
        pinned.setdefault(pin.ordinal, pin)
    for entry in history:
        pin = pinned.get(entry.ordinal)
        if pin is None:
            problems.append(
                f"History entry {entry.ordinal} ({entry.name}) has no pin: add "
                f"{_pin_line(entry)} to {PIN_PATH.name}."
            )
        elif (pin.name, pin.set_digest) != (entry.name, entry.set_digest()):
            if entry.ordinal <= frozen_through:
                problems.append(
                    f"History entry {entry.ordinal} ({pin.name}) was edited, and a published "
                    f"release created instances under it. Restore it (pinned {pin.set_digest}); "
                    f"for new seeds add entry {following}. Do not edit entry {entry.ordinal}."
                )
            else:
                problems.append(
                    f"History entry {entry.ordinal} changed. It is not published yet "
                    f"(SEED_HISTORY_FROZEN_THROUGH = {frozen_through}), so it may be replaced: "
                    f"set its pin to {_pin_line(entry)}."
                )
    for ordinal in sorted(pinned.keys() - {entry.ordinal for entry in history}):
        problems.append(
            f"Pinned entry {ordinal} ({pinned[ordinal].name}) is missing from "
            "GENESIS_SEED_HISTORY: entries are never removed; restore it."
        )
    return problems


def test_the_seed_history_is_append_only() -> None:
    problems = seed_history_problems(_pins(), _checked_in_table())
    assert not problems, "\n".join(problems)


def test_the_newest_entry_is_what_creation_writes() -> None:
    assert held_seed_table(genesis_seed_files()) == current_genesis_seed_set().table()
    assert _checked_in_table() == current_genesis_seed_set().table()


def _moved(table: dict[str, str]) -> dict[str, str]:
    """The checked-in table with one seed's bytes changed."""

    return {**table, "triggers/floor-refresh.json": hashlib.sha256(b"moved").hexdigest()}


def _edited(entry: GenesisSeedSet, table: dict[str, str]) -> GenesisSeedSet:
    return GenesisSeedSet(
        ordinal=entry.ordinal,
        name=entry.name,
        seeds=tuple(GenesisSeed(path, digest) for path, digest in sorted(table.items())),
    )


def test_moved_seeds_without_a_new_entry_fail_after_the_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = current_genesis_seed_set()
    monkeypatch.setattr(genesis_seeds, "SEED_HISTORY_FROZEN_THROUGH", entry.ordinal)
    moved = _moved(_checked_in_table())

    # The seed bytes moved and the history did not.
    problems = seed_history_problems(_pins(), moved)
    assert len(problems) == 1
    assert f"Add entry {entry.ordinal + 1}" in problems[0]
    assert f"do not edit entry {entry.ordinal}" in problems[0]
    assert "triggers/floor-refresh.json" in problems[0]

    # The bytes moved and entry N was edited to match: still refused.
    monkeypatch.setattr(genesis_seeds, "GENESIS_SEED_HISTORY", (_edited(entry, moved),))
    (problem,) = seed_history_problems(_pins(), moved)
    assert f"History entry {entry.ordinal} (0.5.0) was edited" in problem
    assert f"Do not edit entry {entry.ordinal}" in problem

    # ... and a second pin for the same ordinal does not hide the edit.
    edited = _edited(entry, moved)
    doubled = SeedSetPins(
        entries=(
            *_pins().entries,
            SeedSetPin(ordinal=entry.ordinal, name=entry.name, set_digest=edited.set_digest()),
        )
    )
    problems = seed_history_problems(doubled, moved)
    assert any("more than once" in problem for problem in problems), problems
    assert any("was edited" in problem for problem in problems), problems


def test_a_new_entry_with_its_pin_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = current_genesis_seed_set()
    monkeypatch.setattr(genesis_seeds, "SEED_HISTORY_FROZEN_THROUGH", entry.ordinal)
    moved = _moved(_checked_in_table())
    added = GenesisSeedSet(
        ordinal=entry.ordinal + 1, name="next", seeds=_edited(entry, moved).seeds
    )
    monkeypatch.setattr(genesis_seeds, "GENESIS_SEED_HISTORY", (entry, added))

    # Without its pin the guard names the line to add.
    (problem,) = seed_history_problems(_pins(), moved)
    assert problem.startswith(f"History entry {added.ordinal} (next) has no pin: add ")
    assert added.set_digest() in problem

    pins = SeedSetPins(
        entries=(
            *_pins().entries,
            SeedSetPin(ordinal=added.ordinal, name=added.name, set_digest=added.set_digest()),
        )
    )
    assert seed_history_problems(pins, moved) == []


def test_an_unpublished_entry_may_be_replaced_with_its_pin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = current_genesis_seed_set()
    monkeypatch.setattr(genesis_seeds, "SEED_HISTORY_FROZEN_THROUGH", entry.ordinal - 1)
    moved = _moved(_checked_in_table())
    replaced = _edited(entry, moved)
    monkeypatch.setattr(genesis_seeds, "GENESIS_SEED_HISTORY", (replaced,))

    (problem,) = seed_history_problems(_pins(), moved)
    assert "it may be replaced: set its pin to" in problem
    assert replaced.set_digest() in problem

    pins = SeedSetPins(
        entries=(
            SeedSetPin(
                ordinal=replaced.ordinal, name=replaced.name, set_digest=replaced.set_digest()
            ),
        )
    )
    assert seed_history_problems(pins, moved) == []


def test_a_removed_entry_fails() -> None:
    entry = current_genesis_seed_set()
    pins = SeedSetPins(
        entries=(
            *_pins().entries,
            SeedSetPin(ordinal=entry.ordinal + 1, name="gone", set_digest=entry.set_digest()),
        )
    )
    (problem,) = seed_history_problems(pins, _checked_in_table())
    assert "entries are never removed" in problem
