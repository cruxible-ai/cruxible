"""No-network fixture: setup, scheduling, persistent memory, review sheets and usage end to end."""

from __future__ import annotations

import csv
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from benchmarks.agent_memory_series.__main__ import main
from benchmarks.agent_memory_series.adapters import (
    CLIAdapterStub,
    CLIInvocation,
    Context,
    Invocation,
    MemoryReport,
    Reply,
    ScriptedAdapter,
    SurfaceAction,
    Usage,
)
from benchmarks.agent_memory_series.config import BenchmarkConfig, load_config
from benchmarks.agent_memory_series.demo import FIX
from benchmarks.agent_memory_series.demo import create_fixture as fixture_config
from benchmarks.agent_memory_series.grading import apply_sheets
from benchmarks.agent_memory_series.results import Review, load_results
from benchmarks.agent_memory_series.runner import run_benchmark
from benchmarks.agent_memory_series.summary import (
    spread,
    summarize,
    summarize_setup,
    surfaces,
    trends,
)
from pydantic import ValidationError

EXAMPLES = Path(__file__).resolve().parents[2] / "benchmarks/agent_memory_series/examples"


def responder(calls):
    def respond(context: Context, invocation: Invocation) -> Reply:
        calls.append((context, invocation))
        assert "CRUXIBLE_SERVER_URL" not in context.environment
        assert "CRUXIBLE_INSTANCE_ID" not in context.environment
        assert Path(context.environment["CRUXIBLE_STATE_ROOT"]).is_relative_to(context.run_root)
        assert context.environment["AGENT_MEMORY_FEEDS_DIR"] == str(context.feeds_dir)
        assert "reviewer_context" not in json.dumps(invocation.model_dump())
        assert not (context.workspace / "CLAUDE.md").exists()
        env = context.environment
        if context.arm.id == "A":
            assert "CRUXIBLE_MCP_PROFILE" not in env
        else:
            assert env["CRUXIBLE_MCP_PROFILE"] == "full"
        if context.arm.id == "B":
            assert "CRUXIBLE_MCP_TOOLS" not in env  # the full catalog
            assert env["PATH"].split(os.pathsep)[0] == str(context.tooling_dir / "bin")
        if context.arm.id == "C":
            assert env["CRUXIBLE_MCP_TOOLS"] == "cruxible_orient,cruxible_query"
        if invocation.phase == "setup":
            if context.arm.id == "B":
                # Background helper is up and both approval keys are provisioned.
                assert (context.run_root / "helper.ready").exists()
                keys = Path(context.environment["CRUXIBLE_MCP_KEY_DIR"])
                assert keys == context.keys_dir / "mcp"
                assert sorted(p.name for p in keys.iterdir()) == [
                    "owner.ed25519",
                    "reviewer.ed25519",
                ]
            return Reply(
                text="Prepared",
                usage=Usage(input_tokens=3, output_tokens=2, turns=1, cost_usd=0.5),
            )
        if invocation.phase == "question":
            assert (context.workspace / context.arm.memory_paths[0]).exists()
            fixed = "5.4" in (context.workspace / "requirements.txt").read_text()
            return Reply(
                text=("fixed" if fixed else "live") + "; evidence requirements.txt:1",
                usage=Usage(
                    input_tokens=8 * invocation.session_index,
                    output_tokens=2 * invocation.session_index,
                    cache_read_tokens=100,
                    turns=2,
                    cost_usd=0.001 * invocation.session_index,
                    model_calls=1,
                    subagent_calls=0,
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
                cost_usd=0.01 * invocation.session_index,
                model_calls=4,
                subagent_calls=1,
                compute_seconds=0.1 * invocation.session_index,
            ),
            memory=report,
            upkeep_compute_seconds=0.1 * invocation.session_index,
            first_useful_state=invocation.session_index == 1,
            model_id="scripted-model",
            actions=()
            if context.arm.id == "A"
            else (
                SurfaceAction(surface="mcp", name="cruxible_query", calls=2),
                SurfaceAction(surface="cli", name="line run"),
            ),
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
        config, output=output, adapter=ScriptedAdapter(respond=responder(calls))
    )
    return config, output, records, calls


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A terminated child may linger as a zombie until reaped; Popen.wait reaped it.
    return True


def test_dry_run_loop_setup_scheduling_isolation_and_metrics(completed):
    config, output, records, calls = completed
    # Per repetition: A 6 audits + 7 answers; B and C add one setup agent step each.
    assert len(records) == len(calls) == 82
    assert records == load_results(output / "results.jsonl")
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["status"] == "completed" and manifest["dry_run"] is True
    assert manifest["runaway_hits"] == 0 and manifest["cost_complete"] is True
    s = manifest["source_snapshots"]
    assert s["0"] == s["1"] != s["2"] != s["3"]
    assert s["4"] == s["2"]  # the revert restores the post-dependency manifest
    assert s["5"] != s["4"] and s["6"] == s["5"]  # a feed update changes no source
    f = manifest["feed_snapshots"]
    assert f["1"] == f["5"] != f["6"]
    contexts = {(ctx.arm.id, ctx.repetition): ctx for ctx, _ in calls}
    assert len({ctx.workspace for ctx in contexts.values()}) == 6
    assert len({ctx.state_root for ctx in contexts.values() if ctx.state_root}) == 4
    for (arm, _rep), ctx in contexts.items():
        setup = json.loads((ctx.run_root / "setup.json").read_text())
        assert len(setup) == {"A": 1, "B": 3, "C": 1}[arm]
        if arm == "A":
            assert (ctx.tooling_dir / "skills/fixture-audit/SKILL.md").exists()
        if arm == "B":
            assert not _alive(setup[0]["pid"]), "background setup helper must be stopped"
            assert (ctx.feeds_dir / "kev.json").exists()
    setups = [r for r in records if r.kind == "setup"]
    assert {(r.arm, r.setup_step) for r in setups} == {("B", 2), ("C", 0)}
    for record in records:
        assert record.source.files == 2 and record.runaway is None
        if record.kind == "question" and record.question.kind == "contradiction":
            assert record.session_index in (4, 5, 6)
        if record.kind == "session":
            assert record.size.state_bytes > 0
            if record.arm != "A":
                assert record.size.claims_per_source_file == record.session_index / 2
                assert record.size.copy_in_failure_share == 0
    rows = summarize(records)
    assert {row["session_index"] for row in rows} == set(range(1, 7))  # setup excluded
    first = next(row for row in rows if row["arm"] == "A" and row["session_index"] == 1)
    assert first["question_tokens"] == 10 and first["question_total_tokens"] == 110
    assert first["question_cost_usd"] == pytest.approx(0.001)
    assert first["session_model_calls"] == 4 and first["session_subagent_calls"] == 1
    assert first["amortized_tokens_per_question"] == 110
    assert first["first_useful_state_tokens"] == 100 and first["first_useful_state_turns"] == 3
    assert first["reviewed_answers"] == 0 and first["flagged_answers"] is None
    fourth = next(row for row in rows if row["arm"] == "B" and row["session_index"] == 4)
    assert fourth["upkeep_tokens_between_questions"] == 400
    assert fourth["amortized_tokens_per_question"] == pytest.approx(1120 / 4)
    third = next(row for row in rows if row["arm"] == "B" and row["session_index"] == 3)
    assert third["upkeep_tokens_between_questions"] == 500
    setup = {row["arm"]: row for row in summarize_setup(records)}
    assert set(setup) == {"B", "C"}
    assert setup["B"]["setup_tokens"] == 5 and setup["B"]["setup_cost_usd"] == 0.5
    trend = next(t for t in trends(rows) if t["arm"] == "A" and t["metric"] == "question_tokens")
    assert (trend["first"], trend["last"], trend["change_pct"]) == (10, 60, 500)
    for name in ("summary.csv", "setup.csv", "trend.csv", "summary.md", "plot-status.json"):
        assert (output / name).exists()
    assert "Synthetic dry run" in (output / "summary.md").read_text()
    assert set(manifest["runs"]) == {f"{a}/{r}" for a in "ABC" for r in (1, 2)}
    assert manifest["runs"]["B/1"]["integrations"]["kit"]["name"] == "security"
    assert manifest["runs"]["A/1"]["model"] == "scripted-fixture"
    sixth = next(row for row in rows if row["arm"] == "B" and row["session_index"] == 6)
    assert (sixth["mcp_actions"], sixth["cli_actions"], sixth["sdk_actions"]) == (2, 1, 0)
    used = [u for u in surfaces(records) if u["arm"] == "C" and u["session_index"] == 1]
    assert {(u["surface"], u["name"], u["calls"]) for u in used} == {
        ("mcp", "cruxible_query", 4),
        ("cli", "line run", 2),
    }
    spreads = [x for x in spread(records) if x["metric"] == "session_tokens"]
    assert all(x["n"] == 2 and x["stdev"] == 0 for x in spreads)
    for name in ("spread.csv", "surfaces.csv"):
        assert (output / name).exists()
    sheets = sorted(p.name for p in (output / "review").iterdir())
    assert sheets == [f"session-{i:02d}.csv" for i in range(1, 7)]


def _rewrite(path, rows, fields):
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_review_sheets_round_trip_partial_reviews_and_frozen_context(completed, tmp_path):
    _config, output, records, _calls = completed
    sheet = output / "review" / "session-04.csv"
    with sheet.open(newline="") as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames
        rows = list(reader)
    assert all(not row["arm"] for row in rows)
    assert "question_tokens" not in fields  # the eye test does not see cost
    contradiction = [row for row in rows if row["question_id"] == "contradiction"]
    assert contradiction[0]["reviewer_context"].startswith("The fix was reverted")
    flagged = contradiction[0]["response_id"]
    for row in rows:
        if row["kind"] != "question":
            continue  # audit rows left blank stay unreviewed
        row.update(stale_as_current="false", wrong="false", missing="false")
        if row["response_id"] == flagged:
            row.update(stale_as_current="true", notes="Calls the reverted fix current")
        elif row["question_id"] == "contradiction":
            row.update(needs_validation_with_blocker="true", notes="Names the blocker")
    _rewrite(sheet, rows, fields)
    reviewed = apply_sheets(records, [output / "review"])
    assert [r.output for r in reviewed] == [r.output for r in records]
    assert all(r.review is None for r in reviewed if r.session_index != 4)
    assert all(r.review is None for r in reviewed if r.kind == "session")
    summary = [row for row in summarize(reviewed) if row["session_index"] == 4]
    assert sum(row["stale_as_current"] for row in summary) == 1
    assert sum(row["flagged_answers"] for row in summary) == 1
    assert sum(row["needs_validation_with_blocker"] for row in summary) == 5
    assert all(row["reviewed_answers"] == 4 for row in summary)  # 2 answers x 2 repetitions
    raw = (output / "results.jsonl").read_bytes()
    arguments = ["review", "--results", str(output / "results.jsonl"), "--sheets"]
    assert main([*arguments, str(sheet), "--output", str(tmp_path / "reviewed")]) == 0
    assert (output / "results.jsonl").read_bytes() == raw
    stored = load_results(tmp_path / "reviewed" / "results.jsonl")
    assert sum(r.review is not None for r in stored) == 12
    rows[0]["reviewer_context"] += " edited after the run"
    _rewrite(sheet, rows, fields)
    with pytest.raises(ValueError, match="frozen reviewer context"):
        apply_sheets(records, [sheet])
    rows[0]["reviewer_context"] = rows[0]["reviewer_context"].removesuffix(" edited after the run")
    _rewrite(sheet, [*rows, rows[-1]], fields)
    with pytest.raises(ValueError, match="duplicate review row"):
        apply_sheets(records, [sheet])
    rows[-1]["wrong"] = "maybe"
    _rewrite(sheet, rows, fields)
    with pytest.raises(ValueError, match="invalid review flag"):
        apply_sheets(records, [sheet])
    unblind = tmp_path / "unblind"
    assert main(["export-review", "--results", str(output / "results.jsonl"),
                 "--output", str(unblind), "--unblind"]) == 0  # fmt: skip
    with (unblind / "session-01.csv").open(newline="") as handle:
        assert {row["arm"] for row in csv.DictReader(handle)} == {"A", "B", "C"}


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
    assert all(r.dry_run and r.usage.tokens == 0 and r.usage.cost_usd == 0 for r in records)
    assert all(row["reviewed_answers"] == 0 for row in summarize(records))
    with pytest.raises(FileExistsError):
        run_benchmark(config, output=tmp_path / "noop", adapter=ScriptedAdapter())


def test_example_series_is_a_valid_seven_session_plan():
    template = load_config(EXAMPLES / "series.yaml")
    assert template.target is None and 6 <= len(template.sessions) <= 8
    kinds = {change.kind for session in template.sessions for change in session.changes}
    assert kinds == {"patch", "feed"}
    assert {q.kind for q in template.questions} == {
        "recall",
        "change",
        "contradiction",
        "coverage",
        "control",
    }
    arm_b = next(arm for arm in template.arms if arm.id == "B")
    assert arm_b.environment["CRUXIBLE_MCP_KEY_DIR"] == "{keys}/mcp"
    assert any("--reviewer-key-dir" in step.argv for step in arm_b.setup if step.kind == "command")


@pytest.mark.parametrize(
    "mutation",
    [
        {"target": {"git_url": "https://example.invalid/repo", "base_commit": "HEAD"}},
        {"repetitions": 0},
        {"budget": {"max_tokens": 1, "max_turns": 1}},  # budgets are gone, not ignored
        {"guard": {"max_seconds_per_invocation": 0}},
        {"questions": [{"id": "bad", "kind": "control", "prompt": "x", "after_sessions": [99]}]},
        {
            "questions": [
                {
                    "id": "bad",
                    "kind": "control",
                    "prompt": "x",
                    "after_sessions": [1],
                    "reviewer_context_by_session": {2: "unscheduled"},
                }
            ]
        },
    ],
)
def test_configuration_rejects_unpinned_or_invalid_experiments(tmp_path, mutation):
    data = fixture_config(tmp_path).model_dump(mode="json")
    with pytest.raises(ValidationError):
        BenchmarkConfig.model_validate({**data, **mutation})


@pytest.mark.parametrize(
    "arm_update",
    [
        {"environment": {"CRUXIBLE_STATE_ROOT": "/elsewhere"}},
        {"environment": {"lower": "x"}},
        {"tooling_paths": ["../outside"]},
        {"environment": {"CLAUDE_CONFIG_DIR": "/shared"}},
        {"cruxible": {"mcp_tools": []}},
        {"cruxible": {"mcp_tools": ["not_a_cruxible_tool"]}},
        {"setup": [{"kind": "command", "description": "x", "argv": ["x"], "wait_for": "f"}]},
    ],
)
def test_arm_setup_and_environment_validation(tmp_path, arm_update):
    data = fixture_config(tmp_path).model_dump(mode="json")
    data["arms"][1].update(arm_update)
    with pytest.raises(ValidationError):
        BenchmarkConfig.model_validate(data)


def test_stubs_and_unfilled_slots_never_launch_real_clients(tmp_path):
    template = load_config(EXAMPLES / "series.yaml")
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
        guard=config.guard,
        workspace=tmp_path / "workspace",
        state_root=tmp_path / "isolated-state",
        run_root=tmp_path,
        keys_dir=tmp_path / "keys",
        feeds_dir=tmp_path / "feeds",
        tooling_dir=tmp_path / "tooling",
        environment={"CRUXIBLE_STATE_ROOT": str(tmp_path / "isolated-state")},
    )
    invocation = Invocation(phase="session", session_index=1, prompt=config.session_prompt)
    question = Invocation(phase="question", session_index=1, prompt="q", question_id="control")
    for client in ("claude-code", "codex"):
        plan = CLIAdapterStub(client).plan(context, invocation)
        assert plan.arm.plugin.name == "shipped-cruxible-plugin"
        assert plan.arm.kit.name == "security"
        assert plan.guard == config.guard and plan.subagents_allowed
        assert plan.environment["CRUXIBLE_STATE_ROOT"] == str(tmp_path / "isolated-state")
        # A fresh client config directory per invocation: no auto-memory carryover.
        assert plan.environment["CLAUDE_CONFIG_DIR"] == str(plan.config_dir)
        assert plan.config_dir != CLIAdapterStub(client).plan(context, question).config_dir
    plan = CLIAdapterStub("claude-code").plan(context, invocation)
    assert plan.argv[:2] == ("claude", "-p") and "--bare" not in plan.argv
    with pytest.raises(ValidationError, match="fresh session"):
        CLIInvocation.model_validate({**plan.model_dump(), "argv": (*plan.argv, "--resume")})


def test_runaway_guard_is_recorded_as_an_outcome_and_the_series_continues(tmp_path):
    data = fixture_config(tmp_path).model_dump(mode="json")
    data["arms"] = data["arms"][:2]
    data["repetitions"] = 1
    config = BenchmarkConfig.model_validate(data)
    original = responder([])

    def respond(context, invocation):
        reply = original(context, invocation)
        if invocation.phase == "session" and invocation.session_index == 2:
            return reply.model_copy(update={"stopped_by_guard": "wall_clock"})
        if invocation.phase == "session" and invocation.session_index == 3:
            return reply.model_copy(
                update={"usage": reply.usage.model_copy(update={"cost_usd": 5.0})}
            )
        return reply

    records = run_benchmark(
        config, output=tmp_path / "guard", adapter=ScriptedAdapter(respond=respond)
    )
    hits = {(r.arm, r.session_index): r.runaway for r in records if r.runaway}
    assert hits == {
        ("A", 2): "wall_clock",
        ("A", 3): "cost",
        ("B", 2): "wall_clock",
        ("B", 3): "cost",
    }
    manifest = json.loads((tmp_path / "guard/manifest.json").read_text())
    assert manifest["status"] == "completed" and manifest["runaway_hits"] == 4
    assert max(r.session_index for r in records) == 6
    row = next(r for r in summarize(records) if r["arm"] == "A" and r["session_index"] == 2)
    assert row["runaway_hits"] == 1


def test_source_mutation_by_agent_or_setup_fails_the_run(tmp_path):
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
    data = config.model_dump(mode="json")
    data["arms"][0]["setup"][0]["argv"][3] = "stray.txt"  # the install now lands in source
    with pytest.raises(ValueError, match="setup modified target source"):
        run_benchmark(
            BenchmarkConfig.model_validate(data),
            output=tmp_path / "setup-mutates",
            adapter=ScriptedAdapter(),
        )


def test_patch_and_feed_inputs_are_frozen_before_any_adapter_call(tmp_path):
    config = fixture_config(tmp_path)
    patch = tmp_path / "fix.patch"
    patch.write_text(FIX)
    feed = tmp_path / "kev.json"
    feed.write_text('{"frozen": true}')
    data = config.model_dump(mode="json")
    data["sessions"][2]["changes"] = [
        {"kind": "patch", "description": "Apply the fixture fix", "path": "fix.patch"}
    ]
    data["sessions"][5]["changes"] = [
        {"kind": "feed", "description": "KEV", "name": "kev.json", "path": "kev.json"}
    ]
    config = BenchmarkConfig.model_validate(data)
    original = responder([])

    def mutate_input(context, invocation):
        patch.write_text("invalid new patch; the frozen original must still be used")
        feed.write_text("changed")
        return original(context, invocation)

    records = run_benchmark(
        config,
        output=tmp_path / "frozen",
        config_directory=tmp_path,
        adapter=ScriptedAdapter(respond=mutate_input),
    )
    assert len(records) == 82
    frozen = json.loads((tmp_path / "frozen/frozen-config.json").read_text())
    assert frozen["sessions"][2]["changes"][0]["text"] == FIX
    assert frozen["sessions"][5]["changes"][0]["text"] == '{"frozen": true}'
    written = tmp_path / "frozen/runs/C/rep-002/feeds/kev.json"
    assert written.read_text() == '{"frozen": true}'


def test_unknown_usage_and_partial_reviews_remain_missing(completed):
    _config, _output, records, _calls = completed
    changed = []
    for record in records:
        if record.arm == "A" and record.kind == "question" and record.session_index == 1:
            update: dict[str, object] = {"usage": Usage()}
            if record.repetition == 1:
                update["review"] = Review(notes="partial", wrong=False)
            record = record.model_copy(update=update)
        changed.append(record)
    row = next(r for r in summarize(changed) if r["arm"] == "A" and r["session_index"] == 1)
    assert row["question_tokens"] is None and row["questions_with_token_usage"] == 0
    assert row["question_cost_usd"] is None
    assert row["repetitions_with_complete_cost"] == 0
    assert row["reviewed_answers"] == 0 and row["flagged_answers"] is None
    assert row["wrong"] == 0 and row["stale_as_current"] is None


def test_sparse_question_batches_count_upkeep_once_and_close_each_arm(tmp_path):
    config = fixture_config(tmp_path)
    data = config.model_dump(mode="json")
    data["arms"] = data["arms"][:2]
    data["repetitions"] = 1
    for question in data["questions"]:
        question["after_sessions"] = [3]
        question["reviewer_context_by_session"] = {}
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
    assert len(records) == 8 + 9  # A; then B, which adds one setup agent step
    assert events.index(("close", "A")) < events.index(("run", "B"))
    assert events[-1] == ("close", "B")
    for record in records:
        if record.kind == "session":
            assert record.between_questions_tokens == (600 if record.session_index == 3 else None)
    row = next(r for r in summarize(records) if r["arm"] == "A" and r["session_index"] == 3)
    assert row["upkeep_tokens_between_questions"] == 600
    assert row["upkeep_compute_between_questions_seconds"] == pytest.approx(0.6)
    assert row["amortized_tokens_per_question"] == (600 + 60) / 2


def test_copy_in_metrics_require_consistent_assessment_counts():
    with pytest.raises(ValidationError, match="both assessed and failed"):
        MemoryReport(claims=2, copy_in_failed_claims=1)
    with pytest.raises(ValidationError, match="failed <= assessed"):
        MemoryReport(claims=2, copy_in_assessed_claims=1, copy_in_failed_claims=2)


def test_source_changing_scripts_are_rejected_when_arms_diverge(tmp_path):
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
                "Path('divergent.txt').write_text(os.environ['CRUXIBLE_STATE_ROOT'])",
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


def test_fixture_base_commit_is_pinned(tmp_path):
    config = fixture_config(tmp_path)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=config.target.git_url)
    assert config.target.base_commit == head.decode().strip()
