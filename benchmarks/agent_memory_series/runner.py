"""An agent-independent loop; only the scripted adapter is executable out of the box."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO, cast

from .adapters import AgentAdapter, CLIAdapterStub, Context, Invocation, Usage
from .config import BenchmarkConfig, Feed, Patch, Question, Session, SetupAgent, digest
from .grading import export_sheets
from .results import Record
from .summary import write_outputs
from .workspace import (
    apply_changes,
    feeds_snapshot,
    prepare_context,
    run_setup_command,
    size_metrics,
    source_snapshot,
    stop_process,
)


def freeze(config: BenchmarkConfig, directory: Path) -> BenchmarkConfig:
    """Inline every patch/feed file so later edits cannot change the experiment."""
    sessions = []
    for session in config.sessions:
        changes = []
        for change in session.changes:
            if isinstance(change, Patch | Feed) and change.path is not None:
                text = (directory / change.path).read_bytes().decode("utf-8")
                change = change.model_copy(update={"path": None, "text": text})
            changes.append(change)
        sessions.append(
            Session(id=session.id, description=session.description, changes=tuple(changes))
        )
    target = config.target
    if target is not None and (directory / target.git_url).exists():
        target = target.model_copy(update={"git_url": str((directory / target.git_url).resolve())})
    return BenchmarkConfig.model_validate(
        {
            **config.model_dump(),
            "sessions": tuple(sessions),
            "target": target,
        }
    )


def _add(total: int | float | None, value: int | float | None) -> int | float | None:
    return None if total is None or value is None else total + value


def run_benchmark(
    config: BenchmarkConfig,
    *,
    output: Path,
    adapter: AgentAdapter,
    config_directory: Path | None = None,
    offline: bool = True,
) -> list[Record]:
    if isinstance(adapter, CLIAdapterStub):
        raise NotImplementedError(
            "Live CLI execution is intentionally stubbed; use a scripted dry run"
        )
    if config.target is None:
        raise ValueError("target is an unfilled slot; supply git_url and pinned base_commit")
    config = freeze(config, config_directory or Path.cwd())
    target = config.target
    assert target is not None
    if offline and not Path(target.git_url).is_dir():
        raise ValueError("offline runs require a local fixture Git repository, not a network URL")
    if any(part.startswith(".cruxible") for part in output.absolute().parts):
        raise ValueError("output must be a fresh scratch directory outside .cruxible state")
    output = output.resolve()
    target_path = Path(target.git_url).resolve() if Path(target.git_url).is_dir() else None
    if target_path is not None and (output == target_path or target_path in output.parents):
        raise ValueError("output must be outside the target repository")
    output.mkdir(parents=True)  # refuses to reuse/overwrite a previous run
    data = config.model_dump(mode="json")
    fingerprint = digest(data)
    (output / "frozen-config.json").write_text(json.dumps(data, indent=2) + "\n")
    manifest: dict[str, Any] = {
        "version": 2,
        "name": config.name,
        "config_digest": fingerprint,
        "adapter": adapter.name,
        "dry_run": adapter.dry_run,
        "guard": config.guard.model_dump(mode="json"),
        "started_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "source_snapshots": {},
        "feed_snapshots": {},
        "runaway_hits": 0,
        "runs": {},
        "limits": [
            "No live adapter is shipped",
            "No benchmark result is claimed by a dry run",
        ],
    }
    records: list[Record] = []
    snapshots: dict[int, str] = {}
    feeds: dict[int, str] = {}

    def save_manifest() -> None:
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    save_manifest()
    try:
        with (output / "results.jsonl").open("x") as results:
            for arm in config.arms:
                for repetition in range(1, config.repetitions + 1):
                    context = prepare_context(
                        config, arm, repetition, output / "runs" / arm.id / f"rep-{repetition:03d}"
                    )
                    background: list[subprocess.Popen[bytes]] = []
                    try:
                        _run_arm(
                            config,
                            context,
                            adapter,
                            config_directory or Path.cwd(),
                            background,
                            snapshots,
                            feeds,
                            records,
                            results,
                            manifest,
                            save_manifest,
                        )
                    finally:
                        try:
                            adapter.close(context)
                        finally:
                            for process in background:
                                stop_process(process)
        if digest(config.model_dump(mode="json")) != fingerprint:
            raise ValueError("frozen configuration was mutated during the run")
        manifest["status"] = "completed"
    except Exception as exc:
        manifest.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        manifest["usage_complete"] = bool(records) and all(
            r.usage.tokens is not None and r.usage.turns is not None for r in records
        )
        manifest["cost_complete"] = bool(records) and all(
            r.usage.cost_usd is not None for r in records
        )
        manifest["finished_at"] = datetime.now(UTC).isoformat()
        save_manifest()
        write_outputs(records, output)
        export_sheets(records, output / "review")
    return records


def _run_arm(
    config: BenchmarkConfig,
    context: Context,
    adapter: AgentAdapter,
    directory: Path,
    background: list[subprocess.Popen[bytes]],
    snapshots: dict[int, str],
    feeds: dict[int, str],
    records: list[Record],
    results: TextIO,
    manifest: dict[str, Any],
    save_manifest: Callable[[], None],
) -> None:
    arm, repetition = context.arm, context.repetition
    run_id = f"{config.name}-{arm.id}-{repetition}"
    guard = config.guard

    def invoke(invocation: Invocation, question: Question | None = None) -> Record:
        before = source_snapshot(context)
        feeds_before = feeds_snapshot(context)
        started = time.perf_counter()
        reply = adapter.run(context, invocation)
        wall = time.perf_counter() - started
        if source_snapshot(context) != before:
            raise ValueError(
                "agent modified target source; configure all memory/tooling paths explicitly"
            )
        # Frozen feeds are inputs for every arm: a later invocation must never
        # read data an earlier one rewrote.
        if feeds_snapshot(context) != feeds_before:
            raise ValueError("agent modified the frozen advisory feeds")
        cost = reply.usage.cost_usd
        runaway = reply.stopped_by_guard or (
            "wall_clock"
            if wall > guard.max_seconds_per_invocation
            else "cost"
            if cost is not None
            and guard.max_cost_usd_per_invocation is not None
            and cost > guard.max_cost_usd_per_invocation
            else None
        )
        return Record(
            kind=invocation.phase,
            setup_kind="agent" if invocation.phase == "setup" else None,
            run_id=run_id,
            arm=arm.id,
            repetition=repetition,
            session_index=invocation.session_index,
            setup_step=invocation.setup_step,
            adapter=adapter.name,
            dry_run=adapter.dry_run,
            prompt=invocation.prompt,
            output=reply.text,
            usage=reply.usage,
            wall_seconds=wall,
            runaway=runaway,
            first_useful_state=reply.first_useful_state,
            model_id=reply.model_id,
            actions=reply.actions,
            source=before,
            size=size_metrics(context, before, reply.memory)
            if invocation.phase == "session"
            else None,
            upkeep_compute_seconds=reply.upkeep_compute_seconds,
            question=question,
        )

    def append(record: Record) -> None:
        records.append(record)
        results.write(record.model_dump_json() + "\n")
        results.flush()
        if record.runaway is not None:
            # An outcome, not a failure: record it and carry on with the series.
            manifest["runaway_hits"] += 1
            save_manifest()

    manifest["runs"][f"{arm.id}/{repetition}"] = {
        **adapter.versions(context),
        "integrations": {
            key: value.model_dump(mode="json")
            for key, value in (("skill", arm.skill), ("plugin", arm.plugin), ("kit", arm.kit))
            if value is not None
        },
    }
    save_manifest()
    initial = source_snapshot(context)
    if snapshots.setdefault(0, initial.digest) != initial.digest:
        raise ValueError("base source snapshots differ between arms/repetitions")
    initial_feeds = feeds_snapshot(context)
    # Setup is off the clock: recorded as kind="setup", excluded from session metrics.
    setup_logs: list[dict[str, object]] = []
    for step_index, step in enumerate(arm.setup):
        if isinstance(step, SetupAgent):
            append(
                invoke(
                    Invocation(
                        phase="setup",
                        session_index=0,
                        prompt=step.prompt,
                        setup_step=step_index,
                    )
                )
            )
            setup_logs.append({"step": step.model_dump(mode="json"), "agent": True})
            continue
        log, process = run_setup_command(context, step)
        if process is not None:
            background.append(process)
        setup_logs.append(log)
        # Command steps are setup too: recorded with their wall time and no model usage.
        append(
            Record(
                kind="setup",
                setup_kind="command",
                run_id=run_id,
                arm=arm.id,
                repetition=repetition,
                session_index=0,
                setup_step=step_index,
                adapter="harness",
                dry_run=adapter.dry_run,
                prompt=step.description,
                output=str(log.get("stdout", "")),
                usage=Usage.none_used(),
                wall_seconds=cast(float, log["wall_seconds"]),
                source=initial,
            )
        )
    (context.run_root / "setup.json").write_text(json.dumps(setup_logs, indent=2) + "\n")
    if source_snapshot(context) != initial:
        raise ValueError("setup modified target source; declare installed tooling paths")
    if feeds_snapshot(context) != initial_feeds:
        raise ValueError("setup modified the frozen advisory feeds")
    upkeep_tokens: int | float | None = 0
    upkeep_compute: int | float | None = 0
    for index, session in enumerate(config.sessions, 1):
        logs = apply_changes(context, session.changes, directory)
        (context.run_root / f"changes-{index:03d}.json").write_text(
            json.dumps(logs, indent=2) + "\n"
        )
        source = source_snapshot(context)
        if snapshots.setdefault(index, source.digest) != source.digest:
            raise ValueError(
                f"source snapshots differ at session {index}; change script must be deterministic"
            )
        feed = feeds_snapshot(context)
        if feeds.setdefault(index, feed) != feed:
            raise ValueError(f"advisory feed snapshots differ at session {index}")
        manifest["source_snapshots"] = {str(k): v for k, v in snapshots.items()}
        manifest["feed_snapshots"] = {str(k): v for k, v in feeds.items()}
        save_manifest()
        record = invoke(
            Invocation(
                phase="session",
                session_index=index,
                prompt=config.session_prompt + "\n\n" + session.description,
            )
        )
        upkeep_tokens = _add(upkeep_tokens, record.usage.tokens)
        upkeep_compute = _add(upkeep_compute, record.upkeep_compute_seconds)
        scheduled = [q for q in config.questions if index in q.after_sessions]
        if scheduled:
            record = record.model_copy(
                update={
                    "between_questions_tokens": upkeep_tokens,
                    "between_questions_compute_seconds": upkeep_compute,
                }
            )
        append(record)
        for question in scheduled:
            append(
                invoke(
                    Invocation(
                        phase="question",
                        session_index=index,
                        prompt=question.prompt,
                        question_id=question.id,
                        question_kind=question.kind,
                    ),
                    question=question,
                )
            )
        if scheduled:
            upkeep_tokens = upkeep_compute = 0
