"""Rubric grading and a round-trip manual sheet, with optional arm blinding."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Protocol

from .config import Question, digest
from .results import CriterionResult, Grade, Record, WrongMoves


class Grader(Protocol):
    name: str

    def grade(self, question: Question, session_index: int, answer: str) -> Grade: ...


class ManualGrader:
    name = "manual_pending"

    def grade(self, question: Question, session_index: int, answer: str) -> Grade:
        return Grade(
            criteria=tuple(CriterionResult(criterion_id=c.id) for c in question.criteria),
            grader=self.name,
        )


class ModelGraderStub:
    """A model grader must return the same typed per-criterion contract.

    The interface deliberately receives no arm, plugin configuration, cost or
    memory size. Implement model transport/usage capture before using it.
    """

    name = "model_grader_stub"

    def grade(self, question: Question, session_index: int, answer: str) -> Grade:
        raise NotImplementedError("Implement a model grader; no model grading has been performed")


def _blind_id(record: Record) -> str:
    return digest(
        {
            "key": record.key,
            "prompt": record.prompt,
            "answer": record.output,
            "source": record.source.digest,
            "rubric": None if record.question is None else record.question.model_dump(mode="json"),
        }
    ).removeprefix("sha256:")[:24]


def export_sheet(records: list[Record], path: Path, *, blind: bool = True) -> None:
    """One row per criterion, plus one wrong-moves row for each audit session."""
    columns = [
        "response_id",
        "arm",
        "repetition",
        "session_index",
        "kind",
        "question_id",
        "question_kind",
        "prompt",
        "answer",
        "criterion_id",
        "criterion",
        "expected",
        "passed",
        "stale_answer_as_current",
        "lost_finding",
        "notes",
    ]
    rows = []
    for record in records:
        if record.kind == "setup":
            continue
        question = record.question
        criteria = question.criteria if question is not None else (None,)
        for criterion in criteria:
            current = (
                next(
                    (
                        c
                        for c in record.grade.criteria
                        if criterion and c.criterion_id == criterion.id
                    ),
                    None,
                )
                if record.grade
                else None
            )
            rows.append(
                {
                    "response_id": _blind_id(record),
                    "arm": "" if blind else record.arm,
                    "repetition": record.repetition,
                    "session_index": record.session_index,
                    "kind": record.kind,
                    "question_id": question.id if question else "",
                    "question_kind": question.kind if question else "",
                    "prompt": record.prompt,
                    "answer": record.output,
                    "criterion_id": criterion.id if criterion else "",
                    "criterion": criterion.description
                    if criterion
                    else "Audit-session wrong moves",
                    "expected": criterion.expected_by_session.get(record.session_index, "")
                    if criterion
                    else "",
                    "passed": _cell(None if current is None else current.passed),
                    "stale_answer_as_current": _cell(record.wrong_moves.stale_answer_as_current),
                    "lost_finding": _cell(record.wrong_moves.lost_finding),
                    "notes": "" if current is None else current.notes,
                }
            )
    # Stable mixing avoids presenting all A answers followed by all B answers.
    rows.sort(key=lambda row: (row["response_id"], row["criterion_id"]))
    with path.open("x", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _cell(value: bool | None) -> str:
    return "" if value is None else str(value).lower()


def _boolean(value: str) -> bool | None:
    if not value.strip():
        return None
    if value.strip().lower() in {"true", "1", "yes", "pass"}:
        return True
    if value.strip().lower() in {"false", "0", "no", "fail"}:
        return False
    raise ValueError(f"invalid grading value {value!r}; use true, false or blank")


def apply_sheet(records: list[Record], path: Path) -> list[Record]:
    by_id = {_blind_id(record): record for record in records if record.kind != "setup"}
    rows: dict[str, dict[str, dict[str, str]]] = {}
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            response = row["response_id"]
            if response not in by_id:
                raise ValueError(f"unknown response id {response}")
            record = by_id[response]
            if row["session_index"] != str(record.session_index) or row["kind"] != record.kind:
                raise ValueError(
                    "grading sheet session metadata differs from the captured response"
                )
            if row["answer"] != record.output or row["prompt"] != record.prompt:
                raise ValueError("grading sheet no longer matches the captured answer/prompt")
            criteria = rows.setdefault(response, {})
            key = row["criterion_id"]
            if key in criteria:
                raise ValueError(f"duplicate criterion row for {response}/{key}")
            criteria[key] = row
    updated = []
    for record in records:
        if record.kind == "setup":
            updated.append(record)
            continue
        criteria = rows.get(_blind_id(record), {})
        expected = {c.id for c in record.question.criteria} if record.question else {""}
        if set(criteria) != expected:
            raise ValueError("grading sheet must retain each response and every criterion row")
        if record.question:
            for criterion in record.question.criteria:
                row = criteria[criterion.id]
                if row["criterion"] != criterion.description or row[
                    "expected"
                ] != criterion.expected_by_session.get(record.session_index, ""):
                    raise ValueError(
                        "grading sheet changed a frozen rubric criterion or expected fact"
                    )
        moves = {
            (_boolean(row["stale_answer_as_current"]), _boolean(row["lost_finding"]))
            for row in criteria.values()
        }
        if len(moves) != 1:
            raise ValueError("wrong-move flags must agree across a response's criterion rows")
        stale, lost = moves.pop()
        values: dict[str, object] = {
            "wrong_moves": WrongMoves(
                stale_answer_as_current=stale,
                lost_finding=lost,
            )
        }
        if record.question:
            values["grade"] = Grade(
                grader="manual_csv",
                criteria=tuple(
                    CriterionResult(
                        criterion_id=c.id,
                        passed=_boolean(criteria[c.id]["passed"]),
                        notes=criteria[c.id]["notes"],
                    )
                    for c in record.question.criteria
                ),
            )
        updated.append(Record.model_validate({**record.model_dump(), **values}))
    return updated
