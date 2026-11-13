"""Per-session review sheets for the maintainer's eye test, with optional arm blinding.

No rubric: each answer gets free-text notes and three fault flags
(stale_as_current, wrong, missing), plus needs_validation_with_blocker, a
valid answer that names what is unverified and what blocks verifying it.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

from .config import digest
from .results import Record, Review

COLUMNS = [
    "response_id",
    "arm",
    "repetition",
    "session_index",
    "kind",
    "question_id",
    "question_kind",
    "prompt",
    "reviewer_context",
    "answer",
    "stale_as_current",
    "wrong",
    "missing",
    "needs_validation_with_blocker",
    "notes",
]
FLAGS = ("stale_as_current", "wrong", "missing", "needs_validation_with_blocker")


def _blind_id(record: Record) -> str:
    return digest(
        {
            "key": record.key,
            "prompt": record.prompt,
            "answer": record.output,
            "source": record.source.digest,
        }
    ).removeprefix("sha256:")[:24]


def _context(record: Record) -> str:
    if record.question is None:
        return ""
    return record.question.reviewer_context_by_session.get(record.session_index, "")


def _cell(value: bool | None) -> str:
    return "" if value is None else str(value).lower()


def _boolean(value: str) -> bool | None:
    if not value.strip():
        return None
    if value.strip().lower() in {"true", "1", "yes", "y", "x"}:
        return True
    if value.strip().lower() in {"false", "0", "no", "n"}:
        return False
    raise ValueError(f"invalid review flag {value!r}; use true, false or blank")


def export_sheets(records: list[Record], directory: Path, *, blind: bool = True) -> list[Path]:
    """One CSV per session index (`session-NN.csv`): audit-session and answer rows.

    Metrics are left out so the eye test does not see cost. Blinding hides the
    arm and mixes response order; an answer itself may reveal its memory method.
    """
    by_session: dict[int, list[dict[str, str]]] = defaultdict(list)
    for record in records:
        if record.kind == "setup":
            continue
        review = record.review or Review()
        question = record.question
        by_session[record.session_index].append(
            {
                "response_id": _blind_id(record),
                "arm": "" if blind else record.arm,
                "repetition": str(record.repetition),
                "session_index": str(record.session_index),
                "kind": record.kind,
                "question_id": question.id if question else "",
                "question_kind": question.kind if question else "",
                "prompt": record.prompt,
                "reviewer_context": _context(record),
                "answer": record.output,
                **{flag: _cell(getattr(review, flag)) for flag in FLAGS},
                "notes": review.notes,
            }
        )
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for session, rows in sorted(by_session.items()):
        # Audit rows first, then answers grouped by question; ids mix the arms.
        rows.sort(
            key=lambda row: (row["kind"] != "session", row["question_id"], row["response_id"])
        )
        path = directory / f"session-{session:02d}.csv"
        with path.open("x", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        paths.append(path)
    return paths


def sheet_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        files.extend(sorted(path.glob("*.csv")) if path.is_dir() else [path])
    return files


def apply_sheets(
    records: list[Record], paths: list[Path], *, reviewer: str = "manual"
) -> list[Record]:
    """Import reviewed rows; untouched rows (no flag, no note) stay unreviewed."""
    by_id = {_blind_id(record): record for record in records if record.kind != "setup"}
    reviews: dict[str, Review] = {}
    seen: set[str] = set()
    for path in sheet_files(paths):
        with path.open(newline="") as handle:
            for row in csv.DictReader(handle):
                response = row["response_id"]
                if response not in by_id:
                    raise ValueError(f"unknown response id {response}")
                if response in seen:
                    raise ValueError(f"duplicate review row for {response}")
                seen.add(response)
                record = by_id[response]
                if row["session_index"] != str(record.session_index) or row["kind"] != record.kind:
                    raise ValueError(
                        "review sheet session metadata differs from the captured response"
                    )
                if row["answer"] != record.output or row["prompt"] != record.prompt:
                    raise ValueError("review sheet no longer matches the captured answer/prompt")
                if row["reviewer_context"] != _context(record):
                    raise ValueError("review sheet changed the frozen reviewer context")
                flags = {flag: _boolean(row[flag]) for flag in FLAGS}
                notes = row["notes"].strip()
                if notes or any(value is not None for value in flags.values()):
                    reviews[response] = Review(reviewer=reviewer, notes=notes, **flags)
    return [
        record.model_copy(update={"review": reviews[_blind_id(record)]})
        if record.kind != "setup" and _blind_id(record) in reviews
        else record
        for record in records
    ]
