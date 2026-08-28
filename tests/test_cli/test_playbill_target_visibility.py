"""Target-visibility guardrails for surviving Playbill and credential writes."""

from __future__ import annotations

from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.context import CliContextState, save_cli_context
from cruxible_core.cli.main import MUTATING_COMMAND_TARGETS, cli

EXPECTED_MUTATING_COMMAND_TARGETS = {
    ("playbill", "host", "create"): "create",
    ("playbill", "init"): "active",
    ("playbill", "body", "store"): "active",
    ("playbill", "document", "propose"): "active",
    ("playbill", "subject", "propose"): "active",
    ("playbill", "claim-type", "propose"): "active",
    ("playbill", "claim-type", "migrate"): "active",
    ("playbill", "claim", "propose"): "active",
    ("playbill", "claim", "propose-batch"): "active",
    ("playbill", "claim", "retire"): "active",
    ("playbill", "authoring", "create"): "manual",
    ("playbill", "authoring", "bind"): "active",
    ("playbill", "authoring", "compile"): "active",
    ("playbill", "authoring", "preflight"): "active",
    ("playbill", "authoring", "rebase"): "active",
    ("playbill", "authoring", "submit"): "active",
    ("playbill", "seed", "apply"): "manual",
    ("playbill", "query", "propose"): "active",
    ("playbill", "procedure", "bind"): "active",
    ("playbill", "proposal", "approve"): "active",
    ("playbill", "proposal", "activate"): "active",
    ("playbill", "proposal", "readmit"): "active",
    ("playbill", "sources", "propose"): "active",
    ("playbill", "principal", "add"): "active",
    ("playbill", "principal", "rotate"): "active",
    ("playbill", "principal", "recover"): "active",
    ("playbill", "principal", "revoke"): "active",
    ("credential", "claim-bootstrap"): "active",
    ("credential", "mint"): "active",
    ("credential", "recover-admin"): "manual",
    ("credential", "revoke"): "active",
    ("credential", "rotate"): "active",
}


def _command_at_path(path: tuple[str, ...]) -> click.Command:
    command: click.Command = cli
    for name in path:
        assert isinstance(command, click.Group)
        command = command.commands[name]
    return command


def test_mutating_command_inventory_is_exact_and_registered() -> None:
    assert MUTATING_COMMAND_TARGETS == EXPECTED_MUTATING_COMMAND_TARGETS
    for path in MUTATING_COMMAND_TARGETS:
        assert _command_at_path(path).callback is not None, path


def test_explicit_playbill_write_names_instance_transport_and_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    body = tmp_path / "body.md"
    body.write_text("# governed\n")

    class StubClient:
        def store_playbill_body(
            self, instance_id: str, content: bytes
        ) -> contracts.PlaybillCasObjectResult:
            assert instance_id == "inst_explicit"
            assert content == b"# governed\n"
            return contracts.PlaybillCasObjectResult(
                digest="sha256:" + "1" * 64,
                present=True,
                byte_length=len(content),
                redacted=False,
            )

    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: StubClient(),
    )
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://explicit.example.test",
            "--instance-id",
            "inst_explicit",
            "playbill",
            "body",
            "store",
            str(body),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stderr == ("target: inst_explicit @ https://explicit.example.test (explicit)\n")


def test_remembered_playbill_write_marks_remembered_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    context_path = tmp_path / "context.json"
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(context_path))
    save_cli_context(
        CliContextState(
            server_url="https://remembered.example.test",
            instance_id="inst_remembered",
        )
    )

    class StubClient:
        def activate_playbill_proposal(
            self, instance_id: str, proposal_id: str
        ) -> contracts.PlaybillActivationReceipt:
            assert (instance_id, proposal_id) == ("inst_remembered", "proposal-1")
            return contracts.PlaybillActivationReceipt(
                proposal_id=proposal_id,
                activated_by="owner",
                status="lost_cas",
                accepted_coordinate=None,
            )

    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: StubClient(),
    )
    result = CliRunner().invoke(
        cli,
        ["playbill", "proposal", "activate", "proposal-1", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert result.stderr == (
        "target: inst_remembered @ https://remembered.example.test (remembered)\n"
    )


def test_host_creation_names_explicit_requested_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))

    class StubClient:
        def create_playbill_host(
            self, *, instance_id: str | None = None
        ) -> contracts.PlaybillHostResult:
            assert instance_id == "inst_requested"
            return contracts.PlaybillHostResult(instance_id=instance_id, status="created")

    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: StubClient(),
    )
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://host.example.test",
            "playbill",
            "host",
            "create",
            "--instance-id",
            "inst_requested",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stderr == (
        "target: inst_requested @ https://host.example.test (transport=explicit)\n"
    )


def test_coverage_commands_are_reads_and_stay_out_of_the_mutating_inventory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Coverage delivery adds no authority, so it announces no write target.

    The inventory above is equality-tested, so this is not a second assertion of
    the same fact: it pins the *reason* the new surface is absent from it, by
    driving a real resolve and proving the command stays silent on stderr the
    way every other read does.
    """

    for path in (("playbill", "coverage", "resolve"), ("playbill", "coverage", "status")):
        assert path not in MUTATING_COMMAND_TARGETS
        assert _command_at_path(path).callback is not None

    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    working = tmp_path / "workspace"
    working.mkdir()
    (working / "notes.txt").write_text("ordinary working notes\n", encoding="utf-8")

    class StubClient:
        def resolve_playbill_coverage(
            self,
            instance_id: str,
            *,
            observations: list[dict[str, object]],
            **_: object,
        ) -> contracts.PlaybillCoverageResult:
            assert instance_id == "inst_read"
            assert len(observations) == 1
            coordinate = contracts.PlaybillAcceptedCoordinate(
                git_oid="11" * 20,
                semantic_root="sha256:" + "aa" * 32,
                generation_root="sha256:" + "22" * 32,
                compiler_digest="sha256:" + "bb" * 32,
            )
            return contracts.PlaybillCoverageResult(
                coordinate=coordinate,
                result={
                    "tag": "playbill-coverage-result-v2",
                    "at": coordinate.model_dump(mode="json"),
                    "instance_id": instance_id,
                    "index_digest": "sha256:" + "cc" * 32,
                    "overlay_digest": "sha256:" + "dd" * 32,
                    "manifest_digest": None,
                    "epoch": None,
                    "watcher_health": "absent",
                    "access_profile": {
                        "tag": "playbill-coverage-access-profile-v1",
                        "profile_id": "playbill.coverage.read",
                        "permitted_access_classes": ["instance", "public"],
                        "disclose_restricted_existence": True,
                    },
                    "scope": [],
                    "spans": [],
                    "summary": {
                        "tag": "playbill-coverage-batch-summary-v2",
                        "exact": 0,
                        "drifted": 0,
                        "candidate": 0,
                        "none": 0,
                        "returned_spans": 0,
                        "omitted_card_count": 0,
                    },
                    "health": "complete",
                    "coverage": {
                        "tag": "playbill-coverage-descriptor-v1",
                        "requested_facets": ["coverage"],
                    },
                },
            )

    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: StubClient(),
    )
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://read.example.test",
            "--instance-id",
            "inst_read",
            "playbill",
            "coverage",
            "resolve",
            "--root",
            str(working),
            "--bind",
            "notes.txt=external:workspace.notes",
            "--file",
            "notes.txt",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert result.stdout.startswith("Playbill coverage: 0 exact, 0 drifted, 0 candidates, 0 none")
