"""Offline reduction of captured data; no missing grades or usage become zero."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean

from .results import Record


def _mean(values: list[int | float | None]) -> float | None:
    known = [value for value in values if value is not None]
    return mean(known) if known else None


def summarize(records: list[Record]) -> list[dict[str, object]]:
    groups: dict[tuple[str, int], list[Record]] = defaultdict(list)
    cumulative: dict[tuple[str, int], int | None] = defaultdict(int)
    asked: dict[tuple[str, int], int] = defaultdict(int)
    amortized: dict[tuple[str, int, int], float | None] = {}
    audit_tokens: dict[tuple[str, int], int | None] = defaultdict(int)
    audit_turns: dict[tuple[str, int], int | None] = defaultdict(int)
    useful_cost: dict[tuple[str, int], tuple[int | None, int | None]] = {}
    for record in records:
        run = (record.arm, record.repetition)
        tokens = record.usage.tokens
        held = cumulative[run]
        cumulative[run] = None if held is None or tokens is None else held + tokens
        if record.kind == "question":
            asked[run] += 1
        else:
            held = audit_tokens[run]
            audit_tokens[run] = None if held is None or tokens is None else held + tokens
            held_turns = audit_turns[run]
            audit_turns[run] = (
                None
                if held_turns is None or record.usage.turns is None
                else held_turns + record.usage.turns
            )
            if record.session_index <= 1 and record.first_useful_state and run not in useful_cost:
                useful_cost[run] = (audit_tokens[run], audit_turns[run])
        if record.kind != "setup":
            groups[(record.arm, record.session_index)].append(record)
            total = cumulative[run]
            amortized[(record.arm, record.session_index, record.repetition)] = (
                total / asked[run] if total is not None and asked[run] else None
            )
    rows = []
    for (arm, session), events in sorted(groups.items()):
        questions = [r for r in events if r.kind == "question"]
        graded = [r for r in questions if r.grade and r.grade.complete]
        criteria = [c for r in questions if r.grade for c in r.grade.criteria]
        total_criteria = sum(
            [len(r.question.criteria) for r in questions if r.question is not None]
        )
        graded_criteria = sum(c.passed is not None for c in criteria)
        audits = [r for r in events if r.kind == "session"]
        useful = [value for (key, _rep), value in useful_cost.items() if key == arm]
        rows.append(
            {
                "arm": arm,
                "session_index": session,
                "repetitions": len(audits),
                "questions": len(questions),
                "graded_questions": len(graded),
                "criteria_met": sum(c.passed is True for c in criteria)
                if graded_criteria
                else None,
                "criteria_total": total_criteria or None,
                "criteria_graded": graded_criteria,
                "criteria_met_pct": 100 * sum(c.passed is True for c in criteria) / total_criteria
                if total_criteria and graded_criteria == total_criteria
                else None,
                "tokens_per_question": _mean([r.usage.tokens for r in questions]),
                "questions_with_token_usage": sum(r.usage.tokens is not None for r in questions),
                "turns_per_question": _mean([r.usage.turns for r in questions]),
                "amortized_tokens_per_question": _mean(
                    [amortized[(arm, session, r.repetition)] for r in audits]
                ),
                "repetitions_with_complete_cost": sum(
                    amortized[(arm, session, r.repetition)] is not None for r in audits
                ),
                "session_tokens": _mean([r.usage.tokens for r in audits]),
                "session_turns": _mean([r.usage.turns for r in audits]),
                "upkeep_tokens_between_questions": _mean(
                    [r.between_questions_tokens for r in audits]
                ),
                "upkeep_compute_between_questions_seconds": _mean(
                    [r.between_questions_compute_seconds for r in audits]
                ),
                "session_wall_seconds": _mean([r.wall_seconds for r in audits]),
                "upfront_tokens_to_useful_state": _mean([cost for cost, _turns in useful]),
                "upfront_turns_to_useful_state": _mean([turns for _cost, turns in useful]),
                "state_to_source_ratio": _mean(
                    [r.size.state_to_source_ratio for r in audits if r.size]
                ),
                "state_bytes": _mean([r.size.state_bytes for r in audits if r.size]),
                "source_bytes": _mean([r.source.bytes for r in audits]),
                "claims_per_source_file": _mean(
                    [r.size.claims_per_source_file for r in audits if r.size]
                ),
                "copy_in_failure_share": _mean(
                    [r.size.copy_in_failure_share for r in audits if r.size]
                ),
                "stale_answer_as_current": sum(
                    r.wrong_moves.stale_answer_as_current is True for r in events
                )
                if any(r.wrong_moves.stale_answer_as_current is not None for r in events)
                else None,
                "lost_finding": sum(r.wrong_moves.lost_finding is True for r in events)
                if any(r.wrong_moves.lost_finding is not None for r in events)
                else None,
                "wrong_moves_graded_responses": sum(
                    r.wrong_moves.stale_answer_as_current is not None
                    and r.wrong_moves.lost_finding is not None
                    for r in events
                ),
                "budget_exceeded": sum(r.budget_exceeded for r in events),
            }
        )
    return rows


def write_outputs(records: list[Record], directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    rows = summarize(records)
    # CSV is always available, including where matplotlib is not installed.
    columns = (
        list(rows[0])
        if rows
        else ["arm", "session_index", "criteria_met_pct", "tokens_per_question"]
    )
    for name in ("summary.csv", "amortization.csv"):
        with (directory / name).open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
    visible = [
        "arm",
        "session_index",
        "graded_questions",
        "questions",
        "criteria_met_pct",
        "tokens_per_question",
        "amortized_tokens_per_question",
    ]
    with (directory / "summary.md").open("w") as handle:
        handle.write(
            "Synthetic dry run; no model was called.\n\n"
            if records and all(r.dry_run for r in records)
            else ""
        )
        handle.write(
            "| " + " | ".join(visible) + " |\n| " + " | ".join("---" for _ in visible) + " |\n"
        )
        for row in rows:
            handle.write(
                "| "
                + " | ".join("" if row[key] is None else str(row[key]) for key in visible)
                + " |\n"
            )
    try:
        import matplotlib  # type: ignore[import-not-found]  # Optional plotting dependency.
    except ImportError:
        status = {"generated": False, "reason": "matplotlib is not installed; use amortization.csv"}
    else:
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # type: ignore[import-not-found]

        figure, axes = plt.subplots(2, 1, sharex=True, figsize=(8, 7))
        for arm in sorted({str(row["arm"]) for row in rows}):
            selected = [row for row in rows if row["arm"] == arm]
            for axis, field in zip(axes, ("criteria_met_pct", "tokens_per_question"), strict=True):
                valid = [row for row in selected if row[field] is not None]
                if valid:
                    axis.plot(
                        [row["session_index"] for row in valid],
                        [row[field] for row in valid],
                        marker="o",
                        label=arm,
                    )
        axes[0].set_ylabel("Criteria met (%)")
        axes[0].set_ylim(0, 100)
        axes[1].set_ylabel("Tokens / question")
        axes[1].set_xlabel("Session index")
        for axis in axes:
            axis.grid(alpha=0.2)
            if axis.lines:
                axis.legend(title="Arm")
        figure.suptitle(
            "Synthetic dry run"
            if records and all(r.dry_run for r in records)
            else "Agent memory series"
        )
        figure.tight_layout()
        figure.savefig(directory / "amortization.png", dpi=160)
        plt.close(figure)
        status = {"generated": True, "path": "amortization.png"}
    (directory / "plot-status.json").write_text(json.dumps(status, indent=2) + "\n")
