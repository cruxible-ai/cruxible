"""Target-visibility guardrails for surviving Playbill and credential writes."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_client.authoring.world_stub import render_world_stub_for
from cruxible_core.cli.commands import playbill as playbill_commands
from cruxible_core.cli.context import CliContextState, save_cli_context
from cruxible_core.cli.main import MUTATING_COMMAND_TARGETS, cli


@pytest.fixture(autouse=True)
def _isolate_target_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "CRUXIBLE_SERVER_URL",
        "CRUXIBLE_SERVER_SOCKET",
        "CRUXIBLE_INSTANCE_ID",
        "CRUXIBLE_PLAYBILL_WORKSPACE",
        "CRUXIBLE_NO_WORKSPACE",
    ):
        monkeypatch.delenv(name, raising=False)


EXPECTED_MUTATING_COMMAND_TARGETS = {
    ("playbill", "host", "create"): "create",
    ("playbill", "workspace", "attach"): "manual",
    ("playbill", "workspace", "detach"): "manual",
    ("playbill", "init"): "active",
    ("playbill", "instance", "decommission"): "active",
    ("playbill", "body", "store"): "active",
    ("playbill", "provider", "seed"): "active",
    ("playbill", "ledger", "set-mirror"): "active",
    ("playbill", "ledger", "publish"): "active",
    ("playbill", "document", "propose"): "active",
    ("playbill", "claim-type", "propose"): "active",
    ("playbill", "claim-type", "migrate"): "active",
    ("playbill", "block", "depublish"): "active",
    ("playbill", "claim", "retire"): "active",
    ("playbill", "claim", "attest"): "active",
    ("playbill", "predict"): "active",
    ("playbill", "settle"): "active",
    ("playbill", "claim-attestation", "recover"): "active",
    ("playbill", "authoring", "create"): "manual",
    ("playbill", "authoring", "bind"): "active",
    ("playbill", "authoring", "compile"): "active",
    ("playbill", "authoring", "preflight"): "active",
    ("playbill", "authoring", "rebase"): "active",
    ("playbill", "authoring", "submit"): "active",
    ("playbill", "procedure", "bind"): "active",
    ("playbill", "procedure", "run"): "active",
    ("playbill", "line", "run"): "active",
    ("playbill", "proposal", "approve"): "active",
    ("playbill", "proposal", "activate"): "active",
    ("playbill", "proposal", "readmit"): "active",
    ("playbill", "proposal", "withdraw"): "active",
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
        def resolve_playbill_proposal_selector(
            self, instance_id: str, selector: str
        ) -> contracts.PlaybillProposalSelectorResultV1:
            assert instance_id == "inst_remembered"
            return contracts.PlaybillProposalSelectorResultV1(
                selector=selector,
                proposal_id=selector,
            )

        def activate_playbill_proposal(
            self, instance_id: str, proposal_id: str
        ) -> contracts.PlaybillActivationReceipt:
            assert (instance_id, proposal_id) == ("inst_remembered", "proposal-1")
            return contracts.PlaybillActivationReceipt(
                proposal_id=proposal_id,
                activated_by="owner",
                status="lost_cas",
                accepted_coordinate=None,
                workspace_advertisement={"status": "not_attached", "workspace_path": None},
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


def test_two_attached_workspaces_route_the_same_write_to_their_own_instance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    context_path = tmp_path / "context.json"
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(context_path))
    for name in (
        "CRUXIBLE_SERVER_URL",
        "CRUXIBLE_SERVER_SOCKET",
        "CRUXIBLE_INSTANCE_ID",
        "CRUXIBLE_PLAYBILL_WORKSPACE",
    ):
        monkeypatch.delenv(name, raising=False)
    save_cli_context(
        CliContextState(
            server_url="https://global.example.test",
            instance_id="inst_global",
        )
    )
    calls: list[str] = []

    class StubClient:
        def store_playbill_body(
            self, instance_id: str, content: bytes
        ) -> contracts.PlaybillCasObjectResult:
            calls.append(instance_id)
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
    body = tmp_path / "body.md"
    body.write_text("# governed\n", encoding="utf-8")
    for label in ("first", "second"):
        workspace = tmp_path / label
        (workspace / ".playbill").mkdir(parents=True)
        (workspace / ".playbill" / "coverage.json").write_text(
            json.dumps(
                {
                    "tag": "playbill-coverage-workspace-config-v2",
                    "server_url": f"https://{label}.example.test",
                    "instance_id": f"inst_{label}",
                    "rules": [],
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.chdir(workspace)
        result = CliRunner().invoke(
            cli,
            ["playbill", "body", "store", str(body), "--json"],
        )
        assert result.exit_code == 0, result.output
        assert result.stderr == (
            f"target: inst_{label} @ https://{label}.example.test (workspace)\n"
        )

    assert calls == ["inst_first", "inst_second"]


def test_host_creation_names_explicit_requested_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    monkeypatch.chdir(tmp_path)

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


def test_explicit_transport_does_not_inherit_another_daemons_remembered_instance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    monkeypatch.chdir(tmp_path)
    save_cli_context(
        CliContextState(
            server_url="https://remembered.example.test/",
            instance_id="inst_remembered",
        )
    )

    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://other.example.test",
            "playbill",
            "proposal",
            "activate",
            "proposal-1",
        ],
    )

    assert result.exit_code == 2
    assert "context_instance_transport_mismatch" in result.output
    assert "https://remembered.example.test" in result.output
    assert "https://other.example.test" in result.output
    assert "--instance-id <id>" in result.output
    assert "inst_remembered" not in result.stderr


def test_explicit_transport_and_instance_are_used_together(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    monkeypatch.chdir(tmp_path)
    save_cli_context(
        CliContextState(
            server_url="https://remembered.example.test",
            instance_id="inst_remembered",
        )
    )

    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://other.example.test",
            "--instance-id",
            "inst_explicit",
            "context",
            "show",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["server_url"] == "https://other.example.test"
    assert payload["instance_id"] == "inst_explicit"
    assert payload["transport_source"] == "explicit"
    assert payload["instance_source"] == "explicit"


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
                    "tag": "playbill-coverage-result-v3",
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
                        "tag": "playbill-coverage-batch-summary-v3",
                        "exact": 0,
                        "drifted": 0,
                        "candidate": 0,
                        "none": 0,
                        "returned_spans": 0,
                        "omitted_card_count": 0,
                    },
                    "health": "complete",
                    "global_scan_complete": True,
                    "truncation_reason_codes": [],
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


def test_host_show_is_a_silent_read_and_cli_adds_transport(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    assert ("playbill", "host", "show") not in MUTATING_COMMAND_TARGETS

    class StubClient:
        def show_playbill_host(self, instance_id: str) -> contracts.PlaybillHostInspectionV1:
            return contracts.PlaybillHostInspectionV1(
                instance_id=instance_id,
                managed_root=str(tmp_path / "state"),
                workspace_root=None,
                compatibility="uninitialized",
                writable=False,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    socket = tmp_path / "daemon.sock"
    result = CliRunner().invoke(
        cli,
        [
            "--server-socket",
            str(socket),
            "playbill",
            "host",
            "show",
            "inst_show",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["instance_id"] == "inst_show"
    assert payload["transport"] == f"unix socket {socket}"


def test_claim_type_template_does_not_announce_a_write_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: (_ for _ in ()).throw(AssertionError("template must stay local")),
    )

    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://example.test",
            "--instance-id",
            "inst_template",
            "playbill",
            "claim-type",
            "propose",
            "--template",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stderr == ""


def test_workspace_attach_writes_config_only_after_exact_daemon_registration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    subprocess.run(["git", "init", "-q", "-b", "main", str(workspace)], check=True)
    socket = tmp_path / "daemon.sock"
    monkeypatch.chdir(workspace)

    class StubClient:
        def playbill_host_workspace_registration(
            self, instance_id: str
        ) -> contracts.PlaybillHostWorkspaceRegistrationV1:
            assert instance_id == "inst_attached"
            return contracts.PlaybillHostWorkspaceRegistrationV1(
                instance_id=instance_id,
                status="registered",
                workspace_path=str(workspace.resolve()),
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = CliRunner().invoke(
        cli,
        [
            "--server-socket",
            str(socket),
            "playbill",
            "workspace",
            "attach",
            "--instance-id",
            "inst_attached",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["instance_id"] == "inst_attached"
    assert payload["workspace_root"] == str(workspace.resolve())
    assert payload["transport"] == str(socket.resolve())
    assert result.stderr == f"target: inst_attached @ unix://{socket.resolve()} (explicit)\n"
    config = json.loads((workspace / ".playbill" / "coverage.json").read_text())
    assert config["instance_id"] == "inst_attached"
    assert config["server_socket"] == str(socket.resolve())
    assert not any(
        fragment in json.dumps(config).lower()
        for fragment in ("bearer", "password", "secret", "token")
    )


def test_workspace_attach_marks_a_remembered_target_as_remembered(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "remembered-workspace"
    subprocess.run(["git", "init", "-q", "-b", "main", str(workspace)], check=True)
    socket = tmp_path / "remembered-daemon.sock"
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    save_cli_context(
        CliContextState(
            server_socket=str(socket),
            instance_id="inst_remembered",
            instance_transport=f"unix://{socket.resolve()}",
        )
    )

    class StubClient:
        def playbill_host_workspace_registration(
            self, instance_id: str
        ) -> contracts.PlaybillHostWorkspaceRegistrationV1:
            assert instance_id == "inst_remembered"
            return contracts.PlaybillHostWorkspaceRegistrationV1(
                instance_id=instance_id,
                status="registered",
                workspace_path=str(workspace.resolve()),
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = CliRunner().invoke(cli, ["playbill", "workspace", "attach", "--json"])

    assert result.exit_code == 0, result.output
    assert result.stderr == (f"target: inst_remembered @ unix://{socket.resolve()} (remembered)\n")


def test_workspace_attach_refuses_a_different_registration_without_writing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    other = tmp_path / "other-workspace"
    subprocess.run(["git", "init", "-q", "-b", "main", str(workspace)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(other)], check=True)
    monkeypatch.chdir(workspace)

    class StubClient:
        def playbill_host_workspace_registration(
            self, instance_id: str
        ) -> contracts.PlaybillHostWorkspaceRegistrationV1:
            return contracts.PlaybillHostWorkspaceRegistrationV1(
                instance_id=instance_id,
                status="registered",
                workspace_path=str(other.resolve()),
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = CliRunner().invoke(
        cli,
        [
            "--server-socket",
            str(tmp_path / "daemon.sock"),
            "playbill",
            "workspace",
            "attach",
            "--instance-id",
            "inst_other",
        ],
    )

    assert result.exit_code == 1
    assert "playbill.workspace.registration_disagrees" in result.output
    assert "cruxible playbill host create --instance-id inst_other" in result.output
    assert not (workspace / ".playbill" / "coverage.json").exists()


def test_instance_decommission_names_the_instance_it_is_about_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one irreversible verb must name its target before it runs."""

    calls: list[tuple[str, str]] = []

    class StubClient:
        def decommission_playbill_instance(
            self, instance_id: str, *, reason: str
        ) -> contracts.PlaybillInstanceDecommissionResultV1:
            calls.append((instance_id, reason))
            return contracts.PlaybillInstanceDecommissionResultV1(
                instance_id=instance_id,
                reason=reason,
                decommissioned_at="2026-09-03T12:00:00.000000Z",
                decommissioned_by="owner",
                coordinate=contracts.PlaybillAcceptedCoordinate(
                    git_oid="0" * 40,
                    semantic_root="sha256:" + "2" * 64,
                    generation_root="sha256:" + "3" * 64,
                    compiler_digest="sha256:" + "4" * 64,
                ),
            )

    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: StubClient(),
    )
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://terminal.example.test",
            "--instance-id",
            "inst_terminal",
            "playbill",
            "instance",
            "decommission",
            "--reason",
            "superseded",
            "--yes",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [("inst_terminal", "superseded")]
    assert result.stderr == ("target: inst_terminal @ https://terminal.example.test (explicit)\n")


def test_the_world_stub_leaf_is_a_read_and_stays_out_of_the_mutating_inventory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Writing a `.pyi` is a read of the accepted world; it names no write target.

    The inventory above is equality-tested, so this is not a second assertion of
    the same fact: it pins the *reason* the leaf is absent from it, by driving a
    real generation from a workspace-less cwd and proving the command stays
    silent on stderr the way every other read does.
    """

    path = ("playbill", "world", "stub")
    assert path not in MUTATING_COMMAND_TARGETS
    assert _command_at_path(path).callback is not None

    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    coordinate = contracts.PlaybillAcceptedCoordinate(
        git_oid="11" * 20,
        semantic_root="sha256:" + "aa" * 32,
        generation_root="sha256:" + "22" * 32,
        compiler_digest="sha256:" + "bb" * 32,
    )

    class StubClient:
        def search_playbill(
            self, instance_id: str, **values: object
        ) -> contracts.PlaybillSearchResult:
            assert instance_id == "inst_read"
            return contracts.PlaybillSearchResult(
                mode="orient",
                coordinate=coordinate,
                evaluation_time="2026-09-07T12:00:00Z",
                rows=[],
                orientation={"state": "empty"},
                selection_basis_digest="sha256:" + "cc" * 32,
                truncated=False,
                result_digest="sha256:" + "dd" * 32,
            )

        def list_playbill_claim_types(
            self, instance_id: str, **_values: object
        ) -> contracts.PlaybillClaimTypeList:
            assert instance_id == "inst_read"
            return contracts.PlaybillClaimTypeList(
                coordinate=coordinate,
                claim_types=[
                    contracts.PlaybillClaimTypeView(
                        coordinate=coordinate,
                        path="claim-types/sec.vuln.severity.json",
                        predicate="sec.vuln.severity",
                        identity="ClaimType:sec.vuln.severity",
                        artifact_digest="sha256:" + "ee" * 32,
                        envelope={
                            "artifact_format": "playbill-claim-type-v1",
                            "identity": {"kind": "ClaimType", "name": "sec.vuln.severity"},
                            "predicate": "sec.vuln.severity",
                            "allowed_subject_kinds": ("sec.vulnerability",),
                            "object_kind": "literal",
                            "literal_schema": {"type": "string", "enum": ["high"]},
                            "allowed_object_subject_kinds": (),
                            "cardinality": "one",
                            "permitted_roles": ("observation",),
                            "referent_sensitivity": "identity",
                            "lifecycle": {"state": "live"},
                        },
                    )
                ],
            )

        def list_playbill_subjects(
            self, instance_id: str, **_values: object
        ) -> contracts.PlaybillSubjectList:
            assert instance_id == "inst_read"
            return contracts.PlaybillSubjectList(coordinate=coordinate, subjects=[])

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://read.example.test",
            "--instance-id",
            "inst_read",
            "playbill",
            "world",
            "stub",
        ],
    )

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert result.stdout.startswith("# playbill-world-stub-v1:")
    assert "class _W_sec__vuln__severity(ClaimTypeRef):" in result.stdout
    # The leaf reaches the client through the client package's own sanctioned
    # entry point, not through a private constructor.
    assert render_world_stub_for is playbill_commands.render_world_stub_for
