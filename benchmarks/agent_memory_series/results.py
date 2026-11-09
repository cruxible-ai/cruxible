"""Append-only raw measurements; unknown usage and unreviewed answers stay unknown."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .adapters import MemoryReport, SurfaceAction, Usage
from .config import Model, Question


class Review(Model):
    """The maintainer's eye test of one answer or audit session.

    The three fault flags are the review; `needs_validation_with_blocker` marks
    an answer that names what is unverified and what blocks verifying it. That
    is a valid answer, not a missing one.
    """

    reviewer: str = "manual"
    notes: str = ""
    stale_as_current: bool | None = None
    wrong: bool | None = None
    missing: bool | None = None
    needs_validation_with_blocker: bool | None = None

    @property
    def complete(self) -> bool:
        return None not in (self.stale_as_current, self.wrong, self.missing)

    @property
    def flagged(self) -> bool:
        return bool(self.stale_as_current or self.wrong or self.missing)


class SourceSnapshot(Model):
    digest: str
    bytes: int = Field(ge=0)
    files: int = Field(ge=0)


class SizeMetrics(Model):
    state_bytes: int = Field(ge=0)
    state_to_source_ratio: float | None
    claims_per_source_file: float | None
    copy_in_failure_share: float | None
    report: MemoryReport
    measurement_seconds: float = Field(ge=0)


class Record(Model):
    version: Literal[2] = 2
    kind: Literal["setup", "session", "question"]
    # Setup records only: an agent setup prompt, or a harness command (no model).
    setup_kind: Literal["agent", "command"] | None = None
    run_id: str
    arm: Literal["A", "B", "C"]
    repetition: int = Field(ge=1)
    session_index: int = Field(ge=0)
    setup_step: int | None = Field(default=None, ge=0)
    adapter: str
    dry_run: bool
    prompt: str
    output: str
    usage: Usage
    wall_seconds: float = Field(ge=0)
    # The runaway guard is an outcome, not a failure: the run continues.
    runaway: Literal["wall_clock", "cost"] | None = None
    first_useful_state: bool = False
    model_id: str | None = None
    actions: tuple[SurfaceAction, ...] = ()
    source: SourceSnapshot
    size: SizeMetrics | None = None
    upkeep_compute_seconds: float | None = Field(default=None, ge=0)
    # Session work accumulated since the preceding question batch; recorded once,
    # not once per question (which would double-count upkeep).
    between_questions_tokens: int | None = Field(default=None, ge=0)
    between_questions_compute_seconds: float | None = Field(default=None, ge=0)
    question: Question | None = None
    review: Review | None = None

    @model_validator(mode="after")
    def shape(self) -> Record:
        if (self.kind == "question") != (self.question is not None):
            raise ValueError("only question records carry a question")
        if (self.kind == "setup") != (self.setup_step is not None):
            raise ValueError("only setup records name a setup step")
        if (self.kind == "setup") != (self.setup_kind is not None):
            raise ValueError("only setup records name a setup kind")
        if self.kind == "setup" and self.review is not None:
            raise ValueError("setup is off the clock and not reviewed")
        return self

    @property
    def key(self) -> tuple[str, str, str, str, str, str]:
        return (
            self.run_id,
            self.kind,
            str(self.session_index),
            "" if self.question is None else self.question.id,
            "" if self.setup_step is None else str(self.setup_step),
            str(self.repetition),
        )


def load_results(path: Path) -> list[Record]:
    records = [Record.model_validate_json(line) for line in path.read_text().splitlines() if line]
    if len({record.key for record in records}) != len(records):
        raise ValueError("duplicate result keys")
    return records


def write_results(path: Path, records: list[Record]) -> None:
    with path.open("x") as handle:
        for record in records:
            handle.write(record.model_dump_json() + "\n")
