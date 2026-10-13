"""How long rebuildable and raw operational state is kept: one setting for both.

``generations`` bounds the accepted projection pieces kept besides the serving
head (they are rebuilt on demand, so no days floor applies). ``days`` bounds
raw consumption receipts, which fold into durable counters first. Nothing a
signed or authoritative record references is ever deleted under either.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetentionSettings:
    """The one retention setting: projection generations and raw-receipt days."""

    generations: int = 64
    days: int = 14

    def __post_init__(self) -> None:
        if self.generations < 1:
            raise ValueError("retention keeps at least the serving head's generation")
        if self.days < 0:
            raise ValueError("retention days cannot be negative")


DEFAULT_RETENTION = RetentionSettings()

__all__ = ["DEFAULT_RETENTION", "RetentionSettings"]
