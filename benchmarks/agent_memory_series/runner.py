"""An agent-independent loop; only the scripted adapter is executable out of the box."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from .adapters import AgentAdapter, CLIAdapterStub, Invocation
from .config import BenchmarkConfig, Budget, Patch, Question, Session, digest
from .grading import Grader, ManualGrader, export_sheet
from .results import Record
from .summary import write_outputs
from .workspace import apply_changes, prepare_context, size_metrics, source_snapshot


def freeze(config: BenchmarkConfig, directory: Path) -> BenchmarkConfig:
    sessions = []
    for session in config.sessions:
        changes = []
        for change in session.changes:
            if isinstance(change, Patch) and change.path is not None:
                change = Patch(
                    description=change.description,
                    text=(directory / change.path).read_bytes().decode("utf-8"),
                )
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
    grader: Grader | None = None,
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
    grader = grader or ManualGrader()
    data = config.model_dump(mode="json")
    fingerprint = digest(data)
    (output / "frozen-config.json").write_text(json.dumps(data, indent=2) + "\n")
    manifest = {
        "version": 1,
        "name": config.name,
        "config_digest": fingerprint,
        "adapter": adapter.name,
        "dry_run": adapter.dry_run,
        "grader": grader.name,
        "started_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "source_snapshots": {},
        "limits": [
            "No live adapter or model grader is shipped",
            "No benchmark result is claimed by a dry run",
        ],
    }
    records: list[Record] = []
    snapshots: dict[int, str] = {}

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
                    try:
                        run_id = f"{config.name}-{arm.id}-{repetition}"
                        budget_session: int | None = None
                        spent_tokens: int | None = 0
                        spent_turns: int | None = 0
                        spent_seconds = 0.0

                        def invoke(
                            invocation: Invocation, question: Question | None = None
                        ) -> Record:
                            nonlocal budget_session, spent_tokens, spent_turns, spent_seconds
                            if budget_session != invocation.session_index:
                                budget_session = invocation.session_index
                                spent_tokens = spent_turns = 0
                                spent_seconds = 0.0
                            available_tokens = config.budget.max_tokens - (spent_tokens or 0)
                            available_turns = config.budget.max_turns - (spent_turns or 0)
                            available_seconds = config.budget.max_seconds - spent_seconds
                            if min(available_tokens, available_turns, available_seconds) <= 0:
                                raise ValueError(
                                    "session budget exhausted before its next invocation"
                                )
                            remaining = Budget(
                                max_tokens=available_tokens,
                                max_turns=available_turns,
                                max_seconds=available_seconds,
                            )
                            before = source_snapshot(context)
                            started = time.perf_counter()
                            reply = adapter.run(replace(context, budget=remaining), invocation)
                            wall = time.perf_counter() - started
                            tokens = reply.usage.tokens
                            spent_tokens = (
                                None
                                if spent_tokens is None or tokens is None
                                else spent_tokens + tokens
                            )
                            turns = reply.usage.turns
                            spent_turns = (
                                None
                                if spent_turns is None or turns is None
                                else spent_turns + turns
                            )
                            spent_seconds += wall
                            if source_snapshot(context) != before:
                                raise ValueError(
                                    "agent modified target source; "
                                    "configure all memory paths explicitly"
                                )
                            exceeded = (
                                spent_tokens is not None
                                and spent_tokens > config.budget.max_tokens
                                or spent_turns is not None
                                and spent_turns > config.budget.max_turns
                                or tokens is not None
                                and tokens > available_tokens
                                or turns is not None
                                and turns > available_turns
                                or spent_seconds > config.budget.max_seconds
                            )
                            record = Record(
                                kind=invocation.phase,
                                run_id=run_id,
                                arm=arm.id,
                                repetition=repetition,
                                session_index=invocation.session_index,
                                adapter=adapter.name,
                                dry_run=adapter.dry_run,
                                prompt=invocation.prompt,
                                output=reply.text,
                                usage=reply.usage,
                                wall_seconds=wall,
                                budget_exceeded=exceeded,
                                first_useful_state=reply.first_useful_state,
                                source=before,
                                size=size_metrics(context, before, reply.memory)
                                if invocation.phase == "session"
                                else None,
                                upkeep_compute_seconds=reply.upkeep_compute_seconds,
                                question=question,
                                grade=None
                                if question is None
                                else grader.grade(question, invocation.session_index, reply.text),
                            )
                            return record

                        def append(record: Record) -> None:
                            records.append(record)
                            results.write(record.model_dump_json() + "\n")
                            results.flush()
                            if record.budget_exceeded:
                                raise ValueError(
                                    f"adapter exceeded budget in {record.run_id}/"
                                    f"{record.kind}/{record.session_index}"
                                )

                        initial = source_snapshot(context)
                        if snapshots.setdefault(0, initial.digest) != initial.digest:
                            raise ValueError(
                                "base source snapshots differ between arms/repetitions"
                            )
                        append(
                            invoke(
                                Invocation(
                                    phase="setup", session_index=0, prompt=config.setup_prompt
                                )
                            )
                        )
                        upkeep_tokens: int | float | None = 0
                        upkeep_compute: int | float | None = 0
                        for index, session in enumerate(config.sessions, 1):
                            logs = apply_changes(
                                context, session.changes, config_directory or Path.cwd()
                            )
                            (context.run_root / f"changes-{index:03d}.json").write_text(
                                json.dumps(logs, indent=2) + "\n"
                            )
                            source = source_snapshot(context)
                            if snapshots.setdefault(index, source.digest) != source.digest:
                                raise ValueError(
                                    f"source snapshots differ at session {index}; "
                                    "change script must be deterministic"
                                )
                            manifest["source_snapshots"] = {str(k): v for k, v in snapshots.items()}
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
                    finally:
                        adapter.close(context)
        if digest(config.model_dump(mode="json")) != fingerprint:
            raise ValueError("frozen configuration/rubrics were mutated during the run")
        manifest["status"] = "completed"
    except Exception as exc:
        manifest.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        manifest["usage_complete"] = bool(records) and all(
            r.usage.tokens is not None and r.usage.turns is not None for r in records
        )
        manifest["finished_at"] = datetime.now(UTC).isoformat()
        save_manifest()
        write_outputs(records, output)
        export_sheet(records, output / "grading.csv")
    return records
