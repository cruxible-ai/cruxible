"""Tests for the gate verb group: list, check, source adapters, fail-closed paths."""

from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from cruxible_client.errors import AuthenticationError
from cruxible_core.cli.commands._gate_adapters import _ADAPTERS
from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.cli.main import cli
from cruxible_core.config.schema import GATE_KINDS
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance

GATED_CONFIG_YAML = """\
version: "1.0"
name: gate_cli_test
description: Gate verb test domain

entity_types:
  ReviewRequest:
    properties:
      review_request_id:
        type: string
        primary_key: true
      status:
        type: string
        enum: [requested, approved]
      change_head:
        type: string
        optional: true

gates:
  merge-review:
    description: Merges to main need an approved review pinning the merged tip.
    kind: git-pre-push
    entity_type: ReviewRequest
    match_property: change_head
    condition: {status: approved}
    adapter: {branch_pattern: refs/heads/main}
  action-review:
    description: Irreversible actions need an approved review pinning the action ID.
    kind: generic
    entity_type: ReviewRequest
    match_property: change_head
    condition: {status: approved}
"""

APPROVED_SHA = "a" * 40
REQUESTED_SHA = "b" * 40
UNKNOWN_SHA = "c" * 40


def test_every_declared_kind_has_an_adapter() -> None:
    # GATE_KINDS (config schema) and the adapter registry must stay in sync:
    # lint admits exactly the kinds the CLI can actually evaluate.
    assert set(_ADAPTERS) == set(GATE_KINDS)


@pytest.fixture(autouse=True)
def cli_context_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "cli-context.json"))


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _review(review_id: str, status: str, change_head: str) -> EntityInstance:
    return EntityInstance(
        entity_type="ReviewRequest",
        entity_id=review_id,
        properties={
            "review_request_id": review_id,
            "status": status,
            "change_head": change_head,
        },
    )


@pytest.fixture
def gated_instance(tmp_path: Path) -> CruxibleInstance:
    """Instance with a merge-review gate, one approved and one requested review."""
    (tmp_path / "config.yaml").write_text(GATED_CONFIG_YAML)
    instance = CruxibleInstance.init(tmp_path, "config.yaml")
    graph = EntityGraph()
    graph.add_entity(_review("RR-1", "approved", APPROVED_SHA))
    graph.add_entity(_review("RR-2", "requested", REQUESTED_SHA))
    instance.save_graph(graph)
    return instance


def _chdir_run(
    runner: CliRunner,
    directory: Path,
    args: list[str],
    stdin: str | bytes | io.BufferedIOBase | None = None,
) -> Result:
    original = os.getcwd()
    try:
        os.chdir(directory)
        return runner.invoke(cli, args, input=stdin)
    finally:
        os.chdir(original)


def _gate_receipts(instance: CruxibleInstance):
    store = instance.get_receipt_store()
    try:
        summaries = store.list_receipts(operation_type="gate_evaluation")
        return [store.get_receipt(item["receipt_id"]) for item in summaries]
    finally:
        store.close()


class TestGateList:
    def test_list_shows_declared_gate(
        self, runner: CliRunner, gated_instance: CruxibleInstance
    ) -> None:
        result = _chdir_run(runner, gated_instance.root, ["gate", "list"])
        assert result.exit_code == 0
        assert (
            "merge-review [git-pre-push]: ReviewRequest.change_head where "
            "status=approved (branch_pattern refs/heads/main)" in result.output
        )
        assert (
            "action-review [generic]: ReviewRequest.change_head where status=approved"
            in result.output
        )

    def test_list_json_shape(self, runner: CliRunner, gated_instance: CruxibleInstance) -> None:
        result = _chdir_run(runner, gated_instance.root, ["gate", "list", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["merge-review"]["kind"] == "git-pre-push"
        assert payload["merge-review"]["match_property"] == "change_head"
        assert payload["merge-review"]["condition"] == {"status": "approved"}
        assert payload["merge-review"]["adapter"] == {"branch_pattern": "refs/heads/main"}

    def test_list_without_gates_says_so(
        self, runner: CliRunner, initialized_project: CruxibleInstance
    ) -> None:
        result = _chdir_run(runner, initialized_project.root, ["gate", "list"])
        assert result.exit_code == 0
        assert "No gates declared" in result.output


class TestGateCheckValueOverride:
    """--value is a hidden diagnostic override, bypassing the source adapter."""

    def test_approved_value_satisfied(
        self, runner: CliRunner, gated_instance: CruxibleInstance
    ) -> None:
        result = _chdir_run(
            runner,
            gated_instance.root,
            ["gate", "check", "merge-review", "--value", APPROVED_SHA],
        )
        assert result.exit_code == 0
        assert f"merge-review {APPROVED_SHA} satisfied" in result.output

        receipts = _gate_receipts(gated_instance)
        assert len(receipts) == 1
        assert receipts[0] is not None
        assert receipts[0].parameters["instance_id"] == str(gated_instance.root.resolve())
        assert receipts[0].parameters["verdict"] == "satisfied"

    def test_pinned_but_unapproved_value_unsatisfied(
        self, runner: CliRunner, gated_instance: CruxibleInstance
    ) -> None:
        result = _chdir_run(
            runner,
            gated_instance.root,
            ["gate", "check", "merge-review", "--value", REQUESTED_SHA],
        )
        assert result.exit_code == 1
        assert f"merge-review {REQUESTED_SHA} unsatisfied" in result.output
        assert "REFUSED" in result.stderr

    def test_unknown_value_unsatisfied(
        self, runner: CliRunner, gated_instance: CruxibleInstance
    ) -> None:
        result = _chdir_run(
            runner,
            gated_instance.root,
            ["gate", "check", "merge-review", "--value", UNKNOWN_SHA],
        )
        assert result.exit_code == 1

    def test_mixed_candidates_fail_with_verdict_per_candidate(
        self, runner: CliRunner, gated_instance: CruxibleInstance
    ) -> None:
        result = _chdir_run(
            runner,
            gated_instance.root,
            [
                "gate",
                "check",
                "merge-review",
                "--value",
                APPROVED_SHA,
                "--value",
                REQUESTED_SHA,
            ],
        )
        assert result.exit_code == 1
        assert f"merge-review {APPROVED_SHA} satisfied" in result.output
        assert f"merge-review {REQUESTED_SHA} unsatisfied" in result.output

    def test_verdicts_on_stdout_errors_on_stderr(
        self, runner: CliRunner, gated_instance: CruxibleInstance
    ) -> None:
        result = _chdir_run(
            runner,
            gated_instance.root,
            ["gate", "check", "merge-review", "--value", REQUESTED_SHA],
        )
        assert f"merge-review {REQUESTED_SHA} unsatisfied" not in result.stderr
        assert "REFUSED" in result.stderr

    def test_value_bypasses_adapter_even_with_stdin(
        self, runner: CliRunner, gated_instance: CruxibleInstance
    ) -> None:
        # The override never consults the adapter: protocol garbage on stdin
        # is ignored when explicit values are given.
        result = _chdir_run(
            runner,
            gated_instance.root,
            ["gate", "check", "merge-review", "--value", APPROVED_SHA],
            stdin="not a protocol line\n",
        )
        assert result.exit_code == 0

    def test_candidate_option_is_public_and_value_option_is_hidden(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["gate", "check", "--help"])
        assert result.exit_code == 0
        assert "--candidate" in result.output
        assert "--value" not in result.output


class TestGateCheckGeneric:
    def test_stdin_candidates_satisfied_with_blank_lines_ignored(
        self, runner: CliRunner, gated_instance: CruxibleInstance
    ) -> None:
        result = _chdir_run(
            runner,
            gated_instance.root,
            ["gate", "check", "action-review"],
            stdin=f"\n  {APPROVED_SHA}  \n\n",
        )
        assert result.exit_code == 0
        assert f"action-review {APPROVED_SHA} satisfied" in result.output

    def test_unsatisfied_stdin_candidate_refused(
        self, runner: CliRunner, gated_instance: CruxibleInstance
    ) -> None:
        result = _chdir_run(
            runner,
            gated_instance.root,
            ["gate", "check", "action-review"],
            stdin=f"{APPROVED_SHA}\n{REQUESTED_SHA}\n",
        )
        assert result.exit_code == 1
        assert f"action-review {APPROVED_SHA} satisfied" in result.output
        assert f"action-review {REQUESTED_SHA} unsatisfied" in result.output
        assert "REFUSED" in result.stderr

    def test_candidate_arguments_are_supported_and_bypass_stdin(
        self, runner: CliRunner, gated_instance: CruxibleInstance
    ) -> None:
        result = _chdir_run(
            runner,
            gated_instance.root,
            ["gate", "check", "action-review", "--candidate", APPROVED_SHA],
            stdin="ignored input\n",
        )
        assert result.exit_code == 0
        assert f"action-review {APPROVED_SHA} satisfied" in result.output

    @pytest.mark.parametrize("stdin", ["", "\n  \n"])
    def test_empty_candidate_set_fails_closed(
        self,
        runner: CliRunner,
        gated_instance: CruxibleInstance,
        stdin: str,
    ) -> None:
        result = _chdir_run(
            runner,
            gated_instance.root,
            ["gate", "check", "action-review"],
            stdin=stdin,
        )
        assert result.exit_code == 2
        assert "generic gate received no candidate values" in result.stderr
        receipts = _gate_receipts(gated_instance)
        assert len(receipts) == 1
        assert receipts[0] is not None
        assert receipts[0].parameters["verdict"] == "error"
        assert "generic gate received no candidate values" in receipts[0].parameters["reason"]

    def test_tty_stdin_fails_closed(
        self, runner: CliRunner, gated_instance: CruxibleInstance
    ) -> None:
        class TTYInput(io.BytesIO):
            def isatty(self) -> bool:
                return True

        result = _chdir_run(
            runner,
            gated_instance.root,
            ["gate", "check", "action-review"],
            stdin=TTYInput(),
        )
        assert result.exit_code == 2
        assert "stdin is a terminal" in result.stderr

    def test_candidate_arguments_are_refused_for_git_kind(
        self, runner: CliRunner, gated_instance: CruxibleInstance
    ) -> None:
        result = _chdir_run(
            runner,
            gated_instance.root,
            ["gate", "check", "merge-review", "--candidate", APPROVED_SHA],
        )
        assert result.exit_code == 2
        assert "supported only for gates of kind generic" in result.stderr


class TestGateCheckFailClosed:
    def test_unknown_gate_name(self, runner: CliRunner, gated_instance: CruxibleInstance) -> None:
        result = _chdir_run(
            runner,
            gated_instance.root,
            ["gate", "check", "release-review", "--value", APPROVED_SHA],
        )
        assert result.exit_code == 2
        assert "no gate named 'release-review'" in result.stderr
        assert "merge-review" in result.stderr  # instructive: lists declared gates

    def test_no_gates_element_in_config(
        self, runner: CliRunner, initialized_project: CruxibleInstance
    ) -> None:
        result = _chdir_run(
            runner,
            initialized_project.root,
            ["gate", "check", "merge-review", "--value", APPROVED_SHA],
        )
        assert result.exit_code == 2
        assert "declares no gates element" in result.stderr

    def test_unknown_kind_fails_closed(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A gate declaring a kind this build has no adapter for must refuse
        # (exit 2), never silently pass. GateSchema keeps kind permissive so
        # the declaration still parses; dispatch is the fail-closed point.
        def _future_schema(self: object, instance_id: str) -> dict[str, object]:
            return {
                "gates": {
                    "deploy-approved": {
                        "kind": "ci-status",
                        "entity_type": "ReviewRequest",
                        "match_property": "change_head",
                        "condition": {"status": "approved"},
                    }
                }
            }

        monkeypatch.setattr("cruxible_client.CruxibleClient.schema", _future_schema)
        result = _chdir_run(
            runner,
            tmp_path,
            [
                "--server-url",
                "http://127.0.0.1:9",
                "--instance-id",
                "inst_x",
                "gate",
                "check",
                "deploy-approved",
            ],
            stdin="irrelevant\n",
        )
        assert result.exit_code == 2
        assert "no source adapter for gate kind 'ci-status'" in result.stderr

    def test_daemon_unreachable(self, runner: CliRunner, tmp_path: Path) -> None:
        result = _chdir_run(
            runner,
            tmp_path,
            [
                "--server-url",
                "http://127.0.0.1:9",
                "--instance-id",
                "inst_x",
                "gate",
                "check",
                "merge-review",
                "--value",
                APPROVED_SHA,
            ],
        )
        assert result.exit_code == 2
        assert "cannot evaluate" in result.stderr

    def test_invalid_token(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _raise_auth(self: object, instance_id: str) -> dict[str, object]:
            raise AuthenticationError("Unauthorized")

        monkeypatch.setattr("cruxible_client.CruxibleClient.schema", _raise_auth)
        result = _chdir_run(
            runner,
            tmp_path,
            [
                "--server-url",
                "http://127.0.0.1:9",
                "--instance-id",
                "inst_x",
                "gate",
                "check",
                "merge-review",
                "--value",
                APPROVED_SHA,
            ],
        )
        assert result.exit_code == 2
        assert "cannot evaluate" in result.stderr
        assert "AuthenticationError" in result.stderr

    def test_empty_stdin_fails_closed(
        self, runner: CliRunner, gated_instance: CruxibleInstance
    ) -> None:
        # No --value override and nothing on stdin: the git-pre-push adapter
        # has no protocol input, so the check cannot evaluate.
        result = _chdir_run(
            runner,
            gated_instance.root,
            ["gate", "check", "merge-review"],
            stdin="",
        )
        assert result.exit_code == 2
        assert "empty pre-push stdin" in result.stderr

    def test_malformed_stdin(self, runner: CliRunner, gated_instance: CruxibleInstance) -> None:
        result = _chdir_run(
            runner,
            gated_instance.root,
            ["gate", "check", "merge-review"],
            stdin="refs/heads/main only-two-fields\n",
        )
        assert result.exit_code == 2
        assert "malformed pre-push stdin" in result.stderr
        receipts = _gate_receipts(gated_instance)
        assert len(receipts) == 1
        assert receipts[0] is not None
        assert receipts[0].parameters["verdict"] == "error"
        assert "malformed pre-push stdin" in receipts[0].parameters["reason"]

    def test_git_failure(self, runner: CliRunner, gated_instance: CruxibleInstance) -> None:
        # Instance root is not a git repository, so rev-list fails.
        stdin = f"refs/heads/main {APPROVED_SHA} refs/heads/main {REQUESTED_SHA}\n"
        result = _chdir_run(
            runner,
            gated_instance.root,
            ["gate", "check", "merge-review"],
            stdin=stdin,
        )
        assert result.exit_code == 2
        assert "git rev-list" in result.stderr


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _build_merge_repo(repo: Path) -> tuple[str, str, str]:
    """Create a repo with one merge into main.

    Returns (base_sha, merged_tip_sha, main_head_sha).
    """
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "gate-test@example.com")
    _git(repo, "config", "user.name", "Gate Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "wi-feature")
    (repo / "b.txt").write_text("b\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-q", "-m", "feature work")
    tip_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge wi-feature", "wi-feature")
    main_sha = _git(repo, "rev-parse", "HEAD")
    return base_sha, tip_sha, main_sha


@pytest.fixture
def merge_repo_instance(tmp_path: Path) -> tuple[CruxibleInstance, str, str, str]:
    """Gated instance whose root is also a git repo with one merge into main."""
    base_sha, tip_sha, main_sha = _build_merge_repo(tmp_path)
    (tmp_path / "config.yaml").write_text(GATED_CONFIG_YAML)
    instance = CruxibleInstance.init(tmp_path, "config.yaml")
    return instance, base_sha, tip_sha, main_sha


def _approve(instance: CruxibleInstance, sha: str) -> None:
    graph = instance.load_graph()
    graph.add_entity(_review("RR-tip", "approved", sha))
    instance.save_graph(graph)


class TestGateCheckGitPrePush:
    """The gate's declared kind (git-pre-push) drives candidates; no flag."""

    def test_approved_merge_tip_passes(
        self,
        runner: CliRunner,
        merge_repo_instance: tuple[CruxibleInstance, str, str, str],
    ) -> None:
        instance, base_sha, tip_sha, main_sha = merge_repo_instance
        _approve(instance, tip_sha)
        stdin = f"refs/heads/main {main_sha} refs/heads/main {base_sha}\n"
        result = _chdir_run(
            runner,
            instance.root,
            ["gate", "check", "merge-review"],
            stdin=stdin,
        )
        assert result.exit_code == 0
        assert f"merge-review {tip_sha} satisfied" in result.output
        assert "merge" in result.output  # provenance context on the verdict line

    def test_unapproved_merge_tip_refused(
        self,
        runner: CliRunner,
        merge_repo_instance: tuple[CruxibleInstance, str, str, str],
    ) -> None:
        instance, base_sha, tip_sha, main_sha = merge_repo_instance
        stdin = f"refs/heads/main {main_sha} refs/heads/main {base_sha}\n"
        result = _chdir_run(
            runner,
            instance.root,
            ["gate", "check", "merge-review"],
            stdin=stdin,
        )
        assert result.exit_code == 1
        assert f"merge-review {tip_sha} unsatisfied" in result.output
        assert "REFUSED" in result.stderr

    def test_new_branch_all_zeros_remote_sha_still_gates(
        self,
        runner: CliRunner,
        merge_repo_instance: tuple[CruxibleInstance, str, str, str],
    ) -> None:
        # New remote branch: no remote-tracking refs exist, so every merge in
        # local history is in scope (local_sha --not --remotes).
        instance, _base_sha, tip_sha, main_sha = merge_repo_instance
        stdin = f"refs/heads/main {main_sha} refs/heads/main {'0' * 40}\n"
        result = _chdir_run(
            runner,
            instance.root,
            ["gate", "check", "merge-review"],
            stdin=stdin,
        )
        assert result.exit_code == 1
        assert f"merge-review {tip_sha} unsatisfied" in result.output

    def test_ref_deletion_skipped(
        self,
        runner: CliRunner,
        merge_repo_instance: tuple[CruxibleInstance, str, str, str],
    ) -> None:
        instance, base_sha, _tip_sha, _main_sha = merge_repo_instance
        stdin = f"(delete) {'0' * 40} refs/heads/main {base_sha}\n"
        result = _chdir_run(
            runner,
            instance.root,
            ["gate", "check", "merge-review"],
            stdin=stdin,
        )
        assert result.exit_code == 0
        assert "nothing to evaluate" in result.stderr

    def test_non_matching_ref_not_gated(
        self,
        runner: CliRunner,
        merge_repo_instance: tuple[CruxibleInstance, str, str, str],
    ) -> None:
        instance, base_sha, _tip_sha, main_sha = merge_repo_instance
        stdin = f"refs/heads/wi-feature {main_sha} refs/heads/wi-feature {base_sha}\n"
        result = _chdir_run(
            runner,
            instance.root,
            ["gate", "check", "merge-review"],
            stdin=stdin,
        )
        assert result.exit_code == 0
        assert "nothing to evaluate" in result.stderr

    def test_invalid_sha_token_refused_before_git(
        self, runner: CliRunner, gated_instance: CruxibleInstance
    ) -> None:
        # A crafted token in SHA position (e.g. a git flag) must refuse with
        # exit 2, never silence evaluation by reaching git argv.
        stdin = f"refs/heads/main --max-count=0 refs/heads/main {'0' * 40}\n"
        result = _chdir_run(
            runner,
            gated_instance.root,
            ["gate", "check", "merge-review"],
            stdin=stdin,
        )
        assert result.exit_code == 2
        assert "invalid commit SHA" in result.stderr
        assert "--max-count=0" in result.stderr

    def test_no_merges_in_range_passes_with_notice(
        self,
        runner: CliRunner,
        merge_repo_instance: tuple[CruxibleInstance, str, str, str],
    ) -> None:
        instance, base_sha, tip_sha, _main_sha = merge_repo_instance
        # Push only the linear feature history: base..tip contains no merges.
        stdin = f"refs/heads/main {tip_sha} refs/heads/main {base_sha}\n"
        result = _chdir_run(
            runner,
            instance.root,
            ["gate", "check", "merge-review"],
            stdin=stdin,
        )
        assert result.exit_code == 0
        assert "nothing to evaluate" in result.stderr


def _build_octopus_repo(repo: Path) -> tuple[str, str, str, str]:
    """Create a repo with an octopus merge of two branches into main.

    Returns (base_sha, tip1_sha, tip2_sha, main_head_sha).
    """
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "gate-test@example.com")
    _git(repo, "config", "user.name", "Gate Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    tips: list[str] = []
    for branch, file_name in (("wi-one", "b.txt"), ("wi-two", "c.txt")):
        _git(repo, "checkout", "-q", "-b", branch, "main")
        (repo / file_name).write_text(f"{file_name}\n")
        _git(repo, "add", file_name)
        _git(repo, "commit", "-q", "-m", f"work on {branch}")
        tips.append(_git(repo, "rev-parse", "HEAD"))
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "octopus merge", "wi-one", "wi-two")
    main_sha = _git(repo, "rev-parse", "HEAD")
    return base_sha, tips[0], tips[1], main_sha


@pytest.fixture
def octopus_repo_instance(tmp_path: Path) -> tuple[CruxibleInstance, str, str, str, str]:
    """Gated instance whose root holds an octopus merge into main."""
    base_sha, tip1_sha, tip2_sha, main_sha = _build_octopus_repo(tmp_path)
    (tmp_path / "config.yaml").write_text(GATED_CONFIG_YAML)
    instance = CruxibleInstance.init(tmp_path, "config.yaml")
    return instance, base_sha, tip1_sha, tip2_sha, main_sha


class TestGateCheckOctopusMerge:
    def test_partially_approved_octopus_refused_naming_unapproved_tip(
        self,
        runner: CliRunner,
        octopus_repo_instance: tuple[CruxibleInstance, str, str, str, str],
    ) -> None:
        instance, base_sha, tip1_sha, tip2_sha, main_sha = octopus_repo_instance
        _approve(instance, tip1_sha)
        stdin = f"refs/heads/main {main_sha} refs/heads/main {base_sha}\n"
        result = _chdir_run(
            runner,
            instance.root,
            ["gate", "check", "merge-review"],
            stdin=stdin,
        )
        assert result.exit_code == 1
        assert f"merge-review {tip1_sha} satisfied" in result.output
        assert f"merge-review {tip2_sha} unsatisfied" in result.output
        assert "REFUSED" in result.stderr

    def test_fully_approved_octopus_passes(
        self,
        runner: CliRunner,
        octopus_repo_instance: tuple[CruxibleInstance, str, str, str, str],
    ) -> None:
        instance, base_sha, tip1_sha, tip2_sha, main_sha = octopus_repo_instance
        graph = instance.load_graph()
        graph.add_entity(_review("RR-t1", "approved", tip1_sha))
        graph.add_entity(_review("RR-t2", "approved", tip2_sha))
        instance.save_graph(graph)
        stdin = f"refs/heads/main {main_sha} refs/heads/main {base_sha}\n"
        result = _chdir_run(
            runner,
            instance.root,
            ["gate", "check", "merge-review"],
            stdin=stdin,
        )
        assert result.exit_code == 0
        assert f"merge-review {tip1_sha} satisfied" in result.output
        assert f"merge-review {tip2_sha} satisfied" in result.output


class TestGateCheckEvaluationHardening:
    def test_abbreviated_stored_sha_does_not_satisfy_full_candidate(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        # Exact-match contract: a stored 12-char abbreviation must never
        # satisfy a full-sha candidate.
        (tmp_path / "config.yaml").write_text(GATED_CONFIG_YAML)
        instance = CruxibleInstance.init(tmp_path, "config.yaml")
        graph = EntityGraph()
        graph.add_entity(_review("RR-abbrev", "approved", APPROVED_SHA[:12]))
        instance.save_graph(graph)
        result = _chdir_run(
            runner,
            instance.root,
            ["gate", "check", "merge-review", "--value", APPROVED_SHA],
        )
        assert result.exit_code == 1
        assert f"merge-review {APPROVED_SHA} unsatisfied" in result.output

    def test_mid_loop_query_error_fails_closed(
        self,
        runner: CliRunner,
        gated_instance: CruxibleInstance,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # First candidate evaluates satisfied; the second query erroring must
        # collapse the whole check to exit 2, never a partial verdict exit.
        from cruxible_core.service import gates as gates_service

        real_satisfying_entity_ids = gates_service._satisfying_entity_ids
        calls = {"count": 0}

        def flaky_satisfying_entity_ids(*args: object, **kwargs: object) -> list[str]:
            calls["count"] += 1
            if calls["count"] >= 2:
                raise RuntimeError("state backend exploded")
            return real_satisfying_entity_ids(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(gates_service, "_satisfying_entity_ids", flaky_satisfying_entity_ids)
        result = _chdir_run(
            runner,
            gated_instance.root,
            [
                "gate",
                "check",
                "merge-review",
                "--value",
                APPROVED_SHA,
                "--value",
                REQUESTED_SHA,
            ],
        )
        assert result.exit_code == 2
        assert f"merge-review {APPROVED_SHA} satisfied" in result.output
        assert "cannot evaluate" in result.stderr

    def test_malformed_server_gate_payload_fails_closed(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A server payload whose gates element fails GateSchema validation
        # must exit 2 with a clean error, not a pydantic traceback.
        def _bad_schema(self: object, instance_id: str) -> dict[str, object]:
            return {"gates": {"merge-review": {"entity_type": "ReviewRequest"}}}

        monkeypatch.setattr("cruxible_client.CruxibleClient.schema", _bad_schema)
        server_args = ["--server-url", "http://127.0.0.1:9", "--instance-id", "inst_x"]
        result = _chdir_run(
            runner,
            tmp_path,
            [*server_args, "gate", "check", "merge-review", "--value", APPROVED_SHA],
        )
        assert result.exit_code == 2
        assert "failed validation" in result.stderr
        assert "Traceback" not in result.stderr

        listed = _chdir_run(runner, tmp_path, [*server_args, "gate", "list"])
        assert listed.exit_code != 0
        assert "failed validation" in listed.stderr
        assert "Traceback" not in listed.stderr
