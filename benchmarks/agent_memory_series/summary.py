"""Offline reduction of captured data; missing usage and reviews never become zero.

Setup is off the clock: it is summarized separately (setup.csv) and excluded
from every session metric, including amortized cost per question.
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from statistics import mean, stdev

from .results import Record


def _mean(values: list[int | float | None]) -> float | None:
    known = [value for value in values if value is not None]
    return mean(known) if known else None


def _sum(values: list[int | float | None]) -> int | float | None:
    total: int | float = 0
    for value in values:
        if value is None:
            return None
        total += value
    return total


USAGE: dict[str, Callable[[Record], int | float | None]] = {
    "tokens": lambda r: r.usage.tokens,
    "total_tokens": lambda r: r.usage.total_tokens,
    "turns": lambda r: r.usage.turns,
    "cost_usd": lambda r: r.usage.cost_usd,
    "model_calls": lambda r: r.usage.model_calls,
    "subagent_calls": lambda r: r.usage.subagent_calls,
    "wall_seconds": lambda r: r.wall_seconds,
}


def _usage_means(prefix: str, records: list[Record]) -> dict[str, float | None]:
    return {f"{prefix}_{name}": _mean([get(r) for r in records]) for name, get in USAGE.items()}


def _count(records: list[Record], flag: str) -> int | None:
    reviewed = [r.review for r in records if r.review is not None]
    known = [getattr(review, flag) for review in reviewed if getattr(review, flag) is not None]
    return sum(value is True for value in known) if known else None


def summarize(records: list[Record]) -> list[dict[str, object]]:
    """One row per arm and session (1..N), mean across repetitions."""
    groups: dict[tuple[str, int], list[Record]] = defaultdict(list)
    cumulative_tokens: dict[tuple[str, int], int | None] = defaultdict(int)
    cumulative_cost: dict[tuple[str, int], float | None] = defaultdict(float)
    asked: dict[tuple[str, int], int] = defaultdict(int)
    amortized: dict[tuple[str, int, int], tuple[float | None, float | None]] = {}
    audit_tokens: dict[tuple[str, int], int | None] = defaultdict(int)
    audit_turns: dict[tuple[str, int], int | None] = defaultdict(int)
    useful_cost: dict[tuple[str, int], tuple[int | None, int | None]] = {}
    for record in records:
        if record.kind == "setup":
            continue
        run = (record.arm, record.repetition)
        tokens, cost = record.usage.tokens, record.usage.cost_usd
        held = cumulative_tokens[run]
        cumulative_tokens[run] = None if held is None or tokens is None else held + tokens
        held_cost = cumulative_cost[run]
        cumulative_cost[run] = None if held_cost is None or cost is None else held_cost + cost
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
            if record.session_index == 1 and record.first_useful_state and run not in useful_cost:
                useful_cost[run] = (audit_tokens[run], audit_turns[run])
        groups[(record.arm, record.session_index)].append(record)
        total_tokens, total_cost = cumulative_tokens[run], cumulative_cost[run]
        amortized[(record.arm, record.session_index, record.repetition)] = (
            total_tokens / asked[run] if total_tokens is not None and asked[run] else None,
            total_cost / asked[run] if total_cost is not None and asked[run] else None,
        )
    rows: list[dict[str, object]] = []
    for (arm, session), events in sorted(groups.items()):
        questions = [r for r in events if r.kind == "question"]
        audits = [r for r in events if r.kind == "session"]
        useful = [value for (key, _rep), value in useful_cost.items() if key == arm]
        reviewed = [r for r in questions if r.review is not None and r.review.complete]
        audits_reviewed = [r for r in audits if r.review is not None and r.review.complete]
        rows.append(
            {
                "arm": arm,
                "session_index": session,
                "repetitions": len(audits),
                "questions": len(questions),
                "reviewed_answers": len(reviewed),
                "flagged_answers": sum(r.review.flagged for r in reviewed if r.review)
                if reviewed
                else None,
                "stale_as_current": _count(questions, "stale_as_current"),
                "wrong": _count(questions, "wrong"),
                "missing": _count(questions, "missing"),
                "needs_validation_with_blocker": _count(questions, "needs_validation_with_blocker"),
                "audit_flagged": sum(r.review.flagged for r in audits_reviewed if r.review)
                if audits_reviewed
                else None,
                **_usage_means("question", questions),
                "questions_with_token_usage": sum(r.usage.tokens is not None for r in questions),
                **_usage_means("session", audits),
                "amortized_tokens_per_question": _mean(
                    [amortized[(arm, session, r.repetition)][0] for r in audits]
                ),
                "amortized_cost_per_question": _mean(
                    [amortized[(arm, session, r.repetition)][1] for r in audits]
                ),
                "repetitions_with_complete_cost": sum(
                    amortized[(arm, session, r.repetition)][0] is not None for r in audits
                ),
                "upkeep_tokens_between_questions": _mean(
                    [r.between_questions_tokens for r in audits]
                ),
                "upkeep_compute_between_questions_seconds": _mean(
                    [r.between_questions_compute_seconds for r in audits]
                ),
                "first_useful_state_tokens": _mean([cost for cost, _turns in useful]),
                "first_useful_state_turns": _mean([turns for _cost, turns in useful]),
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
                **{
                    f"{surface}_actions": sum(
                        a.calls for r in events for a in r.actions if a.surface == surface
                    )
                    / max(len(audits), 1)
                    for surface in ("mcp", "cli", "sdk")
                },
                "runaway_hits": sum(r.runaway is not None for r in events),
            }
        )
    return rows


def surfaces(records: list[Record]) -> list[dict[str, object]]:
    """Which Cruxible surface (MCP tool, CLI subcommand, SDK call) agents chose, per session."""
    calls: dict[tuple[str, int, str, str, str], int] = defaultdict(int)
    for record in records:
        for action in record.actions:
            key = (record.arm, record.session_index, record.kind, action.surface, action.name)
            calls[key] += action.calls
    return [
        {
            "arm": arm,
            "session_index": session,
            "phase": phase,
            "surface": surface,
            "name": name,
            "calls": count,
        }
        for (arm, session, phase, surface, name), count in sorted(calls.items())
    ]


SPREAD_METRICS = (
    "question_tokens",
    "question_cost_usd",
    "question_wall_seconds",
    "session_tokens",
    "session_cost_usd",
    "amortized_tokens_per_question",
    "flagged_answers",
)


def spread(records: list[Record]) -> list[dict[str, object]]:
    """Across repetitions: n, mean, min, max and standard deviation of per-repetition values."""
    values: dict[tuple[str, int, str], list[float]] = defaultdict(list)
    for repetition in sorted({r.repetition for r in records}):
        for row in summarize([r for r in records if r.repetition == repetition]):
            for metric in SPREAD_METRICS:
                if row.get(metric) is not None:
                    key = (str(row["arm"]), int(str(row["session_index"])), metric)
                    values[key].append(float(str(row[metric])))
    return [
        {
            "arm": arm,
            "session_index": session,
            "metric": metric,
            "n": len(items),
            "mean": mean(items),
            "min": min(items),
            "max": max(items),
            "stdev": stdev(items) if len(items) > 1 else None,
        }
        for (arm, session, metric), items in sorted(values.items())
    ]


def summarize_setup(records: list[Record]) -> list[dict[str, object]]:
    """Setup cost per arm, summed over setup steps then averaged over repetitions."""
    runs: dict[str, dict[int, list[Record]]] = defaultdict(lambda: defaultdict(list))
    for record in records:
        if record.kind == "setup":
            runs[record.arm][record.repetition].append(record)
    rows: list[dict[str, object]] = []
    for arm, repetitions in sorted(runs.items()):
        row: dict[str, object] = {"arm": arm, "repetitions": len(repetitions)}
        for name, get in USAGE.items():
            row[f"setup_{name}"] = _mean(
                [_sum([get(r) for r in steps]) for steps in repetitions.values()]
            )
        row["runaway_hits"] = sum(
            r.runaway is not None for steps in repetitions.values() for r in steps
        )
        rows.append(row)
    return rows


TREND_METRICS = (
    "question_tokens",
    "question_total_tokens",
    "question_turns",
    "question_cost_usd",
    "question_wall_seconds",
    "question_model_calls",
    "session_tokens",
    "session_cost_usd",
    "session_wall_seconds",
    "amortized_tokens_per_question",
    "amortized_cost_per_question",
    "flagged_answers",
)


def _slope(points: list[tuple[int, float]]) -> float | None:
    if len(points) < 2:
        return None
    xs, ys = [x for x, _ in points], [y for _, y in points]
    x_mean, y_mean = mean(xs), mean(ys)
    spread = sum((x - x_mean) ** 2 for x in xs)
    return sum((x - x_mean) * (y - y_mean) for x, y in points) / spread if spread else None


def trends(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Session 1 -> N per arm and metric: first/last value, change and least-squares slope."""
    out: list[dict[str, object]] = []
    for arm in sorted({str(row["arm"]) for row in rows}):
        selected = [row for row in rows if row["arm"] == arm]
        for metric in TREND_METRICS:
            points = [
                (int(str(row["session_index"])), float(str(row[metric])))
                for row in selected
                if row.get(metric) is not None
            ]
            if not points:
                continue
            (first_session, first), (last_session, last) = points[0], points[-1]
            out.append(
                {
                    "arm": arm,
                    "metric": metric,
                    "first_session": first_session,
                    "first": first,
                    "last_session": last_session,
                    "last": last,
                    "change_pct": 100 * (last - first) / first if first else None,
                    "slope_per_session": _slope(points),
                    "points": len(points),
                }
            )
    return out


def _cell(value: object) -> str:
    if value is None:
        return ""
    return f"{value:.4g}" if isinstance(value, float) else str(value)


def _write_csv(path: Path, rows: list[dict[str, object]], fallback: list[str]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else fallback)
        writer.writeheader()
        writer.writerows(rows)


def write_outputs(records: list[Record], directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    rows = summarize(records)
    setup = summarize_setup(records)
    trend = trends(rows)
    # CSV is always available, including where matplotlib is not installed.
    _write_csv(directory / "summary.csv", rows, ["arm", "session_index"])
    _write_csv(directory / "setup.csv", setup, ["arm", "repetitions"])
    _write_csv(directory / "trend.csv", trend, ["arm", "metric"])
    _write_csv(directory / "spread.csv", spread(records), ["arm", "session_index", "metric"])
    _write_csv(directory / "surfaces.csv", surfaces(records), ["arm", "session_index", "surface"])
    visible = [
        "arm",
        "session_index",
        "reviewed_answers",
        "flagged_answers",
        "question_tokens",
        "question_cost_usd",
        "question_wall_seconds",
        "session_tokens",
        "amortized_tokens_per_question",
        "mcp_actions",
        "cli_actions",
        "sdk_actions",
        "runaway_hits",
    ]

    def table(columns: list[str], items: list[dict[str, object]]) -> str:
        lines = [
            "| " + " | ".join(columns) + " |",
            "| " + " | ".join("---" for _ in columns) + " |",
        ]
        for item in items:
            lines.append("| " + " | ".join(_cell(item.get(key)) for key in columns) + " |")
        return "\n".join(lines) + "\n"

    with (directory / "summary.md").open("w") as handle:
        if records and all(r.dry_run for r in records):
            handle.write("Synthetic dry run; no model was called.\n\n")
        handle.write("## Sessions (setup excluded)\n\n" + table(visible, rows))
        handle.write(
            "\n## Setup (off the clock)\n\n"
            + table(
                ["arm", "repetitions", "setup_tokens", "setup_cost_usd", "setup_wall_seconds"],
                setup,
            )
        )
        handle.write(
            "\n## Trend, session 1 to N\n\n"
            + table(["arm", "metric", "first", "last", "change_pct", "slope_per_session"], trend)
        )
    try:
        import matplotlib  # type: ignore[import-not-found]  # Optional plotting dependency.
    except ImportError:
        status = {"generated": False, "reason": "matplotlib is not installed; use summary.csv"}
    else:
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # type: ignore[import-not-found]

        fields = ("question_tokens", "question_cost_usd", "session_tokens")
        labels = ("Tokens / question", "Cost / question (USD)", "Audit tokens / session")
        figure, axes = plt.subplots(len(fields), 1, sharex=True, figsize=(8, 9))
        for arm in sorted({str(row["arm"]) for row in rows}):
            selected = [row for row in rows if row["arm"] == arm]
            for axis, field in zip(axes, fields, strict=True):
                valid = [row for row in selected if row[field] is not None]
                if valid:
                    axis.plot(
                        [row["session_index"] for row in valid],
                        [row[field] for row in valid],
                        marker="o",
                        label=arm,
                    )
        for axis, label in zip(axes, labels, strict=True):
            axis.set_ylabel(label)
            axis.grid(alpha=0.2)
            if axis.lines:
                axis.legend(title="Arm")
        axes[-1].set_xlabel("Session index")
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
