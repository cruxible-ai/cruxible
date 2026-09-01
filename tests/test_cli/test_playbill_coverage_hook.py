"""Coverage delivered into a harness's tool results, against a served instance.

Two surfaces, one operation. The §11.7 owned-harness middleware is the primary
one and carries the §11.8 flagship scenario; the Claude Code plugin is the
secondary one and carries what that vendor's tool-result shapes can actually
hold.

The flagship scenario, verbatim from §11.8
-------------------------------------------
"Include a test where an agent edits a governed span after reading it and
another where it edits without first reading it: the hooked arm must expose the
affected Claim within the same agent turn in both cases." Both are below, driven
through the middleware against a real daemon holding a real accepted Claim over
a real foreign source, with no compile, no proposal, and no acceptance anywhere
in the transcript.

How the resolver is reached
---------------------------
The middleware takes its resolve callable by injection, and :func:`_resolver`
below is exactly the embedding a harness writes: a closure over a Playbill
client and an instance ID that carries observations to the served coverage
operation and validates the frozen result back. That is the whole TauBench seam
-- arms 3 and 4 differ by whether this middleware is wired in, and by nothing
else.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_core.cli.commands import _common
from cruxible_core.cli.context import load_cli_context
from cruxible_core.cli.main import cli
from cruxible_core.playbill.coverage.adapter import WorkingSourceObservationV1
from cruxible_core.playbill.coverage.claude_code import (
    ANNOTATABLE_TOOLS,
    ENVELOPE_VERSION,
    HOOK_EVENT_NAME,
    TOOL_KINDS,
    read_post_tool_use_event,
)
from cruxible_core.playbill.coverage.contracts import CoverageResultV3
from cruxible_core.playbill.coverage.middleware import (
    CONFIG_RELATIVE_PATH,
    CoverageMiddlewareV1,
    HarnessLineRangeV1,
    HarnessToolEventV1,
    ResolveCoverage,
    coverage_middleware,
)
from cruxible_core.playbill.coverage.render import (
    BATCH_SUMMARY_PREFIX,
    UNAVAILABLE_NOTE_PREFIX,
    render_coverage_result,
)
from tests.test_cli.test_playbill_coverage_surface import (
    FOREIGN_BYTES,
    FOREIGN_IDENTITY,
    FOREIGN_PATH,
    FOREIGN_SOURCE,
    GOVERNED_LINE,
    _bootstrap,
    _govern_a_foreign_span,
)
from tests.test_cli.test_playbill_knowledge_loop_smoke import (  # noqa: F401
    _Cli,
    served_cli,
)

# `corpus/handbook.md` under prefix `corpus/` and identity prefix `corpus.` is
# `corpus.handbook.md` -- extension and all, because the normalizer is
# non-lossy. The accepted Claim is authored against that exact string.
PREFIX_IDENTITY = "corpus.handbook.md"


def _resolver(client: Any, instance_id: str) -> ResolveCoverage:
    """The embedding recipe: observations in, one frozen coverage result out."""

    def resolve(observations: Sequence[WorkingSourceObservationV1]) -> CoverageResultV3:
        answered = client.resolve_playbill_coverage(
            instance_id,
            observations=[item.model_dump(mode="json") for item in observations],
        )
        return CoverageResultV3.model_validate(answered.result)

    return resolve


def _workspace(tmp_path: Path, *, identity: str = FOREIGN_IDENTITY) -> Path:
    """A working tree holding the governed foreign file, plus its binding config."""

    workspace = tmp_path / "workspace"
    (workspace / "corpus").mkdir(parents=True)
    (workspace / FOREIGN_PATH).write_bytes(FOREIGN_BYTES)
    (workspace / "notes.txt").write_bytes(b"ordinary working notes\n")
    (workspace / ".playbill").mkdir()
    (workspace / CONFIG_RELATIVE_PATH).write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "tag": "playbill-coverage-exact-path-rule-v1",
                        "path": FOREIGN_PATH,
                        "plane": "external",
                        "identity": identity,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return workspace


def _middleware(workspace: Path) -> CoverageMiddlewareV1:
    client = _common._get_client()
    instance_id = load_cli_context().instance_id
    assert client is not None and instance_id
    return coverage_middleware(root=workspace, resolve=_resolver(client, instance_id))


def _first_card_line(text: str) -> str:
    """The leading card line, dropping the summary lines around it.

    The summary names a generation and an epoch that legitimately advance
    between two calls, so the card line is the part two surfaces must agree on
    byte for byte.
    """

    return next(line for line in text.splitlines() if not line.startswith(BATCH_SUMMARY_PREFIX))


# -- the §11.8 flagship scenario -------------------------------------------


def test_editing_a_governed_span_after_reading_it_exposes_the_claim_in_the_same_turn(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    """Read, edit, and the drifted card rides the edit's own tool result.

    No compile, no proposal, no acceptance: the accepted coordinate is identical
    before and after, and the card points at accepted history while granting the
    changed bytes nothing.
    """

    cruxible = served_cli
    _bootstrap(cruxible, tmp_path)
    claim_identity = _govern_a_foreign_span(cruxible, tmp_path)
    workspace = _workspace(tmp_path)
    middleware = _middleware(workspace)

    # 1. The agent reads the governed span. It is exact, and it says so.
    read = middleware.after_tool(
        HarnessToolEventV1(
            kind="read",
            ranges=(HarnessLineRangeV1(path=FOREIGN_PATH, start_line=3, end_line=3),),
            original_output="3\tThe reviewer accepted the migration plan on the second reading.",
        )
    )

    assert read.result is not None
    assert read.result.summary.exact == 1
    assert read.appended_coverage_text.splitlines()[0].startswith(f"exact  {FOREIGN_SOURCE}  ")

    # 2. The agent edits that span. Same turn, no separate call, no compile.
    (workspace / FOREIGN_PATH).write_bytes(FOREIGN_BYTES.replace(b"accepted", b"rejected"))

    edit = middleware.after_tool(
        HarnessToolEventV1(
            kind="edit",
            paths=(FOREIGN_PATH,),
            original_output=f"The file {FOREIGN_PATH} has been updated successfully.",
        )
    )

    assert edit.result is not None
    assert edit.result.summary.drifted == 1
    card = edit.result.spans[0].cards[0]
    assert card.match_state == "drifted"
    assert claim_identity.endswith(Path(card.claim_addresses[0].artifact_path).stem)
    assert card.observed_commitment_digest != card.expected_commitment_digest
    assert card.grants_mutation_authority is False

    # The affected Claim is named in the text the agent is already reading.
    drift_line = edit.appended_coverage_text.splitlines()[0]
    assert drift_line.startswith(f"drifted  {FOREIGN_SOURCE}  ")
    assert card.claim_addresses[0].artifact_path in drift_line

    # 3. Nothing accepted moved across the whole turn.
    assert edit.result.at == read.result.at


def test_editing_a_governed_span_without_reading_it_first_exposes_it_just_the_same(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The second half of the §11.8 scenario: no prior read, same exposure.

    This is the case the unhooked arm cannot reach at all -- there was no read to
    have carried the card, and no compile has run.
    """

    cruxible = served_cli
    _bootstrap(cruxible, tmp_path)
    claim_identity = _govern_a_foreign_span(cruxible, tmp_path)
    workspace = _workspace(tmp_path)
    middleware = _middleware(workspace)

    (workspace / FOREIGN_PATH).write_bytes(FOREIGN_BYTES.replace(b"accepted", b"rejected"))
    delivery = middleware.after_tool(
        HarnessToolEventV1(kind="write", paths=(FOREIGN_PATH,), original_output="written")
    )

    assert delivery.result is not None
    assert delivery.result.summary.drifted == 1
    card = delivery.result.spans[0].cards[0]
    assert claim_identity.endswith(Path(card.claim_addresses[0].artifact_path).stem)
    assert card.match_state == "drifted"
    # The original output is preserved intact and the cards are pure addendum.
    assert delivery.original_output == "written"
    assert delivery.spliced() == "written\n" + delivery.appended_coverage_text


def test_a_write_of_an_ungoverned_file_says_nothing_at_all(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    """An unbound path is not covered, and not covered is silent."""

    cruxible = served_cli
    _bootstrap(cruxible, tmp_path)
    _govern_a_foreign_span(cruxible, tmp_path)
    workspace = _workspace(tmp_path)

    delivery = _middleware(workspace).after_tool(
        HarnessToolEventV1(kind="write", paths=("notes.txt",), original_output="written")
    )

    assert delivery.spliced() == "written"
    assert delivery.unbound_paths == ("notes.txt",)
    assert "notes.txt" not in delivery.appended_coverage_text


def test_the_prefix_rule_normalizer_binds_the_identity_the_claim_was_authored_against(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    """The H1 coupling, end to end: one string, declared on both sides.

    The accepted `logical_source_identity` and the working binding are the same
    literal `corpus.handbook.md`, and the prefix rule's declared normalizer is
    what produces it from the working path. Nothing infers it.
    """

    cruxible = served_cli
    _bootstrap(cruxible, tmp_path)
    _govern_a_foreign_span(cruxible, tmp_path, identity=PREFIX_IDENTITY)

    workspace = tmp_path / "workspace"
    (workspace / "corpus").mkdir(parents=True)
    (workspace / FOREIGN_PATH).write_bytes(FOREIGN_BYTES)
    (workspace / ".playbill").mkdir()
    (workspace / CONFIG_RELATIVE_PATH).write_text(
        json.dumps(
            {
                "rules": [
                    {
                        "tag": "playbill-coverage-path-prefix-rule-v1",
                        "path_prefix": "corpus/",
                        "plane": "external",
                        "identity_prefix": "corpus.",
                        "normalizer": "playbill-coverage-path-identity-v1",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    delivery = _middleware(workspace).after_tool(
        HarnessToolEventV1(kind="read", paths=(FOREIGN_PATH,))
    )

    assert delivery.result is not None
    assert delivery.result.summary.exact == 1
    assert delivery.result.spans[0].cards[0].accepted_source.identity == PREFIX_IDENTITY


def test_the_middleware_renders_exactly_what_the_reference_surface_renders(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    """§11.7 parity: the hooked path and the CLI print the same bytes.

    Asserted twice over, against the renderer directly and against the actual
    `coverage resolve` stdout, so a divergence in either direction fails.
    """

    cruxible = served_cli
    _bootstrap(cruxible, tmp_path)
    _govern_a_foreign_span(cruxible, tmp_path)
    workspace = _workspace(tmp_path)

    delivery = _middleware(workspace).after_tool(
        HarnessToolEventV1(kind="read", paths=(FOREIGN_PATH,))
    )

    assert delivery.result is not None
    assert delivery.lines == render_coverage_result(delivery.result)

    reference = cruxible.run(
        "playbill",
        "coverage",
        "resolve",
        "--root",
        str(workspace),
        "--bind",
        f"{FOREIGN_PATH}={FOREIGN_SOURCE}",
        "--file",
        FOREIGN_PATH,
    ).stdout
    assert _first_card_line(reference.strip()) == _first_card_line(delivery.appended_coverage_text)


def test_a_coverage_card_never_carries_the_governed_bytes(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    """§11.8: deterministic metadata only -- never a source body."""

    cruxible = served_cli
    _bootstrap(cruxible, tmp_path)
    _govern_a_foreign_span(cruxible, tmp_path)
    workspace = _workspace(tmp_path)

    delivery = _middleware(workspace).after_tool(
        HarnessToolEventV1(kind="read", paths=(FOREIGN_PATH,))
    )

    assert GOVERNED_LINE.decode("utf-8").strip() not in delivery.appended_coverage_text
    assert "reviewer" not in delivery.appended_coverage_text


# -- the Claude Code plugin -------------------------------------------------


def _post_tool_use(tool_name: str, tool_input: Any, tool_response: Any) -> dict[str, Any]:
    """One recorded PostToolUse payload, in the real 2.1.234 envelope.

    Field for field what Claude Code writes: `tool_response` rather than
    `tool_result`, alongside `tool_use_id` and `duration_ms`.
    """

    return {
        "session_id": "11111111-2222-3333-4444-555555555555",
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/workspace",
        "permission_mode": "default",
        "hook_event_name": HOOK_EVENT_NAME,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_response": tool_response,
        "tool_use_id": "toolu_01ABCDEF",
        "duration_ms": 12,
    }


def _run_hook(workspace: Path, payload: dict[str, Any]) -> dict[str, Any]:
    result = CliRunner().invoke(
        cli,
        ["playbill", "hook", "post-tool-use", "--root", str(workspace)],
        input=json.dumps(payload),
    )
    assert result.exit_code == 0, result.output
    return dict(json.loads(result.stdout))


def _run_hook_with_diagnostic(
    workspace: Path,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    result = CliRunner().invoke(
        cli,
        ["playbill", "hook", "post-tool-use", "--root", str(workspace)],
        input=json.dumps(payload),
    )
    assert result.exit_code == 0, result.output
    return dict(json.loads(result.stdout)), result.stderr


def test_the_translation_table_covers_the_four_tools_and_annotates_only_grep() -> None:
    """The vendor limitation, pinned so it cannot drift silently."""

    assert set(TOOL_KINDS) == {"Read", "Grep", "Edit", "Write"}
    assert ANNOTATABLE_TOOLS == frozenset({"Grep"})
    assert ENVELOPE_VERSION == "2.1.234"


def test_a_grep_content_result_is_annotated_in_place_with_every_other_field_intact(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    cruxible = served_cli
    _bootstrap(cruxible, tmp_path)
    _govern_a_foreign_span(cruxible, tmp_path)
    workspace = _workspace(tmp_path)

    content = f"{workspace / FOREIGN_PATH}:3:The reviewer accepted the migration plan"
    response = {
        "mode": "content",
        "numFiles": 0,
        "filenames": [],
        "content": content,
        "numLines": 1,
        "totalLines": 1,
    }
    emitted = _run_hook(
        workspace,
        _post_tool_use("Grep", {"pattern": "reviewer", "output_mode": "content"}, response),
    )

    updated = emitted["hookSpecificOutput"]["updatedToolOutput"]
    assert emitted["hookSpecificOutput"]["hookEventName"] == HOOK_EVENT_NAME
    # Original content preserved, cards appended, nothing replaced.
    assert updated["content"].startswith(content + "\n")
    assert f"exact  {FOREIGN_SOURCE}" in updated["content"]
    assert BATCH_SUMMARY_PREFIX in updated["content"]
    # Every other field byte-identical -- including the counts, which describe
    # the search and would be misreported if the annotation were counted in.
    assert {key: value for key, value in updated.items() if key != "content"} == {
        key: value for key, value in response.items() if key != "content"
    }
    # The instruction channel is never touched.
    assert "additionalContext" not in emitted["hookSpecificOutput"]


def test_the_hook_marks_a_floor_from_an_older_generation_without_a_coverage_binding(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    cruxible = served_cli
    _bootstrap(cruxible, tmp_path)
    old = cruxible.json("playbill", "orient")["orientation"]
    _govern_a_foreign_span(cruxible, tmp_path)
    current = cruxible.json("playbill", "orient")["orientation"]
    workspace = tmp_path / "workspace"
    floor = workspace / "playbill-floor"
    floor.mkdir(parents=True)
    (workspace / ".playbill").mkdir()
    (workspace / CONFIG_RELATIVE_PATH).write_text(
        json.dumps(
            {
                "tag": "playbill-coverage-workspace-config-v2",
                "floor_output": {
                    "tag": "playbill-floor-output-v1",
                    "path": "playbill-floor",
                    "format": "playbill-floor-export-v2",
                },
            }
        ),
        encoding="utf-8",
    )
    manifest = {
        "tag": "playbill-floor-manifest-v2",
        "format": "playbill-floor-export-v2",
        "coordinate": old["coordinate"],
        "files": [],
        "floor_digest": typed_digest(
            Sha256Value,
            "playbill-floor-export-v2",
            {"files": []},
        ).tagged,
    }
    (floor / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    content = f"{floor / 'manifest.json'}:1:{{"

    emitted = _run_hook(
        workspace,
        _post_tool_use(
            "Grep",
            {"pattern": "tag", "output_mode": "content"},
            {"mode": "content", "content": content, "numLines": 1},
        ),
    )

    updated = emitted["hookSpecificOutput"]["updatedToolOutput"]["content"]
    assert updated == (
        content
        + f"\nfloor at generation {old['generation']}, current "
        + f"{current['generation']}; re-export required"
    )


def _file_tool_payloads(workspace: Path) -> dict[str, dict[str, Any]]:
    """Recorded Read, Edit, and Write payloads, in their real response shapes."""

    absolute = str(workspace / FOREIGN_PATH)
    return {
        "Read": _post_tool_use(
            "Read",
            {"file_path": absolute, "offset": 3, "limit": 1},
            {
                "type": "text",
                "file": {
                    "filePath": absolute,
                    "content": "The reviewer accepted the migration plan on the second reading.",
                    "numLines": 1,
                    "startLine": 3,
                    "totalLines": 4,
                },
            },
        ),
        "Edit": _post_tool_use(
            "Edit",
            {"file_path": absolute, "old_string": "accepted", "new_string": "rejected"},
            {
                "filePath": absolute,
                "oldString": "accepted",
                "newString": "rejected",
                "originalFile": FOREIGN_BYTES.decode("utf-8"),
                "structuredPatch": [],
                "userModified": False,
                "replaceAll": False,
            },
        ),
        "Write": _post_tool_use(
            "Write",
            {"file_path": absolute, "content": "x"},
            {"type": "update", "filePath": absolute, "content": "x", "structuredPatch": []},
        ),
    }


def test_read_edit_and_write_translate_to_real_observations(tmp_path: Path) -> None:
    """They are consumed, not dropped -- which is what keeps the next Grep fresh.

    Asserted at the translation table rather than through the manifest epoch,
    because the epoch deliberately counts *snapshot movement* rather than calls:
    a probe that resolves in order to read it would absorb the very movement it
    was trying to observe. What is checkable here is that each payload produces
    a well-formed event naming the right source and window, and the middleware
    suite already proves such an event reaches the resolver.
    """

    workspace = _workspace(tmp_path)
    payloads = _file_tool_payloads(workspace)

    read = read_post_tool_use_event(payloads["Read"], workspace_root=workspace)
    assert read is not None and read.kind == "read"
    # A windowed read asks about its window, converted from the lines the
    # response says were actually returned.
    assert read.paths == ()
    assert read.ranges[0].path == FOREIGN_PATH
    assert (read.ranges[0].start_line, read.ranges[0].end_line) == (3, 3)

    for tool_name, kind in (("Edit", "edit"), ("Write", "write")):
        event = read_post_tool_use_event(payloads[tool_name], workspace_root=workspace)
        assert event is not None and event.kind == kind
        # A changed path is asked about whole, never through a guessed window.
        assert event.paths == (FOREIGN_PATH,)
        assert event.ranges == ()


def test_read_edit_and_write_never_modify_their_tool_output(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    """Their result shapes cannot carry an annotation, so nothing is attempted.

    An empty object is the whole response: no `updatedToolOutput`, and above all
    no `additionalContext`, which would arrive as a system reminder and turn
    data about a working file into an instruction.
    """

    cruxible = served_cli
    _bootstrap(cruxible, tmp_path)
    _govern_a_foreign_span(cruxible, tmp_path)
    workspace = _workspace(tmp_path)

    # Drift the governed span, so there is genuinely a card to be tempted by.
    (workspace / FOREIGN_PATH).write_bytes(FOREIGN_BYTES.replace(b"accepted", b"rejected"))

    for tool_name, payload in _file_tool_payloads(workspace).items():
        assert _run_hook(workspace, payload) == {}, tool_name


def test_an_unrecognized_payload_changes_nothing(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    assert _run_hook(workspace, {"hook_event_name": "PreToolUse", "tool_name": "Read"}) == {}
    assert _run_hook(workspace, _post_tool_use("Bash", {"command": "ls"}, {"stdout": ""})) == {}


def test_hook_reports_missing_instance_id_without_changing_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "absent-context.json"))
    monkeypatch.delenv("CRUXIBLE_INSTANCE_ID", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_URL", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_SOCKET", raising=False)
    workspace = _workspace(tmp_path)
    payload = _post_tool_use(
        "Read",
        {"file_path": str(workspace / FOREIGN_PATH)},
        {"type": "text", "file": {"content": "governed"}},
    )

    emitted, stderr = _run_hook_with_diagnostic(workspace, payload)

    assert emitted == {}
    assert stderr == "playbill.coverage_hook.instance_id_missing\n"


def test_hook_uses_the_workspace_config_instance_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "absent-context.json"))
    monkeypatch.delenv("CRUXIBLE_INSTANCE_ID", raising=False)
    workspace = _workspace(tmp_path)
    config_path = workspace / CONFIG_RELATIVE_PATH
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["instance_id"] = "inst_from_workspace"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    calls: list[str] = []

    class StubClient:
        def resolve_playbill_coverage(self, instance_id: str, **_: object) -> None:
            calls.append(instance_id)
            raise RuntimeError("stop after proving instance selection")

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    payload = _post_tool_use(
        "Read",
        {"file_path": str(workspace / FOREIGN_PATH)},
        {"type": "text", "file": {"content": "governed"}},
    )

    emitted, stderr = _run_hook_with_diagnostic(workspace, payload)

    assert calls == ["inst_from_workspace"]
    assert emitted == {}
    assert stderr == ""


def test_hook_reports_invalid_rule_tag_without_changing_stdout(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    config_path = workspace / CONFIG_RELATIVE_PATH
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "playbill-coverage-exact-path-rule-v1",
            "playbill-coverage-path-rule-v1",
        ),
        encoding="utf-8",
    )
    payload = _post_tool_use(
        "Read",
        {"file_path": str(workspace / FOREIGN_PATH)},
        {"type": "text", "file": {"content": "governed"}},
    )

    emitted, stderr = _run_hook_with_diagnostic(workspace, payload)

    assert emitted == {}
    assert stderr == "playbill.coverage_hook.rule_tag_invalid\n"


def test_hook_reports_non_object_tool_response_without_changing_stdout(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    emitted, stderr = _run_hook_with_diagnostic(
        workspace,
        _post_tool_use("Grep", {"pattern": "governed"}, "unstructured output"),
    )

    assert emitted == {}
    assert stderr == "playbill.coverage_hook.tool_response_invalid\n"


@pytest.mark.parametrize("tool_name", ["Read", "Edit", "Write"])
def test_unstructured_nonannotatable_tool_responses_stay_silent(
    tmp_path: Path,
    tool_name: str,
) -> None:
    workspace = _workspace(tmp_path)

    emitted, stderr = _run_hook_with_diagnostic(
        workspace,
        _post_tool_use(
            tool_name,
            {"file_path": str(workspace / "notes.txt")},
            "unstructured output",
        ),
    )

    assert emitted == {}
    assert stderr == ""


def test_unbound_event_remains_silent_without_an_instance_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "absent-context.json"))
    monkeypatch.delenv("CRUXIBLE_INSTANCE_ID", raising=False)
    workspace = _workspace(tmp_path)
    payload = _post_tool_use(
        "Read",
        {"file_path": str(workspace / "notes.txt")},
        {"type": "text", "file": {"content": "ordinary working notes"}},
    )

    emitted, stderr = _run_hook_with_diagnostic(workspace, payload)

    assert emitted == {}
    assert stderr == ""


def test_the_hook_fails_open_when_the_daemon_is_unreachable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No served instance at all: the tool result survives, plus one line.

    Nothing here bootstraps a daemon, so the resolve cannot succeed -- which is
    exactly the condition an agent must never notice as a broken tool call.
    """

    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    monkeypatch.delenv("CRUXIBLE_SERVER_URL", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_SOCKET", raising=False)
    workspace = _workspace(tmp_path)
    content = f"{workspace / FOREIGN_PATH}:3:The reviewer accepted the migration plan"
    emitted = _run_hook(
        workspace,
        _post_tool_use(
            "Grep",
            {"pattern": "reviewer"},
            {"mode": "content", "content": content, "numLines": 1},
        ),
    )

    updated = emitted["hookSpecificOutput"]["updatedToolOutput"]
    assert updated["content"].startswith(content + "\n")
    note = updated["content"][len(content) + 1 :]
    assert note.startswith(UNAVAILABLE_NOTE_PREFIX)
    assert note.splitlines() == [note]
    # Fail open on infrastructure never means guessing a match state.
    for state in ("exact", "drifted", "candidate"):
        assert state not in note


def test_the_same_payload_against_the_same_state_emits_identical_bytes(
    served_cli: _Cli,  # noqa: F811
    tmp_path: Path,
) -> None:
    cruxible = served_cli
    _bootstrap(cruxible, tmp_path)
    _govern_a_foreign_span(cruxible, tmp_path)
    workspace = _workspace(tmp_path)

    content = f"{workspace / FOREIGN_PATH}:3:The reviewer accepted"
    payload = _post_tool_use(
        "Grep",
        {"pattern": "reviewer"},
        {"mode": "content", "content": content, "numLines": 1},
    )

    assert _run_hook(workspace, payload) == _run_hook(workspace, payload)
