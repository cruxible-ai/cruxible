"""No-network fixture: scheduling, persistent memory, grading and accounting end to end."""

from __future__ import annotations

import csv
import json
import socket
import subprocess
from pathlib import Path

import pytest
import yaml
from benchmarks.agent_memory_series.__main__ import main
from benchmarks.agent_memory_series.adapters import (
    CLIAdapterStub,
    Context,
    Invocation,
    MemoryReport,
    Reply,
    ScriptedAdapter,
    Usage,
)
from benchmarks.agent_memory_series.config import BenchmarkConfig, load_config
from benchmarks.agent_memory_series.demo import FIX
from benchmarks.agent_memory_series.demo import create_fixture as fixture_config
from benchmarks.agent_memory_series.grading import (
    ManualGrader,
    ModelGraderStub,
    apply_sheet,
    export_sheet,
)
from benchmarks.agent_memory_series.results import CriterionResult, Grade, load_results
from benchmarks.agent_memory_series.runner import run_benchmark
from benchmarks.agent_memory_series.summary import summarize
from pydantic import ValidationError


class FixtureGrader:
    name = "scripted_fixture_grader"

    def grade(self, question, session_index, answer):
        return Grade(
            grader=self.name,
            criteria=tuple(
                CriterionResult(
                    criterion_id=c.id, passed=("fixed" if session_index == 2 else "live") in answer
                )
                for c in question.criteria
            ),
        )


def responder(calls):
    def respond(context: Context, invocation: Invocation) -> Reply:
        calls.append((context, invocation))
        assert "CRUXIBLE_SERVER_URL" not in context.environment
        assert "CRUXIBLE_INSTANCE_ID" not in context.environment
        assert Path(context.environment["CRUXIBLE_STATE_ROOT"]).is_relative_to(context.run_root)
        assert "criteria" not in invocation.model_dump()
        if invocation.phase == "setup":
            return Reply(
                text="Prepared synthetic memory",
                usage=Usage(input_tokens=3, output_tokens=2, turns=1),
            )
        if invocation.phase == "question":
            assert (context.workspace / context.arm.memory_paths[0]).exists()
            live = "True" in (context.workspace / "app.py").read_text()
            return Reply(
                text=("live" if live else "fixed") + "; dependent endpoint; evidence app.py:1",
                usage=Usage(
                    input_tokens=8 * invocation.session_index,
                    output_tokens=2 * invocation.session_index,
                    turns=2,
                ),
            )
        path = context.workspace / context.arm.memory_paths[0]
        if path.suffix != ".json":
            path.mkdir(exist_ok=True)
            path /= "findings.json"
        if invocation.session_index > 1:
            assert json.loads(path.read_text())["session"] == invocation.session_index - 1
        else:
            assert not path.exists()
        path.write_text(json.dumps({"session": invocation.session_index}))
        if context.state_root is not None:
            (context.state_root / "synthetic-state.json").write_text(path.read_text())
        report = (
            MemoryReport()
            if context.arm.id == "A"
            else MemoryReport(
                claims=invocation.session_index,
                copy_in_assessed_claims=invocation.session_index,
                copy_in_failed_claims=0,
                source="scripted fixture, not a real Cruxible instance",
            )
        )
        return Reply(
            text="Synthetic audit",
            usage=Usage(
                input_tokens=80 * invocation.session_index,
                output_tokens=20 * invocation.session_index,
                turns=3,
                compute_seconds=0.1 * invocation.session_index,
            ),
            memory=report,
            upkeep_compute_seconds=0.1 * invocation.session_index,
            first_useful_state=invocation.session_index == 1,
        )

    return respond


@pytest.fixture
def completed(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("fixture benchmark attempted network access")

    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setenv("CRUXIBLE_SERVER_URL", "http://must-not-be-used.invalid")
    monkeypatch.setenv("CRUXIBLE_INSTANCE_ID", "must-not-be-used")
    config = fixture_config(tmp_path)
    calls = []
    output = tmp_path / "results"
    records = run_benchmark(
        config,
        output=output,
        adapter=ScriptedAdapter(respond=responder(calls)),
        grader=FixtureGrader(),
    )
    return config, output, records, calls


def test_dry_run_loop_scheduling_isolation_and_metrics(completed):
    config, output, records, calls = completed
    # For each of 3 arms x 2 repetitions: setup + 3 audits + 5 questions.
    assert len(records) == len(calls) == 54
    assert records == load_results(output / "results.jsonl")
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["status"] == "completed" and manifest["dry_run"] is True
    snapshots = manifest["source_snapshots"]
    assert snapshots["0"] == snapshots["1"] == snapshots["3"] != snapshots["2"]
    contexts = {(ctx.arm.id, ctx.repetition): ctx for ctx, _ in calls}
    assert len({ctx.workspace for ctx in contexts.values()}) == 6
    assert len({ctx.state_root for ctx in contexts.values() if ctx.state_root}) == 4
    assert (
        config.target.base_commit
        == subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=config.target.git_url)
        .decode()
        .strip()
    )
    for record in records:
        assert record.source.files == 2
        if record.kind == "question" and record.question.kind == "contradiction":
            assert record.session_index in (2, 3)
        if record.kind == "session":
            assert record.size.state_bytes > 0
            assert (
                record.size.state_to_source_ratio == record.size.state_bytes / record.source.bytes
            )
            if record.arm != "A":
                assert record.size.claims_per_source_file == record.session_index / 2
                assert record.size.copy_in_failure_share == 0
    rows = summarize(records)
    first = next(row for row in rows if row["arm"] == "A" and row["session_index"] == 1)
    assert first["criteria_met_pct"] == 100
    assert first["tokens_per_question"] == 10
    assert first["amortized_tokens_per_question"] == 115
    assert first["upfront_tokens_to_useful_state"] == 105
    assert first["upfront_turns_to_useful_state"] == 4
    second = next(row for row in rows if row["arm"] == "B" and row["session_index"] == 2)
    assert second["upkeep_tokens_between_questions"] == 200
    assert second["upkeep_compute_between_questions_seconds"] == pytest.approx(0.2)
    assert second["amortized_tokens_per_question"] == pytest.approx(355 / 3)
    assert (output / "summary.csv").exists() and (output / "summary.md").exists()
    assert (output / "amortization.csv").exists()
    plot = json.loads((output / "plot-status.json").read_text())
    assert plot["generated"] or "matplotlib" in plot["reason"]
    assert "Synthetic dry run" in (output / "summary.md").read_text()


def test_manual_grading_round_trip_preserves_missing_scores_and_raw_answers(completed, tmp_path):
    _config, output, original, _calls = completed
    records = [
        record.model_copy(
            update={
                "grade": ManualGrader().grade(record.question, record.session_index, record.output)
            }
        )
        if record.question
        else record
        for record in original
    ]
    assert all(row["criteria_met_pct"] is None for row in summarize(records))
    sheet = tmp_path / "manual.csv"
    export_sheet(records, sheet)
    with sheet.open(newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames
        rows = list(reader)
    assert all(not row["arm"] for row in rows)
    chosen = next(row["response_id"] for row in rows if row["kind"] == "question")
    for row in rows:
        if row["kind"] == "question":
            row["passed"] = "false" if row["response_id"] == chosen else "true"
        row["stale_answer_as_current"] = "true" if row["response_id"] == chosen else "false"
        row["lost_finding"] = "false"
    with sheet.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    graded = apply_sheet(records, sheet)
    assert [r.output for r in graded] == [r.output for r in records]
    assert any(row["criteria_met_pct"] < 100 for row in summarize(graded))
    assert sum(row["stale_answer_as_current"] for row in summarize(graded)) == 1
    raw = (output / "results.jsonl").read_bytes()
    assert (
        main(
            [
                "grade",
                "--results",
                str(output / "results.jsonl"),
                "--sheet",
                str(sheet),
                "--output",
                str(tmp_path / "graded"),
            ]
        )
        == 0
    )
    assert (output / "results.jsonl").read_bytes() == raw
    assert (tmp_path / "graded" / "results.jsonl").exists()
    criterion_row = next(row for row in rows if row["criterion_id"])
    criterion_row["criterion"] += " edited after the run"
    with sheet.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(ValueError, match="frozen rubric"):
        apply_sheet(records, sheet)
    criterion_row["criterion"] = criterion_row["criterion"].removesuffix(" edited after the run")
    rows.append(rows[-1])
    with sheet.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(ValueError, match="duplicate criterion"):
        apply_sheet(records, sheet)


def test_noop_cli_and_configuration_files(tmp_path):
    config = fixture_config(tmp_path)
    data = config.model_dump(mode="json")
    data["repetitions"] = 1
    data["arms"] = data["arms"][:2]
    questions = data.pop("questions")
    data["questions_file"] = "questions.yaml"
    (tmp_path / "questions.yaml").write_text(yaml.safe_dump(questions))
    (tmp_path / "series.json").write_text(json.dumps(data))
    assert load_config(tmp_path / "series.json").questions == config.questions
    assert (
        main(["run", "--config", str(tmp_path / "series.json"), "--output", str(tmp_path / "noop")])
        == 0
    )
    records = load_results(tmp_path / "noop" / "results.jsonl")
    assert all(r.dry_run and r.usage.tokens == 0 for r in records)
    assert all(row["criteria_met_pct"] is None for row in summarize(records))
    with pytest.raises(FileExistsError):
        run_benchmark(config, output=tmp_path / "noop", adapter=ScriptedAdapter())


@pytest.mark.parametrize(
    "mutation",
    [
        {"target": {"git_url": "https://example.invalid/repo", "base_commit": "HEAD"}},
        {"repetitions": 0},
        {"budget": {"max_tokens": -1, "max_turns": 1}},
        {
            "questions": [
                {
                    "id": "bad",
                    "kind": "control",
                    "prompt": "x",
                    "criteria": [{"id": "one", "description": "x"}],
                    "after_sessions": [99],
                }
            ]
        },
    ],
)
def test_configuration_rejects_unpinned_or_invalid_experiments(tmp_path, mutation):
    data = fixture_config(tmp_path).model_dump(mode="json")
    with pytest.raises(ValidationError):
        BenchmarkConfig.model_validate({**data, **mutation})


def test_stubs_and_unfilled_slots_never_launch_real_clients(tmp_path):
    path = (
        Path(__file__).resolve().parents[2] / "benchmarks/agent_memory_series/examples/series.yaml"
    )
    template = load_config(path)
    with pytest.raises(ValueError, match="unfilled slot"):
        run_benchmark(template, output=tmp_path / "unfilled", adapter=ScriptedAdapter())
    config = fixture_config(tmp_path)
    with pytest.raises(NotImplementedError, match="stubbed"):
        run_benchmark(config, output=tmp_path / "live", adapter=CLIAdapterStub("codex"))
    assert not (tmp_path / "live").exists()
    context = Context(
        arm=config.arms[1],
        repetition=1,
        model=config.model,
        budget=config.budget,
        workspace=tmp_path / "workspace",
        state_root=tmp_path / "isolated-state",
        run_root=tmp_path,
        environment={"CRUXIBLE_STATE_ROOT": str(tmp_path / "isolated-state")},
    )
    invocation = Invocation(phase="setup", session_index=0, prompt=config.setup_prompt)
    for client in ("claude-code", "codex"):
        plan = CLIAdapterStub(client).plan(context, invocation)
        assert plan.arm.plugin.name == "shipped-cruxible-plugin"
        assert plan.arm.kit.name == "security"
        assert plan.budget == config.budget
        assert plan.environment["CRUXIBLE_STATE_ROOT"] == str(tmp_path / "isolated-state")
    with pytest.raises(NotImplementedError, match="no model grading"):
        ModelGraderStub().grade(config.questions[0], 1, "answer")


def test_source_mutation_and_over_budget_runs_are_marked_failed(tmp_path):
    config = fixture_config(tmp_path)

    def edits(context, invocation):
        (context.workspace / "app.py").write_text("modified by agent")
        return Reply()

    closed = []
    with pytest.raises(ValueError, match="modified target source"):
        run_benchmark(
            config,
            output=tmp_path / "mutates",
            adapter=ScriptedAdapter(respond=edits, cleanup=closed.append),
        )
    assert len(closed) == 1 and closed[0].arm.id == "A"
    assert json.loads((tmp_path / "mutates/manifest.json").read_text())["status"] == "failed"
    over = ScriptedAdapter(
        respond=lambda *_: Reply(usage=Usage(input_tokens=1001, output_tokens=0, turns=1))
    )
    with pytest.raises(ValueError, match="exceeded budget"):
        run_benchmark(config, output=tmp_path / "over", adapter=over)
    records = load_results(tmp_path / "over/results.jsonl")
    assert len(records) == 1 and records[0].budget_exceeded
    assert json.loads((tmp_path / "over/manifest.json").read_text())["status"] == "failed"


def test_patch_inputs_are_frozen_before_any_adapter_call(tmp_path):
    config = fixture_config(tmp_path)
    patch = tmp_path / "fix.patch"
    patch.write_text(FIX)
    data = config.model_dump(mode="json")
    data["sessions"][1]["changes"] = [
        {"kind": "patch", "description": "Apply the fixture fix", "path": "fix.patch"}
    ]
    config = BenchmarkConfig.model_validate(data)
    calls = []
    original = responder(calls)

    def mutate_input(context, invocation):
        patch.write_text("invalid new patch; the frozen original must still be used")
        return original(context, invocation)

    records = run_benchmark(
        config,
        output=tmp_path / "frozen",
        config_directory=tmp_path,
        adapter=ScriptedAdapter(respond=mutate_input),
    )
    assert len(records) == 54
    frozen = json.loads((tmp_path / "frozen/frozen-config.json").read_text())
    assert frozen["sessions"][1]["changes"][0]["text"] == FIX


def test_unknown_usage_and_partial_grades_remain_missing(completed):
    _config, _output, records, _calls = completed
    changed = []
    for record in records:
        if (
            record.arm == "A"
            and record.repetition == 1
            and record.kind == "question"
            and record.session_index == 1
        ):
            record = record.model_copy(
                update={
                    "usage": Usage(),
                    "grade": ManualGrader().grade(record.question, 1, record.output),
                }
            )
        changed.append(record)
    row = next(r for r in summarize(changed) if r["arm"] == "A" and r["session_index"] == 1)
    assert row["criteria_met_pct"] is None
    assert row["criteria_graded"] == 1 and row["criteria_total"] == 2
    assert row["tokens_per_question"] == 10 and row["questions_with_token_usage"] == 1
    assert row["repetitions_with_complete_cost"] == 1
    assert row["stale_answer_as_current"] is None and row["lost_finding"] is None


def test_sparse_question_batches_count_upkeep_once_and_close_each_arm(tmp_path):
    config = fixture_config(tmp_path)
    data = config.model_dump(mode="json")
    data["arms"] = data["arms"][:2]
    data["repetitions"] = 1
    for question in data["questions"]:
        question["after_sessions"] = [3]
        for criterion in question["criteria"]:
            criterion["expected_by_session"] = {
                k: v for k, v in criterion["expected_by_session"].items() if str(k) == "3"
            }
    config = BenchmarkConfig.model_validate(data)
    events = []
    original = responder([])

    def respond(context, invocation):
        events.append(("run", context.arm.id))
        return original(context, invocation)

    def close(context):
        events.append(("close", context.arm.id))

    records = run_benchmark(
        config, output=tmp_path / "sparse", adapter=ScriptedAdapter(respond=respond, cleanup=close)
    )
    assert len(records) == 12
    assert events.index(("close", "A")) < events.index(("run", "B"))
    assert events[-1] == ("close", "B")
    for record in records:
        if record.kind == "session":
            assert record.between_questions_tokens == (600 if record.session_index == 3 else None)
    row = next(r for r in summarize(records) if r["arm"] == "A" and r["session_index"] == 3)
    assert row["upkeep_tokens_between_questions"] == 600
    assert row["upkeep_compute_between_questions_seconds"] == pytest.approx(0.6)
    assert row["amortized_tokens_per_question"] == 665 / 2


def test_copy_in_metrics_require_consistent_assessment_counts():
    with pytest.raises(ValidationError, match="both assessed and failed"):
        MemoryReport(claims=2, copy_in_failed_claims=1)
    with pytest.raises(ValidationError, match="failed <= assessed"):
        MemoryReport(claims=2, copy_in_assessed_claims=1, copy_in_failed_claims=2)


def test_source_changing_scripts_are_rejected_when_arms_diverge(tmp_path):
    import sys

    config = fixture_config(tmp_path)
    data = config.model_dump(mode="json")
    data["repetitions"] = 1
    data["arms"] = data["arms"][:2]
    data["sessions"][0]["changes"] = [
        {
            "kind": "command",
            "description": "Deliberately divergent fixture",
            "argv": [
                sys.executable,
                "-c",
                "import os; from pathlib import Path; "
                "Path('requirements.txt').write_text(os.environ['CRUXIBLE_STATE_ROOT'])",
            ],
        }
    ]
    with pytest.raises(ValueError, match="source snapshots differ"):
        run_benchmark(
            BenchmarkConfig.model_validate(data),
            output=tmp_path / "divergent",
            adapter=ScriptedAdapter(),
        )
    assert json.loads((tmp_path / "divergent/manifest.json").read_text())["status"] == "failed"


def test_audit_and_questions_share_one_session_budget(tmp_path):
    config = fixture_config(tmp_path)
    data = config.model_dump(mode="json")
    data["arms"] = data["arms"][:2]
    data["repetitions"] = 1
    data["sessions"] = data["sessions"][:1]
    data["questions"] = data["questions"][:1]
    data["questions"][0]["after_sessions"] = [1]
    data["questions"][0]["criteria"][0]["expected_by_session"] = {1: "live"}
    data["budget"]["max_tokens"] = 105
    calls = []
    with pytest.raises(ValueError, match="exceeded budget"):
        run_benchmark(
            BenchmarkConfig.model_validate(data),
            output=tmp_path / "shared-budget",
            adapter=ScriptedAdapter(respond=responder(calls)),
        )
    context, question = calls[-1]
    assert question.phase == "question" and context.budget.max_tokens == 5
    records = load_results(tmp_path / "shared-budget/results.jsonl")
    assert records[-1].kind == "question" and records[-1].budget_exceeded
