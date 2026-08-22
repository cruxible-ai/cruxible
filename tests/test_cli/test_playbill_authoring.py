"""CLI authoring adapters keep payloads local and machine identity opaque."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from cruxible_client import contracts
from cruxible_core.cli.main import cli
from cruxible_core.playbill.authoring.examples import claim_self_source_example
from cruxible_core.playbill.seed import (
    plan_seed_bundle,
    seed_group_operation_digest,
    seed_group_proposal_name,
)

COORDINATE = contracts.PlaybillAcceptedCoordinate(
    git_oid="1" * 64,
    semantic_root="sha256:" + "2" * 64,
    generation_root="sha256:" + "3" * 64,
    compiler_digest="sha256:" + "4" * 64,
)
INTENT_ID = "AIT-" + "5" * 32
OBSERVATION = {
    "tag": "playbill-insertion-confirmation-observation-v1",
    "expectation_id": "sha256:" + "6" * 64,
}
SEED_EXAMPLE = Path(__file__).resolve().parents[2] / "benchmarks/playbill_taubench/seed-example"


class _SeedSubmission:
    def __init__(self, *, proposal_id: str, target_ref: str) -> None:
        self.payload = {
            "proposal": {
                "proposal": {
                    "admission": {
                        "proposal_id": proposal_id,
                        "target_ref": target_ref,
                    }
                }
            }
        }

    def model_dump(self, *, mode: str) -> dict[str, Any]:
        assert mode == "json"
        return self.payload


def test_cli_compile_reads_payload_and_submit_uses_only_opaque_intent(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    payload = tmp_path / "claim.json"
    authoring = claim_self_source_example().model_dump(mode="json")
    payload.write_text(json.dumps(authoring))
    calls: list[tuple[str, object]] = []

    class StubClient:
        def compile_playbill_authoring(
            self,
            instance_id: str,
            *,
            payload: dict[str, object],
            intent_id: str | None,
        ) -> contracts.PlaybillAuthoringPreflightResult:
            calls.append((instance_id, payload))
            assert intent_id is None
            return contracts.PlaybillAuthoringPreflightResult(
                verdict="refused",
                certificate={"certificate_digest": "sha256:" + "6" * 64},
                frontier={"diagnostics": []},
            )

        def submit_playbill_authoring_intent(
            self, instance_id: str, intent_id: str
        ) -> contracts.PlaybillAuthoringSubmitResult:
            calls.append((instance_id, intent_id))
            status = contracts.PlaybillCandidateStatus(
                state="draft",
                current_accepted_coordinate=COORDINATE,
            )
            return contracts.PlaybillAuthoringSubmitResult(
                intent={"intent_id": intent_id},
                status=status,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    runner = CliRunner()
    common = [
        "--server-url",
        "https://authoring.example.test",
        "--instance-id",
        "inst_authoring",
        "playbill",
        "authoring",
    ]
    compiled = runner.invoke(cli, [*common, "compile", str(payload), "--json"])
    submitted = runner.invoke(cli, [*common, "submit", INTENT_ID, "--json"])

    assert compiled.exit_code == 0, compiled.output
    assert submitted.exit_code == 0, submitted.output
    assert calls == [
        ("inst_authoring", authoring),
        ("inst_authoring", INTENT_ID),
    ]
    assert "target: inst_authoring @ https://authoring.example.test (explicit)" in compiled.stderr
    assert INTENT_ID in submitted.output


def test_cli_status_is_a_read_and_emits_no_write_target(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class StubClient:
        def playbill_authoring_intent_status(
            self, instance_id: str, intent_id: str
        ) -> contracts.PlaybillCandidateStatus:
            assert (instance_id, intent_id) == ("inst_authoring", INTENT_ID)
            return contracts.PlaybillCandidateStatus(
                state="draft",
                current_accepted_coordinate=COORDINATE,
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://authoring.example.test",
            "--instance-id",
            "inst_authoring",
            "playbill",
            "authoring",
            "status",
            INTENT_ID,
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert result.stderr == ""


def test_cli_whoami_explains_credential_binding_and_lists_open_proposals(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    calls: list[str] = []

    class StubClient:
        def playbill_whoami(self, instance_id: str) -> contracts.PlaybillWhoAmI:
            calls.append(f"whoami:{instance_id}")
            return contracts.PlaybillWhoAmI(
                actor_id="owner",
                credential_label="owner",
                actor_id_source="runtime_credential_label",
                credential_permission_mode="governed_write",
                principal_registration_status="active",
                active_principal_ids=["daemon", "owner"],
                coordinate=COORDINATE,
            )

        def list_playbill_proposals(
            self, instance_id: str, *, status: str | None
        ) -> contracts.PlaybillProposalList:
            calls.append(f"proposals:{instance_id}:{status}")
            return contracts.PlaybillProposalList(
                coordinate=COORDINATE,
                status_filter="open",
                entries=[
                    contracts.PlaybillProposalListEntry(
                        proposal_id="sha256:" + "5" * 64,
                        actor_id="owner",
                        target_ref="refs/proposals/owner/example",
                        admitted_at="2026-08-21T12:00:00.000000Z",
                        verdict="candidate",
                        candidate_digest="sha256:" + "6" * 64,
                        status="open",
                    )
                ],
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    base = [
        "--server-url",
        "https://authoring.example.test",
        "--instance-id",
        "inst_authoring",
        "playbill",
    ]
    runner = CliRunner()
    identity = runner.invoke(cli, [*base, "whoami"])
    proposals = runner.invoke(cli, [*base, "proposal", "list", "--status", "open"])

    assert identity.exit_code == proposals.exit_code == 0
    assert "Actor ID comes from credential label: owner" in identity.output
    assert "governed_write" in identity.output
    assert "open  sha256:" in proposals.output
    assert calls == ["whoami:inst_authoring", "proposals:inst_authoring:open"]


def test_cli_insertion_confirm_and_abandon_use_the_opaque_intent(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    observation = tmp_path / "observation.json"
    observation.write_text(json.dumps(OBSERVATION))
    calls: list[tuple[str, object]] = []

    class StubClient:
        def confirm_playbill_authoring_insertion(
            self,
            instance_id: str,
            intent_id: str,
            *,
            observation: dict[str, object],
        ) -> contracts.PlaybillInsertionConfirmResult:
            calls.append((intent_id, observation))
            return contracts.PlaybillInsertionConfirmResult(
                outcome="stale_target",
                intent={"intent_id": intent_id},
                expectation={"state": "pending"},
            )

        def abandon_playbill_authoring_insertion(
            self,
            instance_id: str,
            intent_id: str,
        ) -> contracts.PlaybillInsertionAbandonResult:
            calls.append((intent_id, "abandon"))
            return contracts.PlaybillInsertionAbandonResult(
                intent={"intent_id": intent_id},
                expectation={"state": "abandoned"},
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    common = [
        "--server-url",
        "https://authoring.example.test",
        "--instance-id",
        "inst_authoring",
        "playbill",
        "authoring",
    ]
    runner = CliRunner()
    confirmed = runner.invoke(
        cli,
        [*common, "confirm-insertion", INTENT_ID, str(observation), "--json"],
    )
    abandoned = runner.invoke(cli, [*common, "abandon-insertion", INTENT_ID, "--json"])

    assert confirmed.exit_code == abandoned.exit_code == 0
    assert calls == [(INTENT_ID, OBSERVATION), (INTENT_ID, "abandon")]


def test_cli_create_examples_are_model_generated_and_need_no_daemon() -> None:
    runner = CliRunner()
    help_result = runner.invoke(cli, ["playbill", "authoring", "create", "--help"])
    assert help_result.exit_code == 0
    assert "playbill-claim-authoring-payload-v1" in help_result.output
    assert "playbill-procedure-authoring-payload-v1" in help_result.output

    for name in ("claim-flow-a", "claim-self-source", "procedure", "brief"):
        result = runner.invoke(cli, ["playbill", "authoring", "create", "--example", name])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["tag"] in {
            "playbill-claim-authoring-payload-v1",
            "playbill-procedure-authoring-payload-v1",
        }
        assert result.stderr == ""


def test_cli_validation_names_field_path_and_matching_example(tmp_path: Path) -> None:
    payload = tmp_path / "invalid.json"
    payload.write_text(
        json.dumps(
            {
                "tag": "playbill-claim-authoring-payload-v1",
                "statement": {},
                "source": {"tag": "playbill-self-source-body-v1"},
            }
        )
    )

    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://authoring.example.test",
            "--instance-id",
            "inst_authoring",
            "playbill",
            "authoring",
            "create",
            str(payload),
        ],
    )

    assert result.exit_code == 1
    assert "$.statement.subject" in result.output
    assert "playbill authoring create --example claim-self-source" in result.output


def test_cli_bind_derives_observation_and_compiles(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    source = tmp_path / "work-items.md"
    source.write_bytes(b"before\nstatus: ready\nafter\n")
    stub = claim_self_source_example().model_dump(mode="json")
    stub["source"] = {
        "tag": "playbill-working-selection-observation-v1",
        "source_id": "repo.work-items",
    }
    stub["citation_role"] = "evidence"
    payload_file = tmp_path / "stub.json"
    payload_file.write_text(json.dumps(stub))
    calls: list[dict[str, object]] = []

    class StubClient:
        def compile_playbill_authoring(
            self,
            instance_id: str,
            *,
            payload: dict[str, object],
            intent_id: str | None,
        ) -> contracts.PlaybillAuthoringPreflightResult:
            assert (instance_id, intent_id) == ("inst_authoring", None)
            calls.append(payload)
            return contracts.PlaybillAuthoringPreflightResult(
                verdict="passed",
                certificate={"certificate_digest": "sha256:" + "6" * 64},
                frontier={"diagnostics": []},
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://authoring.example.test",
            "--instance-id",
            "inst_authoring",
            "playbill",
            "authoring",
            "bind",
            "--file",
            str(source),
            "--anchor",
            "status: ready",
            "--payload-file",
            str(payload_file),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    observation = calls[0]["source"]
    assert isinstance(observation, dict)
    assert observation["coordinate"]["source_content_digest"] == (
        "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    )


def test_cli_bind_ambiguity_reports_candidate_offsets_without_calling_daemon(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    source = tmp_path / "ambiguous.txt"
    source.write_text("aaa")
    stub = claim_self_source_example().model_dump(mode="json")
    stub["source"] = {
        "tag": "playbill-working-selection-observation-v1",
        "source_id": "repo.work-items",
    }
    stub["citation_role"] = "evidence"
    payload_file = tmp_path / "stub.json"
    payload_file.write_text(json.dumps(stub))
    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: (_ for _ in ()).throw(AssertionError("daemon must not be called")),
    )

    result = CliRunner().invoke(
        cli,
        [
            "playbill",
            "authoring",
            "bind",
            "--file",
            str(source),
            "--anchor",
            "aa",
            "--payload-file",
            str(payload_file),
        ],
    )

    assert result.exit_code == 1
    assert "playbill.authoring.anchor_ambiguous" in result.output
    assert '"candidate_byte_offsets":[0,1]' in result.output


def test_direct_claim_propose_help_and_invocation_route_to_the_coordinator(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    authoring = tmp_path / "legacy.json"
    authoring.write_text("{}")

    class Result:
        @staticmethod
        def model_dump(*, mode: str) -> dict[str, object]:
            assert mode == "json"
            return {"ok": True}

    monkeypatch.setattr(
        "cruxible_core.cli.commands.playbill._server_call",
        lambda operation, *, command_name: Result(),
    )
    runner = CliRunner()
    help_result = runner.invoke(cli, ["playbill", "claim", "propose", "--help"])
    invoked = runner.invoke(
        cli,
        [
            "--server-url",
            "https://authoring.example.test",
            "--instance-id",
            "inst_authoring",
            "playbill",
            "claim",
            "propose",
            "--authoring",
            str(authoring),
            "--name",
            "legacy",
            "--json",
        ],
    )

    assert help_result.exit_code == 0
    assert "Legacy-wire path" in help_result.output
    assert "sanctioned authoring coordinator" in help_result.output
    assert invoked.exit_code == 0, invoked.output
    assert "playbill.claim.propose.legacy_wire_deprecated" in invoked.stderr
    assert "playbill authoring create/compile" in invoked.stderr


def test_seed_apply_reuses_machine_identity_and_blocks_only_an_open_retry(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    files = {
        path.relative_to(SEED_EXAMPLE).as_posix(): path.read_bytes()
        for path in sorted(SEED_EXAMPLE.rglob("*"))
        if path.is_file()
    }
    plan = plan_seed_bundle(files, proposal_name="first-human-label")
    group = plan.group("claims")
    expected_name = seed_group_proposal_name(plan, group)
    expected_ref = f"refs/proposals/owner/{expected_name}"
    proposal_id = "sha256:" + "7" * 64

    class StubClient:
        state = "absent"
        proposal_names: list[str] = []
        stored_bodies = 0

        def playbill_whoami(self, instance_id: str) -> contracts.PlaybillWhoAmI:
            assert instance_id == "inst_authoring"
            return contracts.PlaybillWhoAmI(
                actor_id="owner",
                credential_label="owner",
                actor_id_source="runtime_credential_label",
                credential_permission_mode="governed_write",
                principal_registration_status="active",
                active_principal_ids=["owner"],
                coordinate=COORDINATE,
            )

        def list_playbill_proposals(
            self, instance_id: str, *, status: str | None
        ) -> contracts.PlaybillProposalList:
            assert (instance_id, status) == ("inst_authoring", "open")
            entries = []
            if self.state == "open":
                entries.append(
                    contracts.PlaybillProposalListEntry(
                        proposal_id=proposal_id,
                        actor_id="owner",
                        target_ref=expected_ref,
                        admitted_at="2026-08-21T12:00:00.000000Z",
                        verdict="candidate",
                        candidate_digest="sha256:" + "8" * 64,
                        status="open",
                    )
                )
            return contracts.PlaybillProposalList(
                coordinate=COORDINATE,
                status_filter="open",
                entries=entries,
            )

        def store_playbill_body(self, instance_id: str, content: bytes) -> object:
            assert instance_id == "inst_authoring"
            assert content
            self.stored_bodies += 1
            return object()

        def propose_playbill_claims(
            self,
            instance_id: str,
            *,
            authorings: list[dict[str, object]],
            proposal_name: str,
        ) -> _SeedSubmission:
            assert instance_id == "inst_authoring"
            assert authorings
            self.proposal_names.append(proposal_name)
            self.state = "open"
            return _SeedSubmission(proposal_id=proposal_id, target_ref=expected_ref)

    client = StubClient()
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: client)
    runner = CliRunner()

    def invoke(label: str):  # type: ignore[no-untyped-def]
        return runner.invoke(
            cli,
            [
                "--server-url",
                "https://authoring.example.test",
                "--instance-id",
                "inst_authoring",
                "playbill",
                "seed",
                "apply",
                str(SEED_EXAMPLE),
                "--name",
                label,
                "--group",
                "claims",
                "--json",
            ],
        )

    first = invoke("first-human-label")
    stored_after_first = client.stored_bodies
    retry = invoke("different-presentation-label")
    client.state = "stale"
    after_head_advance = invoke("third-human-label")

    assert first.exit_code == 0, first.output
    first_payload = json.loads(first.stdout)
    assert first_payload["proposal_name"] == "first-human-label"
    assert first_payload["target_ref"] == expected_ref
    assert first_payload["operation_digest"] == seed_group_operation_digest(plan, group).tagged
    assert retry.exit_code == 1
    assert proposal_id in retry.output
    assert expected_ref in retry.output
    assert client.stored_bodies == stored_after_first * 2
    assert after_head_advance.exit_code == 0, after_head_advance.output
    assert client.proposal_names == [expected_name, expected_name]
