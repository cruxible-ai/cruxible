"""Append-only raw measurements; unknown usage and ungraded criteria stay unknown."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .adapters import MemoryReport, Usage
from .config import Model, Question


class CriterionResult(Model):
    criterion_id: str
    passed: bool | None = None
    notes: str = ""


class Grade(Model):
    criteria: tuple[CriterionResult, ...]
    grader: str

    @property
    def complete(self) -> bool:
        return bool(self.criteria) and all(c.passed is not None for c in self.criteria)


class WrongMoves(Model):
    stale_answer_as_current: bool | None = None
    lost_finding: bool | None = None


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
    version: Literal[1] = 1
    kind: Literal["setup", "session", "question"]
    run_id: str
    arm: Literal["A", "B", "C"]
    repetition: int = Field(ge=1)
    session_index: int = Field(ge=0)
    adapter: str
    dry_run: bool
    prompt: str
    output: str
    usage: Usage
    wall_seconds: float = Field(ge=0)
    budget_exceeded: bool
    first_useful_state: bool = False
    source: SourceSnapshot
    size: SizeMetrics | None = None
    upkeep_compute_seconds: float | None = Field(default=None, ge=0)
    # Session work accumulated since the preceding question batch; recorded once,
    # not once per question (which would double-count upkeep).
    between_questions_tokens: int | None = Field(default=None, ge=0)
    between_questions_compute_seconds: float | None = Field(default=None, ge=0)
    question: Question | None = None
    grade: Grade | None = None
    wrong_moves: WrongMoves = Field(default_factory=WrongMoves)

    @model_validator(mode="after")
    def grading_shape(self) -> Record:
        if (self.kind == "question") != (self.question is not None):
            raise ValueError("only question records carry a question rubric")
        if self.grade is not None:
            if self.question is None:
                raise ValueError("only question records have criterion grades")
            ids = [c.criterion_id for c in self.grade.criteria]
            if len(set(ids)) != len(ids) or set(ids) != {c.id for c in self.question.criteria}:
                raise ValueError("a grade must name every rubric criterion exactly once")
        return self

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        return (
            self.run_id,
            self.kind,
            str(self.session_index),
            "" if self.question is None else self.question.id,
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
