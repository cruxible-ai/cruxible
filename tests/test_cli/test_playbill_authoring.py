"""CLI authoring adapters keep payloads local and machine identity opaque."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from cruxible_client import CruxibleClient, contracts
from cruxible_client.authoring.blocks import render_projection_opening
from cruxible_client.authoring.examples import claim_flow_a_example, claim_self_source_example
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.declared_blocks import (
    ProjectionBlockStampV1,
    ProjectionClaimBackingV1,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.cli.main import cli
from cruxible_core.playbill.keys import generate_client_principal_key
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.registry import get_registry, reset_registry
from tests.test_client.test_playbill_authoring import OBSERVATION
from tests.test_playbill._claim_type_support import claim_type_input_example

COORDINATE = contracts.PlaybillAcceptedCoordinate(
    git_oid="1" * 64,
    semantic_root="sha256:" + "2" * 64,
    generation_root="sha256:" + "3" * 64,
    compiler_digest="sha256:" + "4" * 64,
)
INTENT_ID = "AIT-" + "5" * 32


def test_cli_compile_reads_payload_and_submit_uses_only_opaque_intent(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    payload = tmp_path / "claim.json"
    authoring = claim_self_source_example().model_dump(mode="json")
    payload.write_text(json.dumps(authoring))
    calls: list[tuple[str, object]] = []

    class StubClient:
        def compile_playbill_authoring_input(
            self,
            instance_id: str,
            *,
            input: dict[str, object],
            intent_id: str | None,
        ) -> contracts.PlaybillAuthoringPreflightResult:
            calls.append((instance_id, input))
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


def test_cli_claim_type_propose_delivers_nonblocking_source_lint(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    payload = tmp_path / "claim-type.json"
    values = {
        **claim_type_input_example().model_dump(mode="json"),
        "anticipated_source_ids": ["corpus.runbook"],
    }
    payload.write_text(json.dumps(values))
    warning = {
        "code": "playbill.claim_type.anticipated_source_contract_omitted",
        "field_path": "$.evidence_admission_policy.rules",
        "source_id": "corpus.runbook",
        "contract_identity": "CaptureContract:playbill.foreign-source.corpus.runbook",
        "contract_digest": "sha256:" + "7" * 64,
        "replacement_rule_fragment": {"capture_contract_digests": ["sha256:" + "7" * 64]},
    }

    class StubClient:
        def propose_playbill_claim_type_input(
            self,
            instance_id: str,
            *,
            input: dict[str, object],
            proposal_name: str,
        ) -> contracts.PlaybillClaimTypeInputProposalResult:
            assert (instance_id, proposal_name) == (
                "inst_authoring",
                "project.work_item.replace_me",
            )
            assert input["anticipated_source_ids"] == ["corpus.runbook"]
            return contracts.PlaybillClaimTypeInputProposalResult(
                proposal=contracts.PlaybillProposalInspection(
                    proposal={"proposal_id": "sha256:" + "8" * 64},
                    accepted_coordinate=COORDINATE,
                ),
                lint=contracts.PlaybillClaimTypeProposalLint(warnings=[warning]),
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
            "claim-type",
            "propose",
            "--input",
            str(payload),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["lint"]["warnings"] == [warning]


def test_cli_examples_are_supported_and_schema_discoverable() -> None:
    runner = CliRunner()

    claim_type_help = runner.invoke(cli, ["playbill", "claim-type", "propose", "--help"])
    claim_type_example = runner.invoke(cli, ["playbill", "claim-type", "propose", "--example"])
    claim_type_missing = runner.invoke(cli, ["playbill", "claim-type", "propose"])
    retirement = runner.invoke(cli, ["playbill", "claim", "retire", "--example"])
    create_help = runner.invoke(cli, ["playbill", "authoring", "create", "--help"])

    assert claim_type_help.exit_code == 0, claim_type_help.output
    assert "--example" not in claim_type_help.output
    assert claim_type_example.exit_code == 2
    assert "No such option: --example" in claim_type_example.output
    assert claim_type_missing.exit_code == 2
    assert "provide exactly one ClaimType input with --input" in claim_type_missing.output

    assert retirement.exit_code == 0, retirement.output
    retirement_payload = json.loads(retirement.stdout)
    assert retirement_payload["tag"] == "playbill-claim-retire-request-v1"
    assert retirement_payload["mode"] == "preflight"
    assert retirement_payload["expected_coordinate"]["tag"] == ("playbill-accepted-coordinate-v1")

    assert create_help.exit_code == 0, create_help.output
    assert "PAYLOAD_FILE" in create_help.output


def test_cli_refused_stale_preflight_teaches_rebase_not_resume(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    old_coordinate = COORDINATE.model_copy(update={"git_oid": "a" * 40})

    class StubClient:
        def preflight_playbill_authoring_intent(
            self, _instance_id: str, _intent_id: str
        ) -> contracts.PlaybillAuthoringPreflightResult:
            return contracts.PlaybillAuthoringPreflightResult(
                verdict="refused",
                certificate={
                    "accepted_coordinate": COORDINATE.model_dump(mode="json"),
                },
                frontier={"diagnostics": []},
            )

        def get_playbill_authoring_intent(
            self, _instance_id: str, _intent_id: str
        ) -> contracts.PlaybillAuthoringIntentView:
            return contracts.PlaybillAuthoringIntentView(
                intent={"base_coordinate": old_coordinate.model_dump(mode="json")}
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
            "preflight",
            INTENT_ID,
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert f"playbill authoring rebase {INTENT_ID}" in result.stderr
    assert "resume does not advance" in result.stderr


def test_cli_claim_type_migration_delivers_nonblocking_source_lint(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    payload = tmp_path / "migration.json"
    payload.write_text(
        json.dumps(
            {
                "tag": "playbill-claim-type-migration-request-v2",
                "mode": "preflight",
                "successor": claim_type_input_example().model_dump(mode="json"),
            }
        )
    )
    warning = {
        "code": "playbill.claim_type.evidence_policy_admits_no_accepted_contract",
        "field_path": "$.evidence_admission_policy.rules",
        "source_id": None,
        "contract_identity": "CaptureContract:available",
        "contract_digest": "sha256:" + "7" * 64,
        "replacement_rule_fragment": {"capture_contract_digests": ["sha256:" + "7" * 64]},
    }

    class StubClient:
        def migrate_playbill_claim_type(
            self,
            instance_id: str,
            *,
            request: dict[str, object],
        ) -> contracts.PlaybillClaimTypeMigrationPreflight:
            assert instance_id == "inst_authoring"
            assert request["mode"] == "preflight"
            return contracts.PlaybillClaimTypeMigrationPreflight(
                coordinate=COORDINATE,
                successor_artifact_digest="sha256:" + "8" * 64,
                dependents=[],
                lint=contracts.PlaybillClaimTypeProposalLint(warnings=[warning]),
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
            "claim-type",
            "migrate",
            str(payload),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["lint"]["warnings"] == [warning]


def test_cli_claim_retire_passes_the_model_validated_request(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    claim_id = "CLM-0123456789abcdef0123456789abcdef"
    payload = tmp_path / "retire.json"
    payload.write_text(
        json.dumps(
            {
                "tag": "playbill-claim-retire-request-v1",
                "mode": "preflight",
                "claim_ref": f"Claim:{claim_id}",
                "reason": "was-rescinded",
                "effective_until": None,
                "expected_coordinate": COORDINATE.model_dump(mode="json"),
                "dependents": [],
            }
        )
    )

    class StubClient:
        def retire_playbill_claim(
            self,
            instance_id: str,
            selected_claim_id: str,
            *,
            request: dict[str, object],
        ) -> contracts.PlaybillClaimRetireResponse:
            assert (instance_id, selected_claim_id) == ("inst_authoring", claim_id)
            assert request["reason"] == "was-rescinded"
            return contracts.PlaybillClaimRetirePreflight(
                operation_digest="sha256:" + "8" * 64,
                coordinate=COORDINATE,
                root_identity={"kind": "Claim", "name": claim_id},
                root_predecessor_digest="sha256:" + "9" * 64,
                reason="was-rescinded",
                effective_until=None,
                required_dependents=[],
                diagnostics=[],
                submit_ready=True,
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
            "claim",
            "retire",
            claim_id,
            str(payload),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["submit_ready"] is True


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
        def prepare_playbill_authoring_publication(
            self,
            instance_id: str,
            intent_id: str,
            *,
            observation: dict[str, object],
        ) -> contracts.PlaybillInsertionPrepareResult:
            calls.append((intent_id, observation))
            return contracts.PlaybillInsertionPrepareResult(
                tag="playbill-insertion-prepare-result-v2",
                outcome="prepared",
                intent={"intent_id": intent_id},
                expectation={"state": "prepared"},
                preparation={"preparation_digest": "sha256:" + "7" * 64},
                warnings=[
                    contracts.PlaybillPublicationPrepareWarning(
                        tag="playbill-publication-prepare-warning-v1",
                        code="playbill.authoring.publication_citation_anchor_collision",
                        source_id="repo.work-items",
                        citation_ids=["sha256:" + "8" * 64],
                    )
                ],
            )

        def confirm_playbill_authoring_insertion(
            self,
            instance_id: str,
            intent_id: str,
            *,
            observation: dict[str, object],
        ) -> contracts.PlaybillInsertionConfirmResultV2:
            calls.append((intent_id, observation))
            return contracts.PlaybillInsertionConfirmResultV2(
                tag="playbill-insertion-confirm-result-v2",
                outcome="bound",
                intent={"intent_id": intent_id},
                expectation={"state": "bound"},
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
    prepared = runner.invoke(
        cli,
        [*common, "prepare-publication", INTENT_ID, str(observation), "--json"],
    )
    abandoned = runner.invoke(cli, [*common, "abandon-insertion", INTENT_ID, "--json"])

    assert confirmed.exit_code == prepared.exit_code == abandoned.exit_code == 0
    assert json.loads(prepared.stdout)["warnings"][0]["citation_ids"] == ["sha256:" + "8" * 64]
    assert calls == [
        (INTENT_ID, OBSERVATION),
        (INTENT_ID, OBSERVATION),
        (INTENT_ID, "abandon"),
    ]


def test_cli_create_examples_are_model_generated_and_need_no_daemon() -> None:
    runner = CliRunner()
    help_result = runner.invoke(cli, ["playbill", "authoring", "create", "--help"])
    assert help_result.exit_code == 0
    assert "procedure_runtime_policy" in help_result.output

    for name in (
        "claim-existing-capture",
        "claim-flow-a",
        "claim-self-source",
        "procedure",
        "subject",
        "approval-policy",
        "procedure-runtime-policy",
        "query-claims-by-type",
    ):
        result = runner.invoke(cli, ["playbill", "authoring", "create", "--example", name])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["kind"] in {
            "claim",
            "procedure",
            "subject",
            "approval_policy",
            "procedure_runtime_policy",
            "query_definition",
        }
        assert "tag" not in payload
        if name == "procedure":
            assert [node["spec"]["tag"] for node in payload["definition"]["nodes"]] == [
                "playbill-transform-adapter-spec-v1",
                "playbill-transform-shape-items-spec-v1",
                "playbill-transform-filter-items-spec-v1",
                "playbill-transform-dedupe-items-spec-v1",
                "playbill-transform-join-items-spec-v1",
                "playbill-transform-aggregate-items-spec-v1",
            ]
        assert result.stderr == ""


def test_subject_propose_is_a_typed_deprecation_shim(tmp_path: Path) -> None:
    envelope = tmp_path / "subject.json"
    envelope.write_text("{}\n")
    result = CliRunner().invoke(
        cli,
        [
            "playbill",
            "subject",
            "propose",
            "--envelope",
            str(envelope),
            "--name",
            "old-path",
        ],
    )

    assert result.exit_code != 0
    assert "playbill.write_surface_deprecated" in result.output
    assert "authoring coordinator with payload kind 'subject'" in result.output
    assert "target:" not in result.output


def test_propose_help_distinguishes_coordinator_shims_from_sanctioned_paths() -> None:
    runner = CliRunner()
    subject = runner.invoke(cli, ["playbill", "subject", "propose", "--help"])
    query = runner.invoke(cli, ["playbill", "query", "propose", "--help"])
    document = runner.invoke(cli, ["playbill", "document", "propose", "--help"])
    claim_type = runner.invoke(cli, ["playbill", "claim-type", "propose", "--help"])

    for shim in (subject, query):
        assert shim.exit_code == 0
        assert "Deprecated" in shim.output
        assert "playbill authoring create" in shim.output
        assert "authoring submit" in shim.output
        assert "``" not in shim.output
        assert "--envelope FILE" in shim.output
        assert "Deprecated and ignored by this compatibility shim" in " ".join(shim.output.split())
        assert "--envelope FILE  [required]" not in shim.output
    assert document.exit_code == 0
    assert "sanctioned command-local Document proposal path" in document.output
    assert "Deprecated" not in document.output
    assert claim_type.exit_code == 0
    assert "sanctioned typed-input ClaimType proposal path" in claim_type.output
    assert "Deprecated" not in claim_type.output

    bare_subject = runner.invoke(cli, ["playbill", "subject", "propose"])
    assert bare_subject.exit_code != 0
    assert "playbill.write_surface_deprecated" in bare_subject.output


@pytest.mark.parametrize(
    "arguments",
    [
        ["--example", "claim-cite-supporting-evidence"],
        [
            "--example",
            "claim-cite-supporting-evidence",
            "--claim-id",
            "CLM-" + "a" * 32,
        ],
        [
            "--example",
            "claim-cite-supporting-evidence",
            "--capture-digest",
            "sha256:" + "b" * 64,
        ],
        ["--example", "claim-flow-a", "--claim-id", "CLM-" + "a" * 32],
        ["--example", "claim-flow-a", "--capture-digest", "sha256:" + "b" * 64],
        [
            "--example",
            "claim-flow-a",
            "--claim-id",
            "CLM-" + "a" * 32,
            "--capture-digest",
            "sha256:" + "b" * 64,
        ],
    ],
)
def test_cli_attestation_door_example_options_refuse_incomplete_or_wrong_hints(
    arguments: list[str],
) -> None:
    result = CliRunner().invoke(cli, ["playbill", "authoring", "create", *arguments])
    assert result.exit_code == 2


def test_cli_attestation_door_example_accepts_both_hints() -> None:
    result = CliRunner().invoke(
        cli,
        [
            "playbill",
            "authoring",
            "create",
            "--example",
            "claim-cite-supporting-evidence",
            "--claim-id",
            "CLM-" + "a" * 32,
            "--capture-digest",
            "sha256:" + "b" * 64,
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["claim_id"] == "CLM-" + "a" * 32
    assert payload["source"]["capture_digest"] == "sha256:" + "b" * 64


@pytest.mark.parametrize("hint", ["--claim-id", "--capture-digest"])
def test_cli_payload_file_refuses_attestation_example_hints(
    tmp_path: Path,
    hint: str,
) -> None:
    payload = tmp_path / "payload.json"
    payload.write_text("{}\n", encoding="utf-8")
    value = "CLM-" + "a" * 32 if hint == "--claim-id" else "sha256:" + "b" * 64

    result = CliRunner().invoke(
        cli,
        ["playbill", "authoring", "create", str(payload), hint, value],
    )

    assert result.exit_code == 2
    assert "require --example" in result.output


def test_cli_create_flow_a_stub_reports_bind_refusal_from_served_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CRUXIBLE_SERVER_STATE_DIR", str(tmp_path / "server-state"))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    reset_permissions()
    reset_registry()
    get_playbill_manager().clear()
    registered = get_registry().create_governed_instance_with_id("inst_authoring_refusal")
    instance_id = registered.record.instance_id
    managed = Path(registered.record.location) / ".cruxible" / "playbill-v1"
    owner = generate_client_principal_key(
        tmp_path / "owner-custody",
        principal_id="operator",
        kind="ordinary",
        forbidden_roots=(managed,),
    )
    reviewer = generate_client_principal_key(
        tmp_path / "reviewer-custody",
        principal_id="reviewer",
        kind="ordinary",
        forbidden_roots=(managed,),
    )
    payload = tmp_path / "claim-flow-a.json"
    payload.write_text(json.dumps(claim_flow_a_example().model_dump(mode="json")))

    try:
        with TestClient(create_app()) as transport:
            initialized = transport.post(
                f"/api/v1/{instance_id}/playbill/init",
                json={
                    "principals": [
                        owner.principal.model_dump(mode="json"),
                        reviewer.principal.model_dump(mode="json"),
                    ]
                },
            )
            assert initialized.status_code == 200, initialized.text
            client = CruxibleClient(base_url="http://cruxible")
            client._client = transport  # type: ignore[assignment]
            monkeypatch.setattr(
                "cruxible_core.cli.commands._common._get_client",
                lambda: client,
            )
            result = CliRunner().invoke(
                cli,
                [
                    "--server-url",
                    "http://cruxible",
                    "--instance-id",
                    instance_id,
                    "playbill",
                    "authoring",
                    "create",
                    str(payload),
                ],
            )
    finally:
        get_playbill_manager().clear()
        reset_registry()
        reset_permissions()

    assert result.exit_code == 1
    assert "playbill.authoring.working_selection_requires_bind" in result.stderr
    assert "Run playbill authoring bind" in result.stderr
    assert "internal server error" not in result.stderr


def test_cli_validation_names_field_path_and_matching_example(tmp_path: Path) -> None:
    payload = tmp_path / "invalid.json"
    payload.write_text(
        json.dumps(
            {
                "kind": "claim",
                "subject": "project.work_item/wi-42",
                "predicate": "project.work_item.status",
                "object": {"kind": "literal", "value": "ready"},
                "role": "observation",
                "rationale": "Observed ready.",
                "source": {"kind": "self_source"},
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
    assert "$.claim.source.self_source.body" in result.output
    assert "playbill authoring create --example claim-self-source" in result.output


def test_cli_bind_derives_observation_and_compiles(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    source = tmp_path / "work-items.md"
    source.write_bytes(b"before\nstatus: ready\nafter\n")
    stub = claim_self_source_example().model_dump(mode="json")
    stub["source"] = {"kind": "working_selection", "source_id": "repo.work-items"}
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
    stub["source"] = {"kind": "working_selection", "source_id": "repo.work-items"}
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


@pytest.mark.parametrize("citation_role", ["evidence", "copy"])
def test_cli_bind_declared_block_is_role_aware(
    monkeypatch,
    tmp_path: Path,
    citation_role: str,
) -> None:  # type: ignore[no-untyped-def]
    source = tmp_path / "work-items.md"
    body = b"status: ready\n"
    stamp = ProjectionBlockStampV1(
        source_id="repo.work-items",
        block_id="status",
        declared_generation=1,
        declared_coordinate=AcceptedCoordinate.model_validate(COORDINATE.model_dump(mode="json")),
        backing=(
            ProjectionClaimBackingV1(
                identity=ArtifactIdentity(kind="Claim", name="CLM-existing"),
                statement_digest="sha256:" + "8" * 64,
            ),
        ),
        body_digest="sha256:" + hashlib.sha256(body).hexdigest(),
    )
    source.write_bytes(
        render_projection_opening(stamp) + body + b"<!-- /playbill:block:status -->\n"
    )
    stub = claim_self_source_example().model_dump(mode="json")
    stub["source"] = {"kind": "working_selection", "source_id": "repo.work-items"}
    stub["citation_role"] = citation_role
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

    if citation_role == "evidence":
        assert result.exit_code == 1
        assert "playbill.projection.independent_evidence_forbidden" in result.output
        assert calls == []
    else:
        assert result.exit_code == 0, result.output
        assert calls[0]["citation_role"] == "copy"
