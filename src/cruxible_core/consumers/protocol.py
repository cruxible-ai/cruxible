"""What a consumer kind is, to the daemon loop that runs every kind.

A kind follows the instance's logs or a clock (its subscription), keeps its own
place in them (its cursor), and acts on what it matched. Matching never acts:
it only records due work, which the runner hands to the kind's bounded workers,
one flight per work key at a time.

Two properties separate the kinds rather than a class hierarchy. The cursor
policy says what a restart does: a forward-only kind starts again from now and
leaves what it missed to explicit action; a resuming kind continues from where
it stopped. The effect class says what acting may change: a governed kind
admits work under authority it rechecks each time; a findings kind writes only
state it could rebuild from the logs.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

CursorPolicy = Literal["forward_only", "resume"]
EffectClass = Literal["governed", "findings"]
ConsumerState = Literal["running", "lagging", "stopped", "stalled", "disabled"]


@dataclass(frozen=True)
class ConsumerWork:
    """One unit of due work; `key` is its single-flight identity within the kind."""

    key: str
    item: Any


@dataclass(frozen=True)
class ConsumerRepair:
    """The served action that gets a stopped or stalled consumer doing its job again."""

    operation: str
    required_change: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ConsumerHealth:
    """Where one consumer stands, for daemon status and `next`."""

    kind: str
    consumer_id: str
    state: ConsumerState
    detail: dict[str, Any]
    repair: ConsumerRepair | None = None


class ConsumerKind(Protocol):
    name: str
    cursor_policy: CursorPolicy
    effect_class: EffectClass
    #: Concurrent flights of this kind, across every instance.
    workers: int

    def active(self, instance: Any) -> bool:
        """Whether the instance has anything for this kind, checked without side effects."""

    def match(self, instance: Any, *, now: datetime, daemon_id: str) -> None:
        """Advance this kind's cursors and record due work; never act."""

    def due(self, instance: Any, *, now: datetime) -> Iterable[ConsumerWork]:
        """The work matching recorded that is due now."""

    def run(self, manager: Any, instance_id: str, work: ConsumerWork, *, now: datetime) -> None:
        """Act on one unit of due work."""

    def health(self, instance: Any, *, now: datetime) -> tuple[ConsumerHealth, ...]:
        """Every consumer of this kind on the instance, as it stands at `now`."""
