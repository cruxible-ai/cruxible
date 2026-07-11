"""Tests for the gate verb group: list, check, input adapters, fail-closed paths."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from cruxible_client.errors import AuthenticationError
from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.cli.main import cli
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
    entity_type: ReviewRequest
    sha_property: change_head
    predicate: {status: approved}
    applies_to: refs/heads/main
"""

APPROVED_SHA = "a" * 40
REQUESTED_SHA = "b" * 40
UNKNOWN_SHA = "c" * 40


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
    stdin: str | None = None,
) -> Result:
    original = os.getcwd()
    try:
        os.chdir(directory)
        return runner.invoke(cli, args, input=stdin)
    finally:
        os.chdir(original)


class TestGateList:
    def test_list_shows_declared_gate(
        self, runner: CliRunner, gated_instance: CruxibleInstance
    ) -> None:
        result = _chdir_run(runner, gated_instance.root, ["gate", "list"])
        assert result.exit_code == 0
        assert (
            "merge-review: ReviewRequest.change_head where status=approved "
            "(applies_to refs/heads/main)" in result.output
        )

    def test_list_json_shape(self, runner: CliRunner, gated_instance: CruxibleInstance) -> None:
        result = _chdir_run(runner, gated_instance.root, ["gate", "list", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["merge-review"]["sha_property"] == "change_head"
        assert payload["merge-review"]["predicate"] == {"status": "approved"}

    def test_list_without_gates_says_so(
        self, runner: CliRunner, initialized_project: CruxibleInstance
    ) -> None:
        result = _chdir_run(runner, initialized_project.root, ["gate", "list"])
        assert result.exit_code == 0
        assert "No gates declared" in result.output


class TestGateCheckSha:
    def test_approved_sha_satisfied(
        self, runner: CliRunner, gated_instance: CruxibleInstance
    ) -> None:
        result = _chdir_run(
            runner,
            gated_instance.root,
            ["gate", "check", "merge-review", "--sha", APPROVED_SHA],
        )
        assert result.exit_code == 0
        assert f"merge-review {APPROVED_SHA} satisfied" in result.output

    def test_pinned_but_unapproved_sha_unsatisfied(
        self, runner: CliRunner, gated_instance: CruxibleInstance
    ) -> None:
        result = _chdir_run(
            runner,
            gated_instance.root,
            ["gate", "check", "merge-review", "--sha", REQUESTED_SHA],
        )
        assert result.exit_code == 1
        assert f"merge-review {REQUESTED_SHA} unsatisfied" in result.output
        assert "REFUSED" in result.stderr

    def test_unknown_sha_unsatisfied(
        self, runner: CliRunner, gated_instance: CruxibleInstance
    ) -> None:
        result = _chdir_run(
            runner,
            gated_instance.root,
            ["gate", "check", "merge-review", "--sha", UNKNOWN_SHA],
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
                "--sha",
                APPROVED_SHA,
                "--sha",
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
            ["gate", "check", "merge-review", "--sha", REQUESTED_SHA],
        )
        assert f"merge-review {REQUESTED_SHA} unsatisfied" not in result.stderr
        assert "REFUSED" in result.stderr


class TestGateCheckFailClosed:
    def test_unknown_gate_name(self, runner: CliRunner, gated_instance: CruxibleInstance) -> None:
        result = _chdir_run(
            runner,
            gated_instance.root,
            ["gate", "check", "release-review", "--sha", APPROVED_SHA],
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
            ["gate", "check", "merge-review", "--sha", APPROVED_SHA],
        )
        assert result.exit_code == 2
        assert "declares no gates element" in result.stderr

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
                "--sha",
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
                "--sha",
                APPROVED_SHA,
            ],
        )
        assert result.exit_code == 2
        assert "cannot evaluate" in result.stderr
        assert "AuthenticationError" in result.stderr

    def test_no_candidate_source_is_usage_error(
        self, runner: CliRunner, gated_instance: CruxibleInstance
    ) -> None:
        result = _chdir_run(runner, gated_instance.root, ["gate", "check", "merge-review"])
        assert result.exit_code == 2
        assert "--sha or --git-pre-push" in result.stderr

    def test_both_candidate_sources_is_usage_error(
        self, runner: CliRunner, gated_instance: CruxibleInstance
    ) -> None:
        result = _chdir_run(
            runner,
            gated_instance.root,
            ["gate", "check", "merge-review", "--sha", APPROVED_SHA, "--git-pre-push"],
        )
        assert result.exit_code == 2

    def test_malformed_stdin(self, runner: CliRunner, gated_instance: CruxibleInstance) -> None:
        result = _chdir_run(
            runner,
            gated_instance.root,
            ["gate", "check", "merge-review", "--git-pre-push"],
            stdin="refs/heads/main only-two-fields\n",
        )
        assert result.exit_code == 2
        assert "malformed pre-push stdin" in result.stderr

    def test_git_failure(self, runner: CliRunner, gated_instance: CruxibleInstance) -> None:
        # Instance root is not a git repository, so rev-list fails.
        stdin = f"refs/heads/main {APPROVED_SHA} refs/heads/main {REQUESTED_SHA}\n"
        result = _chdir_run(
            runner,
            gated_instance.root,
            ["gate", "check", "merge-review", "--git-pre-push"],
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
            ["gate", "check", "merge-review", "--git-pre-push"],
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
            ["gate", "check", "merge-review", "--git-pre-push"],
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
            ["gate", "check", "merge-review", "--git-pre-push"],
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
            ["gate", "check", "merge-review", "--git-pre-push"],
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
            ["gate", "check", "merge-review", "--git-pre-push"],
            stdin=stdin,
        )
        assert result.exit_code == 0
        assert "nothing to evaluate" in result.stderr

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
            ["gate", "check", "merge-review", "--git-pre-push"],
            stdin=stdin,
        )
        assert result.exit_code == 0
        assert "nothing to evaluate" in result.stderr
