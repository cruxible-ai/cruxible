"""End-to-end proof that a Playbill instance is drivable from the CLI alone.

This is the TauBench-runnable surface, written as the harness recipe it has to
support: allocate a host, bootstrap it, seed a ClaimType, a Subject, two Claims,
and a named entrypoint through the governed propose/activate loop, then
read the resulting accepted state back through every read the loop publishes --
query execution with its receipt, semantic discovery, bounded expansion, and the
deterministic floor.

Every step goes through ``cruxible ...`` argv. Nothing here reaches into a
service, and no fixture writes accepted state on the test's behalf.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner, Result
from fastapi.testclient import TestClient

from cruxible_client import CruxibleClient, contracts
from cruxible_client.contracts.authoring.inputs import (
    ClaimInput,
    LiteralObjectInput,
    QueryDefinitionInput,
    SubjectInput,
    WorkingSelectionInput,
)
from cruxible_client.contracts.captures import (
    capture_contract_digest,
    foreign_source_capture_contract,
)
from cruxible_client.contracts.errors import PlaybillBootstrapError
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicyV1,
    ClaimEvidenceAdmissionRuleV1,
)
from cruxible_core.cli.main import cli
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.registry import reset_registry
from tests.test_playbill._knowledge_loop_support import (
    PREDICATE,
    QUERY_NAME,
    SUBJECT_KIND,
    subject_shell,
    work_item_query,
)
from tests.test_playbill.test_claims import _claim_type

CREATOR_ID = "operator"
RECOVERY_ID = "recovery"
SIGNER_ID = "reviewer"
CLAIM_SOURCE_ID = "workspace.claim-status"


class _Cli:
    """Invoke the real CLI against one served daemon, as an operator would."""

    def __init__(self, runner: CliRunner, key_dir: Path) -> None:
        self._runner = runner
        self.private_key = key_dir / f"{SIGNER_ID}.ed25519"
        self.creator_private_key = key_dir / f"{CREATOR_ID}.ed25519"
        self.recovery_private_key = key_dir / f"{RECOVERY_ID}.ed25519"

    def run(self, *args: str) -> Result:
        result = self._runner.invoke(cli, list(args))
        assert result.exit_code == 0, f"cruxible {' '.join(args)}\n{result.output}"
        return result

    def json(self, *args: str) -> Any:
        return json.loads(self.run(*args, "--json").stdout)

    def accept(self, proposal_id: str) -> dict[str, Any]:
        """Activate the admitted candidate, exactly as an operator does."""

        activated = self.json("playbill", "proposal", "activate", proposal_id)
        assert activated["status"] == "accepted", activated
        return activated

    def bootstrap(self, tmp_path: Path) -> dict[str, Any]:
        """Bootstrap an optional two-ordinary mode plus recovery custody."""

        custody = tmp_path / "custody"
        recovery_custody = tmp_path / "recovery-custody"
        initialized = self.json(
            "playbill",
            "init",
            "--key-dir",
            str(custody),
            "--principal-id",
            CREATOR_ID,
            "--reviewer-key-dir",
            str(custody),
            "--recovery-key-dir",
            str(recovery_custody),
            "--recovery-principal-id",
            RECOVERY_ID,
        )
        self.recovery_private_key = recovery_custody / f"{RECOVERY_ID}.ed25519"
        return initialized


def _write(path: Path, payload: Any) -> str:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _proposal_id(inspection: dict[str, Any]) -> str:
    return str(inspection["proposal"]["admission"]["proposal_id"])


def _author_and_accept(
    cruxible: _Cli,
    path: Path,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    created = cruxible.json(
        "playbill",
        "authoring",
        "create",
        _write(path, payload),
    )
    submitted = cruxible.json(
        "playbill",
        "authoring",
        "submit",
        str(created["intent"]["intent_id"]),
    )
    accepted = cruxible.accept(str(submitted["status"]["proposal_id"]))
    return submitted, accepted


def _claim_authoring(subject_id: str, value: str) -> ClaimInput:
    """One sanctioned self-source input against accepted Claim dependencies."""

    return ClaimInput(
        kind="claim",
        subject=f"project.work_item/{subject_id}",
        predicate=_claim_type().predicate,
        object=LiteralObjectInput(kind="literal", value=value),
        role="observation",
        rationale=f"The reviewed status of {subject_id} is {value}.",
        source=WorkingSelectionInput(kind="working_selection", source_id=CLAIM_SOURCE_ID),
        citation_role="evidence",
    )


@pytest.fixture
def served_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_Cli]:
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    monkeypatch.delenv("CRUXIBLE_SERVER_TOKEN", raising=False)
    monkeypatch.delenv("CRUXIBLE_MODE", raising=False)
    monkeypatch.chdir(tmp_path)
    reset_permissions()
    reset_registry()
    get_playbill_manager().clear()

    with TestClient(create_app()) as transport:
        client = CruxibleClient(base_url="http://cruxible")
        client._client = transport  # type: ignore[assignment]
        monkeypatch.setattr(
            "cruxible_core.cli.commands._common._get_client",
            lambda: client,
        )
        yield _Cli(CliRunner(), tmp_path / "custody")

    get_playbill_manager().clear()
    reset_registry()
    reset_permissions()


def test_cli_init_key_dir_alone_bootstraps_a_solo_instance(
    served_cli: _Cli,
    tmp_path: Path,
) -> None:
    cruxible = served_cli
    host = cruxible.json("--server-url", "http://cruxible", "playbill", "host", "create")
    custody = tmp_path / "solo-custody"
    initialized = cruxible.json(
        "playbill",
        "init",
        "--key-dir",
        str(custody),
        "--principal-id",
        CREATOR_ID,
    )
    assert initialized["instance_id"] == host["instance_id"]
    assert initialized["approval_policy_mode"] == "self_approval_allowed"
    assert (custody / f"{CREATOR_ID}.ed25519").is_file()
    assert not (custody / f"{SIGNER_ID}.ed25519").exists()


def test_cli_independent_approval_flag_validates_before_provisioning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reached: list[str] = []

    def unexpected_key(*_args: object, **_kwargs: object) -> object:
        reached.append("key")
        raise AssertionError("invalid arguments must not provision custody")

    def unexpected_client() -> object:
        reached.append("server")
        raise AssertionError("invalid arguments must not contact the server")

    monkeypatch.setattr(
        "cruxible_core.cli.commands.playbill.generate_client_principal_key",
        unexpected_key,
    )
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", unexpected_client)
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://init.example.test",
            "playbill",
            "init",
            "--key-dir",
            str(tmp_path / "solo-custody"),
            "--require-independent-approval",
        ],
    )
    assert result.exit_code == 2
    assert "requires --reviewer-key-dir" in result.output
    assert not (tmp_path / "solo-custody").exists()
    assert reached == []


def test_cli_init_requires_an_active_instance_before_provisioning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    reached: list[str] = []
    monkeypatch.setattr(
        "cruxible_core.cli.commands.playbill.generate_client_principal_key",
        lambda *_args, **_kwargs: reached.append("key"),
    )
    monkeypatch.setattr("cruxible_core.cli.commands.playbill._get_client", lambda: object())

    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://init.example.test",
            "playbill",
            "init",
            "--key-dir",
            str(tmp_path.parent / "missing-instance-custody"),
        ],
    )

    assert result.exit_code == 2
    assert "--instance-id is required in server mode" in result.output
    assert reached == []


def test_cli_init_adopts_only_its_transport_bound_response_loss_orphan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CRUXIBLE_CLI_CONTEXT_PATH", str(tmp_path / "context.json"))
    custody = tmp_path.parent / "retry-custody"
    calls = 0

    class StubClient:
        def init_playbill(
            self,
            instance_id: str,
            *,
            principals: list[dict[str, object]],
            operating_profile: str,
            require_independent_approval: bool,
        ) -> contracts.PlaybillInitResult:
            nonlocal calls
            calls += 1
            assert instance_id == "inst_retry"
            assert operating_profile == "local"
            assert require_independent_approval is False
            if calls == 1:
                raise PlaybillBootstrapError("simulated response loss")
            coordinate = contracts.PlaybillAcceptedCoordinate(
                git_oid="1" * 64,
                semantic_root="sha256:" + "2" * 64,
                generation_root="sha256:" + "3" * 64,
                compiler_digest="sha256:" + "4" * 64,
            )
            return contracts.PlaybillInitResult(
                instance_id=instance_id,
                coordinate=coordinate,
                trust_root={"principals": principals},
                recovery_posture="normal",
                approval_policy_mode="self_approval_allowed",
                workspace_advertisement={"status": "not_attached", "workspace_path": None},
            )

    stub = StubClient()
    monkeypatch.setattr("cruxible_core.cli.commands.playbill._get_client", lambda: stub)
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: stub)
    args = [
        "--server-url",
        "https://init.example.test/",
        "--instance-id",
        "inst_retry",
        "playbill",
        "init",
        "--key-dir",
        str(custody),
        "--principal-id",
        CREATOR_ID,
        "--json",
    ]
    first = CliRunner().invoke(cli, args)
    assert first.exit_code == 1
    private_key = custody / f"{CREATOR_ID}.ed25519"
    marker = custody / f".playbill-init-resume-{CREATOR_ID}.json"
    original_private = private_key.read_bytes()
    assert marker.is_file()

    retry = CliRunner().invoke(cli, args)
    assert retry.exit_code == 0, retry.output
    assert private_key.read_bytes() == original_private
    assert not marker.exists()
    assert calls == 2

    refused_reuse = CliRunner().invoke(cli, args)
    assert refused_reuse.exit_code == 1
    assert "without this init's retry marker" in refused_reuse.output
    assert calls == 2


@pytest.mark.parametrize("command", ["host", "init"])
def test_tcp_in_git_worktree_refuses_before_remote_or_key_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    workspace = tmp_path / "workspace"
    subprocess.run(
        ["git", "init", "-b", "main", str(workspace)],
        check=True,
        capture_output=True,
    )
    monkeypatch.chdir(workspace)
    reached: list[str] = []
    monkeypatch.setattr(
        "cruxible_core.cli.commands.playbill._get_client",
        lambda: reached.append("client"),
    )
    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: reached.append("client"),
    )
    monkeypatch.setattr(
        "cruxible_core.cli.commands.playbill.generate_client_principal_key",
        lambda *_args, **_kwargs: reached.append("key"),
    )
    leaf = ["playbill", "host", "create"]
    if command == "init":
        leaf = [
            "--instance-id",
            "inst_tcp",
            "playbill",
            "init",
            "--key-dir",
            str(tmp_path / "outside-custody"),
        ]
    result = CliRunner().invoke(cli, ["--server-url", "https://remote.test", *leaf])

    assert result.exit_code == 2
    assert "TCP cannot attach a daemon-local workspace" in result.output
    assert "Use --server-socket" in result.output
    assert reached == []


def test_cli_drives_the_whole_knowledge_loop_on_a_served_instance(
    served_cli: _Cli,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cruxible = served_cli

    # 1. Allocate a host and bootstrap it. The host is remembered, so every
    #    later command names neither the daemon nor the instance.
    host = cruxible.json("--server-url", "http://cruxible", "playbill", "host", "create")
    assert host["status"] == "created"
    initialized = cruxible.bootstrap(tmp_path)
    assert initialized["instance_id"] == host["instance_id"]
    assert cruxible.creator_private_key.is_file()

    # 2. Seed the predicate vocabulary.
    source_contract = foreign_source_capture_contract(CLAIM_SOURCE_ID)
    claim_type = _claim_type().model_copy(
        update={
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicyV1(
                rules=(
                    ClaimEvidenceAdmissionRuleV1(
                        rule_id="coordinator-self-source",
                        claim_roles=("normative", "observation"),
                        capture_contract_digests=(capture_contract_digest(source_contract).tagged,),
                        evidence_kinds=("self_asserted",),
                        admission="direct",
                        subject_binding="exact_claim_subject",
                    ),
                )
            )
        }
    )
    proposed = cruxible.json(
        "playbill",
        "claim-type",
        "propose",
        "--envelope",
        _write(tmp_path / "claim-type.json", claim_type.model_dump(mode="json")),
        "--name",
        "seed-claim-type",
    )
    cruxible.accept(_proposal_id(proposed))

    # 3. Seed one Subject through the durable authoring coordinator.
    _author_and_accept(
        cruxible,
        tmp_path / "subject-input.json",
        SubjectInput(
            kind="subject",
            subject=subject_shell("wi-42"),
        ).model_dump(mode="json"),
    )

    # 4. Two Claims through the durable AuthoringIntent coordinator. The second
    #    Subject is admitted first because tagless Claim input resolves accepted
    #    dependencies instead of carrying a private direct-writer closure.
    claim_identities: list[str] = []
    for subject_id, value in (("wi-42", "ready"), ("wi-43", "blocked")):
        if subject_id == "wi-43":
            _author_and_accept(
                cruxible,
                tmp_path / "subject-wi-43-input.json",
                SubjectInput(
                    kind="subject",
                    subject=subject_shell("wi-43"),
                ).model_dump(mode="json"),
            )
        payload_file = _write(
            tmp_path / f"claim-{subject_id}.json",
            _claim_authoring(subject_id, value).model_dump(mode="json"),
        )
        source_file = tmp_path / f"claim-{subject_id}.md"
        source_file.write_text(f"status: {value}\n", encoding="utf-8")
        bound = cruxible.json(
            "playbill",
            "authoring",
            "bind",
            "--file",
            str(source_file),
            "--anchor",
            f"status: {value}",
            "--payload-file",
            payload_file,
        )
        intent_id = str(bound["certificate"]["intent_id"])
        if subject_id == "wi-42":
            brief = cruxible.run(
                "playbill",
                "authoring",
                "submit",
                intent_id,
                "--and-activate",
                "--brief",
            ).stdout
            assert "outcome: accepted" in brief
            assert "coordinate: " in brief
            assert "receipt: playbill-activation-receipt-v1" in brief
            intent = cruxible.json("playbill", "authoring", "get", intent_id)["intent"]
            claim_identities.append(f"Claim:{intent['semantic_identity']}")
        else:
            submitted = cruxible.json(
                "playbill",
                "authoring",
                "submit",
                intent_id,
            )
            claim_identities.append(f"Claim:{submitted['intent']['semantic_identity']}")
            cruxible.accept(str(submitted["status"]["proposal_id"]))

    # 5. Publish the named entrypoint that reads them.
    _submitted_query, accepted = _author_and_accept(
        cruxible,
        tmp_path / "query-input.json",
        QueryDefinitionInput(
            kind="query_definition",
            query_definition=work_item_query(claim_type=claim_type),
        ).model_dump(mode="json"),
    )
    coordinate = accepted["accepted_coordinate"]

    # -- reads ------------------------------------------------------------

    subjects = cruxible.json("playbill", "subject", "list")
    assert {item["envelope"]["identity"] for item in subjects["subjects"]} == {
        f"Subject:{SUBJECT_KIND}/wi-42",
        f"Subject:{SUBJECT_KIND}/wi-43",
    }
    assert subjects["coordinate"] == coordinate
    assert (
        cruxible.json("playbill", "subject", "get", SUBJECT_KIND, "wi-42")["envelope"]["identity"]
        == f"Subject:{SUBJECT_KIND}/wi-42"
    )
    assert cruxible.json("playbill", "subject", "history", SUBJECT_KIND, "wi-42")["entries"]

    claim_types = cruxible.json("playbill", "claim-type", "list")
    assert [item["predicate"] for item in claim_types["claim_types"]] == [PREDICATE]
    assert cruxible.json("playbill", "claim-type", "get", PREDICATE)["predicate"] == PREDICATE

    claims = cruxible.json("playbill", "claim", "list", "--predicate", PREDICATE)
    assert {item["envelope"]["identity"] for item in claims["claims"]} == set(claim_identities)
    assert (
        cruxible.json("playbill", "claim", "get", claim_identities[0])["envelope"]["identity"]
        == claim_identities[0]
    )
    assert cruxible.json("playbill", "claim", "history", claim_identities[0])["entries"]
    explained = cruxible.json("playbill", "claim", "explain", claim_identities[0])
    assert explained["verdict"]["verdict"] == "supported", explained
    assert explained["law_evidence"]

    definitions = cruxible.json("playbill", "query", "list")
    assert [item["name"] for item in definitions["query_definitions"]] == [QUERY_NAME]
    assert cruxible.json("playbill", "query", "get", QUERY_NAME)["name"] == QUERY_NAME

    # 6. Execute the entrypoint. The receipt is the replay coordinate of the
    #    read, and it is surfaced in both output modes.
    run = cruxible.json("playbill", "query", "run", QUERY_NAME)
    assert run["result"]["verdict"] == "completed"
    projected = {
        next(field["value"] for field in row["fields"] if field["name"] == "item_id"): next(
            field["value"] for field in row["fields"] if field["name"] == "status"
        )
        for row in run["result"]["rows"]
    }
    assert projected == {"wi-42": "ready", "wi-43": "blocked"}
    receipt = run["receipt"]
    assert receipt["tag"] == "playbill-query-execution-receipt-v1"
    assert receipt["verdict"] == "completed"
    assert receipt["result_digest"].startswith("sha256:")
    assert receipt["definition_digest"] == run["definition_digest"]
    # The daemon opens no journal for this read; PC-G owns that seam.
    assert run["journal_record_digest"] is None

    # Replaying at the receipt's own evaluation time reproduces it exactly, and
    # the human rendering names the same receipt the JSON does.
    human = cruxible.run(
        "playbill",
        "query",
        "run",
        QUERY_NAME,
        "--evaluation-time",
        receipt["evaluation_time"],
    ).stdout
    assert f"Receipt result digest: {receipt['result_digest']}" in human
    assert f"Receipt definition: {receipt['definition_digest']}" in human
    assert f"Receipt parameters: {receipt['parameter_digest']}" in human
    assert f"{QUERY_NAME}: completed with 2 row(s)" in human

    # 7. Discovery and bounded expansion over the same accepted coordinate.
    page = cruxible.json("playbill", "discover", "--query", "wi-42", "--profile", "all")
    assert page["vocabulary_entry_count"] > 0
    assert any(
        hit["address"]["artifact_path"] == f"subjects/{SUBJECT_KIND}/wi-42.json"
        for hit in page["page"]["hits"]
    )

    capsule = cruxible.json("playbill", "expand", f"subjects/{SUBJECT_KIND}/wi-42.json")
    assert capsule["tag"] == "playbill-context-capsule-v1"
    assert capsule["at"] == coordinate
    assert capsule["canonical_summary"]["identity"] == f"Subject:{SUBJECT_KIND}/wi-42"

    # 8. Materialize the floor and prove it carries the accepted facts bound to
    #    the coordinate they were projected from.
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subdirectory = tmp_path / "nested"
    subdirectory.mkdir()
    monkeypatch.chdir(subdirectory)
    floor = tmp_path / ".playbill/floor"
    exported = cruxible.json("playbill", "floor", "export")
    manifest = json.loads((floor / "manifest.json").read_text(encoding="utf-8"))
    assert manifest == exported
    assert manifest["coordinate"] == coordinate
    assert manifest["floor_digest"].startswith("sha256:")

    written = {str(path.relative_to(floor)) for path in floor.rglob("*") if path.is_file()} - {
        "manifest.json"
    }
    assert written == {item["path"] for item in manifest["files"]}
    assert f"subjects/{SUBJECT_KIND}/wi-42.profile.json" in written
    assert f"subjects/{SUBJECT_KIND}/wi-43.profile.json" in written
    assert "claim-types/project.work_item/status.card.json" in written

    profile = json.loads(
        (floor / f"subjects/{SUBJECT_KIND}/wi-42.profile.json").read_text(encoding="utf-8")
    )
    assert PREDICATE in json.dumps(profile)
    assert "ready" in json.dumps(profile)


def test_cli_floor_export_refuses_to_overwrite_a_non_empty_directory(
    served_cli: _Cli,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cruxible = served_cli
    cruxible.json("--server-url", "http://cruxible", "playbill", "host", "create")
    cruxible.bootstrap(tmp_path)

    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    monkeypatch.chdir(tmp_path)
    floor = tmp_path / ".playbill/floor"
    floor.mkdir(parents=True)
    (floor / "occupied.txt").write_text("not the floor\n", encoding="utf-8")

    refused = CliRunner().invoke(cli, ["playbill", "floor", "export", "--json"])

    assert refused.exit_code != 0
    assert "refusing to write the floor into a non-empty directory" in refused.output
    assert (floor / "occupied.txt").read_text(encoding="utf-8") == "not the floor\n"
