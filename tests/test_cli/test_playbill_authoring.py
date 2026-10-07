"""CLI authoring adapters keep payloads local and machine identity opaque."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal, get_args

import click
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from pydantic import TypeAdapter, ValidationError

from cruxible_client import CruxibleClient, contracts
from cruxible_client.authoring.blocks import render_projection_opening
from cruxible_client.authoring.examples import (
    AUTHORING_EXAMPLE_NAMES,
    authoring_example_note,
    claim_flow_a_example,
    claim_self_source_example,
)
from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.authoring.inputs import (
    AuthoringChangeSetMemberInput,
    AuthoringInput,
)
from cruxible_client.contracts.declared_blocks import (
    ProjectionBlockStampV1,
    ProjectionClaimBacking,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.authoring.lowering import CHANGE_SET_SINGLETON_ONLY_MEMBERS
from cruxible_core.claims.claim_type_inputs import (
    ClaimTypeInputRecord,
    claim_type_input_template,
    lower_claim_type_input,
)
from cruxible_core.cli.main import cli
from cruxible_core.governance.keys import generate_client_principal_key
from cruxible_core.runtime.permissions import reset_permissions
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.server.app import create_app
from cruxible_core.server.registry import get_registry, reset_registry
from tests.core_support._claim_type_support import claim_type_input_example
from tests.support.preflight_results import stub_diagnostic, stub_preflight_result

COORDINATE = contracts.AcceptedCoordinate(
    git_oid="1" * 64,
    semantic_root="sha256:" + "2" * 64,
    generation_root="sha256:" + "3" * 64,
    compiler_digest="sha256:" + "4" * 64,
)
INTENT_ID = "AIT-" + "5" * 32
EXPECTATION_ID = "sha256:" + "6" * 64


def test_cli_line_run_forwards_only_the_occurrence_assertion(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, str, str | None, str]] = []

    class StubClient:
        def run_line(
            self,
            instance_id: str,
            line_identity_digest: str,
            *,
            occurrence_id: str | None,
            evaluation_time: str,
            resolution_contract=None,
            event=None,
            repeat=False,
        ) -> contracts.ProcedureRunState:
            assert resolution_contract is None and event is None and repeat is False
            calls.append((instance_id, line_identity_digest, occurrence_id, evaluation_time))
            return contracts.ProcedureRunState(
                run_id=None,
                procedure_identity={"kind": "Procedure", "name": "triage"},
                procedure_artifact_digest="sha256:" + "9" * 64,
                bound_coordinate=COORDINATE,
                head_at_admission=COORDINATE,
                lane="current",
                evaluation_time=evaluation_time,
                status="admission_refused",
                pending_inputs=[],
                outcomes=[],
                next_operation={"kind": "terminal"},
                terminal={
                    "tag": "playbill-procedure-admission-refusal-v1",
                    "classification": "admission_refusal",
                    "code": "occurrence_not_due",
                    "message": "not due",
                    "details": {},
                    "retryable": False,
                },
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    digest = "sha256:" + "a" * 64
    occurrence = "sha256:" + "b" * 64
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://line.example.test",
            "--instance-id",
            "inst_line",
            "line",
            "run",
            digest,
            "--occurrence-id",
            occurrence,
            "--evaluation-time",
            "2026-09-02T12:00:00Z",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [("inst_line", digest, occurrence, "2026-09-02T12:00:00+00:00")]


def test_cli_compile_reads_payload_and_submit_uses_only_opaque_intent(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    payload = tmp_path / "claim.json"
    authoring = claim_self_source_example().model_dump(mode="json")
    payload.write_text(json.dumps(authoring))
    calls: list[tuple[str, object]] = []

    class StubClient:
        def compile_authoring_input(
            self,
            instance_id: str,
            *,
            input: dict[str, object],
            intent_id: str | None,
        ) -> contracts.AuthoringPreflightResult:
            calls.append((instance_id, input))
            assert intent_id is None
            return stub_preflight_result(verdict="refused", diagnostics=(stub_diagnostic("x"),))

        def submit_authoring_intent(
            self, instance_id: str, intent_id: str
        ) -> contracts.AuthoringSubmitResultRecord:
            calls.append((instance_id, intent_id))
            status = contracts.CandidateStatusRecord(
                state="draft",
                current_accepted_coordinate=COORDINATE,
            )
            return contracts.AuthoringSubmitResultRecord(
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
        "authoring",
    ]
    compiled = runner.invoke(cli, [*common, "compile", str(payload), "--json"])
    submitted = runner.invoke(cli, [*common, "submit", "--intent-id", INTENT_ID, "--json"])

    assert compiled.exit_code == 0, compiled.output
    assert submitted.exit_code == 0, submitted.output
    assert calls == [
        ("inst_authoring", authoring),
        ("inst_authoring", INTENT_ID),
    ]
    assert "target: inst_authoring @ https://authoring.example.test (explicit)" in compiled.stderr
    assert INTENT_ID in submitted.output


def test_cli_submit_takes_a_payload_directly_and_dry_run_saves_nothing(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    payload = tmp_path / "claim.json"
    authoring = claim_self_source_example().model_dump(mode="json")
    payload.write_text(json.dumps(authoring))
    calls: list[tuple[str, object, object]] = []

    class StubClient:
        def submit_authoring_input(
            self, instance_id: str, *, input: dict[str, object], intent_id: str | None
        ) -> contracts.AuthoringSubmitResultRecord:
            calls.append(("submit", input, intent_id))
            return contracts.AuthoringSubmitResultRecord(
                intent={"intent_id": INTENT_ID},
                status=contracts.CandidateStatusRecord(
                    state="draft", current_accepted_coordinate=COORDINATE
                ),
            )

        def preview_authoring_input(
            self, instance_id: str, *, input: dict[str, object]
        ) -> contracts.AuthoringPreflightResult:
            calls.append(("preview", input, None))
            return stub_preflight_result(verdict="refused", diagnostics=(stub_diagnostic("x"),))

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    runner = CliRunner()
    common = [
        "--server-url",
        "https://authoring.example.test",
        "--instance-id",
        "inst_authoring",
        "authoring",
        "submit",
    ]

    direct = runner.invoke(cli, [*common, str(payload), "--json"])
    onto = runner.invoke(cli, [*common, str(payload), "--intent-id", INTENT_ID, "--json"])
    preview = runner.invoke(cli, [*common, str(payload), "--dry-run", "--json"])

    assert direct.exit_code == 0, direct.output
    assert onto.exit_code == 0, onto.output
    assert preview.exit_code == 0, preview.output
    assert calls == [
        ("submit", authoring, None),
        ("submit", authoring, INTENT_ID),
        ("preview", authoring, None),
    ]
    assert json.loads(preview.stdout)["verdict"] == "refused"
    neither = runner.invoke(cli, common)
    assert neither.exit_code == 2 and "provide PAYLOAD, --intent-id, or both" in neither.output
    staged_preview = runner.invoke(cli, [*common, "--intent-id", INTENT_ID, "--dry-run"])
    assert staged_preview.exit_code == 2
    assert "takes no --intent-id" in staged_preview.output


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
        "code": "cruxible.claim_type.anticipated_source_contract_omitted",
        "field_path": "$.evidence_admission_policy.rules",
        "source_id": "corpus.runbook",
        "contract_identity": "CaptureContract:playbill.foreign-source.corpus.runbook",
        "contract_digest": "sha256:" + "7" * 64,
        "replacement_rule_fragment": {"capture_contract_digests": ["sha256:" + "7" * 64]},
    }

    class StubClient:
        def propose_claim_type_input(
            self,
            instance_id: str,
            *,
            input: dict[str, object],
            proposal_name: str,
            dry_run: bool | None = None,
            at: str | None = None,
        ) -> contracts.ClaimTypeInputProposalResult:
            assert (instance_id, proposal_name) == (
                "inst_authoring",
                "project.work_item.replace_me",
            )
            assert input["anticipated_source_ids"] == ["corpus.runbook"]
            return contracts.ClaimTypeInputProposalResult(
                proposal=contracts.ProposalInspection(
                    proposal={"proposal_id": "sha256:" + "8" * 64},
                    accepted_coordinate=COORDINATE,
                ),
                lint=contracts.ClaimTypeProposalLint(warnings=[warning]),
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://authoring.example.test",
            "--instance-id",
            "inst_authoring",
            "claim-type",
            "propose",
            "--input",
            str(payload),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["lint"]["warnings"] == [warning]


def test_cli_claim_type_template_is_complete_model_generated_and_local(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: (_ for _ in ()).throw(AssertionError("template must not contact the daemon")),
    )

    result = CliRunner().invoke(cli, ["claim-type", "propose", "--template"])

    assert result.exit_code == 0, result.output
    rendered = ClaimTypeInputRecord.model_validate(json.loads(result.stdout))
    assert rendered == claim_type_input_template()
    lowered = lower_claim_type_input(rendered, tree={}, identity_rules=True)
    assert lowered.identity.qualified == "ClaimType:project.work_item.status"
    assert lowered.evidence_admission_policy.rules[0].rule_id == "source-repo.replace-me"
    assert rendered.anticipated_source_ids == ("repo.replace-me",)


def test_cli_examples_are_supported_and_schema_discoverable() -> None:
    runner = CliRunner()

    claim_type_help = runner.invoke(cli, ["claim-type", "propose", "--help"])
    claim_type_example = runner.invoke(cli, ["claim-type", "propose", "--example"])
    claim_type_missing = runner.invoke(cli, ["claim-type", "propose"])
    example_help = runner.invoke(cli, ["authoring", "example", "--help"])
    create = runner.invoke(cli, ["authoring", "create", "--help"])

    assert claim_type_help.exit_code == 0, claim_type_help.output
    assert "--template" in claim_type_help.output
    assert "--example" not in claim_type_help.output
    assert claim_type_example.exit_code == 2
    assert "No such option: --example" in claim_type_example.output
    assert claim_type_missing.exit_code == 2
    assert "provide exactly one of --input or --template" in claim_type_missing.output

    assert example_help.exit_code == 0, example_help.output
    assert "authoring example [OPTIONS] [NAME]" in example_help.output
    # The intent is created by compile; there is no separate create door.
    assert create.exit_code == 2
    assert "No such command 'create'" in create.output


def test_cli_refused_stale_preflight_teaches_rebase(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    old_coordinate = COORDINATE.model_copy(update={"git_oid": "a" * 40})

    class StubClient:
        def preflight_authoring_intent(
            self, _instance_id: str, _intent_id: str
        ) -> contracts.AuthoringPreflightResult:
            return stub_preflight_result(
                verdict="refused",
                diagnostics=(stub_diagnostic("x"),),
                accepted_coordinate=COORDINATE.model_dump(mode="json"),
            )

        def get_authoring_intent(
            self, _instance_id: str, _intent_id: str
        ) -> contracts.AuthoringIntentViewRecord:
            return contracts.AuthoringIntentViewRecord(
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
            "authoring",
            "preflight",
            INTENT_ID,
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert f"cruxible authoring rebase {INTENT_ID}" in result.stderr
    assert "advances only through rebase" in result.stderr


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
        "code": "cruxible.claim_type.evidence_policy_admits_no_accepted_contract",
        "field_path": "$.evidence_admission_policy.rules",
        "source_id": None,
        "contract_identity": "CaptureContract:available",
        "contract_digest": "sha256:" + "7" * 64,
        "replacement_rule_fragment": {"capture_contract_digests": ["sha256:" + "7" * 64]},
    }

    class StubClient:
        def migrate_claim_type(
            self,
            instance_id: str,
            *,
            request: dict[str, object],
        ) -> contracts.ClaimTypeMigrationPreflight:
            assert instance_id == "inst_authoring"
            assert request["mode"] == "preflight"
            return contracts.ClaimTypeMigrationPreflight(
                coordinate=COORDINATE,
                successor_artifact_digest="sha256:" + "8" * 64,
                semantic_delta=[
                    contracts.SemanticFieldDelta(
                        field_path="/literal_schema/enum",
                        before=contracts.SemanticFieldValue(state="present", value=["old"]),
                        after=contracts.SemanticFieldValue(state="present", value=["new"]),
                    )
                ],
                dependents=[],
                lint=contracts.ClaimTypeProposalLint(warnings=[warning]),
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://authoring.example.test",
            "--instance-id",
            "inst_authoring",
            "claim-type",
            "migrate",
            str(payload),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["lint"]["warnings"] == [warning]

    human = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://authoring.example.test",
            "--instance-id",
            "inst_authoring",
            "claim-type",
            "migrate",
            str(payload),
        ],
    )
    assert human.exit_code == 0, human.output
    assert "Blast radius: 0 dependent(s)" in human.stdout
    assert '/literal_schema/enum: ["old"] -> ["new"]' in human.stdout
    assert "evidence_admission_policy.rules" in human.stdout


def test_cli_claim_type_migration_submit_names_the_proposal_and_next_step(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    proposal_id = "sha256:" + "7" * 64
    payload = tmp_path / "migration-submit.json"
    payload.write_text(
        json.dumps(
            {
                "tag": "playbill-claim-type-migration-request-v2",
                "mode": "submit",
                "successor": claim_type_input_example().model_dump(mode="json"),
            }
        ),
        encoding="utf-8",
    )

    class StubClient:
        def migrate_claim_type(
            self,
            instance_id: str,
            *,
            request: dict[str, object],
        ) -> contracts.ClaimTypeMigrationResultV2:
            assert instance_id == "inst_authoring"
            assert request["mode"] == "submit"
            return contracts.ClaimTypeMigrationResultV2(
                operation_digest="sha256:" + "6" * 64,
                semantic_delta=[],
                dependents=[],
                proposal=contracts.ProposalInspection(
                    proposal={"admission": {"proposal_id": proposal_id}},
                    accepted_coordinate=COORDINATE,
                ),
            )

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())

    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://authoring.example.test",
            "--instance-id",
            "inst_authoring",
            "claim-type",
            "migrate",
            str(payload),
        ],
    )

    assert result.exit_code == 0, result.output
    assert f"Proposal: {proposal_id}" in result.stdout
    assert f"Next: cruxible proposal approve {proposal_id}" in result.stdout


def test_cli_status_is_a_read_and_emits_no_write_target(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    class StubClient:
        def authoring_intent_status(
            self, instance_id: str, intent_id: str
        ) -> contracts.CandidateStatusRecord:
            assert (instance_id, intent_id) == ("inst_authoring", INTENT_ID)
            return contracts.CandidateStatusRecord(
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
        def whoami(self, instance_id: str) -> contracts.WhoAmI:
            calls.append(f"whoami:{instance_id}")
            return contracts.WhoAmI(
                actor_id="owner",
                credential_label="owner",
                actor_id_source="runtime_credential",
                authenticated=True,
                credential_permission_mode="governed_write",
                principal_registration_status="active",
                active_principal_ids=["daemon", "owner"],
                coordinate=COORDINATE,
                can_author=True,
                authoring_refusal=None,
            )

        def list_proposals(
            self,
            instance_id: str,
            *,
            status: str | None,
            limit: int | None = None,
            cursor: str | None = None,
        ) -> contracts.ProposalList:
            assert limit == contracts.PROPOSAL_LIST_DEFAULT_LIMIT
            assert cursor is None
            calls.append(f"proposals:{instance_id}:{status}")
            return contracts.ProposalList(
                coordinate=COORDINATE,
                status_filter="open",
                entries=[
                    contracts.ProposalListEntry(
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
    ]
    runner = CliRunner()
    identity = runner.invoke(cli, [*base, "whoami"])
    proposals = runner.invoke(cli, [*base, "proposal", "list", "--status", "open"])

    assert identity.exit_code == proposals.exit_code == 0
    assert "Actor ID is the principal this bearer credential is bound to" in identity.output
    assert "governed_write" in identity.output
    assert "open  -  sha256:" in proposals.output
    assert calls == ["whoami:inst_authoring", "proposals:inst_authoring:open"]


def test_cli_examples_are_model_generated_and_need_no_daemon() -> None:
    runner = CliRunner()
    help_result = runner.invoke(cli, ["authoring", "example", "--help"])
    assert help_result.exit_code == 0
    assert "Input kind family: claim | procedure | blueprint | blueprint_instance" in (
        help_result.output
    )
    # Click wraps the family list, so read it as a list rather than by substring:
    # a bare `claim_retirement` was satisfied by the sentence *below* the list.
    unwrapped = " ".join(help_result.output.split())
    member_family = unwrapped.split("Change-set member kind family: ", 1)[1].split(". ", 1)[0]
    assert member_family.split(" | ") == [
        "claim",
        "claim_type",
        "claim_type_succession",
        "claim_retirement",
        "subject",
        "query_definition",
        "procedure_mandate",
        "acquisition_policy",
        "line",
        "trigger",
        "procedure",
        "blueprint",
        "blueprint_instance",
    ]
    assert (
        "approval_policy and procedure_runtime_policy are the reverse: the member union "
        "parses either, but a change set refuses either" in unwrapped
    )

    for name in (
        "claim-existing-capture",
        "claim-flow-a",
        "claim-self-source",
        "procedure",
        "subject",
        "approval-policy",
        "procedure-runtime-policy",
        "procedure-mandate",
        "line",
        "trigger",
        "acquisition-policy",
        "query-claims-by-type",
        "change-set",
        "claim-type-succession",
    ):
        result = runner.invoke(cli, ["authoring", "example", name])
        assert result.exit_code == 0, result.output
        # A note (cron's UTC reading) goes to stderr: stdout is one JSON document.
        payload = json.loads(result.stdout)
        assert payload["kind"] in {
            "claim",
            "change_set",
            "procedure",
            "subject",
            "approval_policy",
            "procedure_runtime_policy",
            "procedure_mandate",
            "query_definition",
            "line",
            "trigger",
            "acquisition_policy",
        }
        if name == "procedure-mandate":
            assert payload["tag"] == "playbill-procedure-mandate-input-v1"
        else:
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
        note = authoring_example_note(name)
        assert result.stderr == ("" if note is None else f"# {note}\n")


def _input_kinds(union: object) -> set[str]:
    """Every `kind` one discriminated authoring-input union actually admits."""

    members = get_args(get_args(union)[0])
    kinds: set[str] = set()
    for model in members:
        annotation = model.model_fields["kind"].annotation
        assert get_args(annotation) and annotation is not Literal
        kinds.add(str(get_args(annotation)[0]))
    return kinds


def _kind_family(output: str, label: str) -> tuple[str, ...]:
    """Read one `--help` kind family back off the rendered help text."""

    _before, _marker, rest = output.partition(f"{label}: ")
    assert _marker, f"{label} missing from help output"
    listed, _stop, _after = rest.partition(".")
    return tuple(
        sorted(
            # `change_set (tagless)` names the one kind whose members carry no tag.
            item.strip().removesuffix(" (tagless)")
            for item in listed.replace("\n", " ").split("|")
        )
    )


def test_cli_example_help_names_only_kinds_the_discriminators_admit() -> None:
    """Every kind the `example` docstring advertises must be authorable.

    The docstring is the only place an agent learns which `kind` a payload file
    may carry, so a kind listed there that `AuthoringInput` refuses costs a
    whole compile round trip -- and so does a member kind that parses but that
    `_lower_change_set` refuses in every set. Both families are read back off
    the rendered help and checked against the discriminated unions and against
    the lowering's own singleton-only table, never against a literal list.
    """

    top_level = TypeAdapter(AuthoringInput)
    member = TypeAdapter(AuthoringChangeSetMemberInput)
    help_output = CliRunner().invoke(cli, ["authoring", "example", "--help"]).output
    member_kinds = _input_kinds(AuthoringChangeSetMemberInput)
    singleton_only = {item.kind for item in CHANGE_SET_SINGLETON_ONLY_MEMBERS}
    assert singleton_only and singleton_only < member_kinds

    advertised_top_level = _kind_family(help_output, "Input kind family")
    assert advertised_top_level == tuple(sorted(_input_kinds(AuthoringInput)))
    advertised_members = _kind_family(help_output, "Change-set member kind family")
    assert advertised_members == tuple(sorted(member_kinds - singleton_only))

    for kind in _input_kinds(AuthoringInput):
        with pytest.raises(ValidationError) as refusal:
            top_level.validate_python({"kind": kind})
        assert {error["type"] for error in refusal.value.errors()} == {"missing"}, kind
    for kind in member_kinds:
        with pytest.raises(ValidationError) as refusal:
            member.validate_python({"kind": kind})
        assert "union_tag_invalid" not in {error["type"] for error in refusal.value.errors()}, kind

    # The two member-only kinds are exactly what the docstring used to promise
    # at top level, and the discriminator refuses both there.
    for kind in ("claim_type", "claim_retirement"):
        assert kind in advertised_members
        assert kind not in advertised_top_level
        with pytest.raises(ValidationError) as refusal:
            top_level.validate_python({"kind": kind})
        assert {error["type"] for error in refusal.value.errors()} == {"union_tag_invalid"}

    # A kind lowering refuses in every change set is the mirror image: the
    # member union parses it, so only this table says it is unauthorable as a
    # member, and the help must place it at top level and nowhere else.
    for kind in sorted(singleton_only):
        assert kind not in advertised_members
        assert kind in advertised_top_level
    assert "a change set refuses either" in " ".join(help_output.split())


def test_propose_help_names_the_sanctioned_proposal_paths() -> None:
    runner = CliRunner()
    document = runner.invoke(cli, ["document", "propose", "--help"])
    claim_type = runner.invoke(cli, ["claim-type", "propose", "--help"])

    assert document.exit_code == 0
    assert "sanctioned command-local Document proposal path" in document.output
    assert "Deprecated" not in document.output
    assert claim_type.exit_code == 0
    assert "sanctioned typed-input ClaimType proposal path" in claim_type.output
    assert "Deprecated" not in claim_type.output
    removed = runner.invoke(cli, ["subject", "propose"])
    assert removed.exit_code != 0
    assert "No such command 'subject'" in removed.output
    # `cruxible query KIND` answers a query itself, so `propose` is read as a
    # kind there; what matters is that query has no subcommands at all.
    from cruxible_core.cli.commands.playbill import query_group

    assert not isinstance(query_group, click.Group)


@pytest.mark.parametrize(
    "arguments",
    [
        ["claim-cite-supporting-evidence"],
        [
            "claim-cite-supporting-evidence",
            "--attestation-claim-id",
            "CLM-" + "a" * 32,
        ],
        [
            "claim-cite-supporting-evidence",
            "--capture-digest",
            "sha256:" + "b" * 64,
        ],
        ["claim-flow-a", "--attestation-claim-id", "CLM-" + "a" * 32],
        ["claim-flow-a", "--capture-digest", "sha256:" + "b" * 64],
        [
            "claim-flow-a",
            "--attestation-claim-id",
            "CLM-" + "a" * 32,
            "--capture-digest",
            "sha256:" + "b" * 64,
        ],
    ],
)
def test_cli_attestation_door_example_options_refuse_incomplete_or_wrong_hints(
    arguments: list[str],
) -> None:
    result = CliRunner().invoke(cli, ["authoring", "example", *arguments])
    assert result.exit_code == 2


def test_cli_attestation_door_example_accepts_both_hints() -> None:
    result = CliRunner().invoke(
        cli,
        [
            "authoring",
            "example",
            "claim-cite-supporting-evidence",
            "--attestation-claim-id",
            "CLM-" + "a" * 32,
            "--capture-digest",
            "sha256:" + "b" * 64,
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["revises"] == "CLM-" + "a" * 32
    assert payload["source"]["capture_digest"] == "sha256:" + "b" * 64


@pytest.mark.parametrize("hint", ["--attestation-claim-id", "--capture-digest"])
def test_cli_example_listing_refuses_attestation_example_hints(hint: str) -> None:
    value = "CLM-" + "a" * 32 if hint == "--attestation-claim-id" else "sha256:" + "b" * 64

    result = CliRunner().invoke(cli, ["authoring", "example", hint, value])

    assert result.exit_code == 2
    assert "require NAME" in result.output


def test_cli_example_without_a_name_lists_every_example() -> None:
    result = CliRunner().invoke(cli, ["authoring", "example"])

    assert result.exit_code == 0, result.output
    assert tuple(result.output.split()) == AUTHORING_EXAMPLE_NAMES


def test_cli_compile_flow_a_stub_reports_bind_refusal_from_served_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "server-state"
    monkeypatch.setenv("CRUXIBLE_STATE_ROOT", str(state))
    monkeypatch.delenv("CRUXIBLE_SERVER_AUTH", raising=False)
    reset_permissions()
    reset_registry()
    get_playbill_manager().clear()
    registered = get_registry().create_governed_instance_with_id("inst_authoring_refusal")
    instance_id = registered.record.instance_id
    managed = Path(registered.record.location)
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
                f"/api/v1/{instance_id}/init",
                json={
                    "principals": [
                        owner.principal.model_dump(mode="json"),
                        reviewer.principal.model_dump(mode="json"),
                    ],
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
                    "authoring",
                    "compile",
                    str(payload),
                ],
            )
    finally:
        get_playbill_manager().clear()
        reset_registry()
        reset_permissions()

    assert result.exit_code == 1
    assert "cruxible.authoring.working_selection_requires_bind" in result.stderr
    assert "Run cruxible authoring bind" in result.stderr
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
            "authoring",
            "compile",
            str(payload),
        ],
    )

    assert result.exit_code == 1
    assert "$.claim.source.self_source.body" in result.output
    assert "cruxible authoring example claim-self-source" in result.output


def _catalog(workspace: Path, source: Path) -> None:
    """Name ``source`` repo.work-items in the workspace's source catalog (bind checks it)."""

    (workspace / ".cruxible").mkdir(exist_ok=True)
    (workspace / ".cruxible" / "sources.yaml").write_text(
        "tag: playbill-source-catalog-v1\n"
        "catalog_kind: portable\n"
        "entries:\n"
        "  - name: repo.work-items\n"
        f"    locator: {source.relative_to(workspace).as_posix()}\n",
        encoding="utf-8",
    )


def test_cli_bind_derives_observation_and_compiles(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    source = tmp_path / "work-items.md"
    source.write_bytes(b"before\nstatus: ready\nafter\n")
    stub = claim_self_source_example().model_dump(mode="json")
    stub["source"] = {"kind": "working_selection", "source_id": "repo.work-items"}
    stub["citation_role"] = "evidence"
    _catalog(tmp_path, source)
    payload_file = tmp_path / "stub.json"
    payload_file.write_text(json.dumps(stub))
    calls: list[dict[str, object]] = []

    class StubClient:
        def compile_authoring(
            self,
            instance_id: str,
            *,
            payload: dict[str, object],
            intent_id: str | None,
        ) -> contracts.AuthoringPreflightResult:
            assert (instance_id, intent_id) == ("inst_authoring", None)
            calls.append(payload)
            return stub_preflight_result()

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://authoring.example.test",
            "--instance-id",
            "inst_authoring",
            "authoring",
            "bind",
            "--workspace-root",
            str(tmp_path),
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
    _catalog(tmp_path, source)
    payload_file = tmp_path / "stub.json"
    payload_file.write_text(json.dumps(stub))
    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: (_ for _ in ()).throw(AssertionError("daemon must not be called")),
    )

    result = CliRunner().invoke(
        cli,
        [
            "authoring",
            "bind",
            "--workspace-root",
            str(tmp_path),
            "--file",
            str(source),
            "--anchor",
            "aa",
            "--payload-file",
            str(payload_file),
        ],
    )

    assert result.exit_code == 1
    assert "cruxible.authoring.anchor_ambiguous" in result.output
    assert '"candidate_byte_offsets":[0,1]' in result.output
    assert "--occurrence" in result.output


def test_cli_bind_missing_anchor_has_no_occurrence_repair_hint(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    source = tmp_path / "missing.txt"
    source.write_text("status: waiting")
    stub = claim_self_source_example().model_dump(mode="json")
    stub["source"] = {"kind": "working_selection", "source_id": "repo.work-items"}
    stub["citation_role"] = "evidence"
    _catalog(tmp_path, source)
    payload_file = tmp_path / "stub.json"
    payload_file.write_text(json.dumps(stub))
    monkeypatch.setattr(
        "cruxible_core.cli.commands._common._get_client",
        lambda: (_ for _ in ()).throw(AssertionError("daemon must not be called")),
    )

    result = CliRunner().invoke(
        cli,
        [
            "authoring",
            "bind",
            "--workspace-root",
            str(tmp_path),
            "--file",
            str(source),
            "--anchor",
            "ready",
            "--payload-file",
            str(payload_file),
        ],
    )

    assert result.exit_code == 1
    assert "cruxible.authoring.anchor_not_found" in result.output
    assert "anchor not found in file" in result.output
    assert '"observed_occurrence_count":0' in result.output
    assert '"candidate_byte_offsets":[]' in result.output
    assert "--occurrence" not in result.output


def test_cli_bind_occurrence_selects_one_ambiguous_anchor(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    source = tmp_path / "ambiguous.txt"
    source.write_text("aaa")
    stub = claim_self_source_example().model_dump(mode="json")
    stub["source"] = {"kind": "working_selection", "source_id": "repo.work-items"}
    stub["citation_role"] = "evidence"
    _catalog(tmp_path, source)
    payload_file = tmp_path / "stub.json"
    payload_file.write_text(json.dumps(stub))
    calls: list[dict[str, object]] = []

    class StubClient:
        def compile_authoring(
            self,
            instance_id: str,
            *,
            payload: dict[str, object],
            intent_id: str | None,
        ) -> contracts.AuthoringPreflightResult:
            calls.append(payload)
            return stub_preflight_result()

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://authoring.example.test",
            "--instance-id",
            "inst_authoring",
            "authoring",
            "bind",
            "--workspace-root",
            str(tmp_path),
            "--file",
            str(source),
            "--anchor",
            "aa",
            "--occurrence",
            "2",
            "--payload-file",
            str(payload_file),
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["source"]["selector"]["start_byte"] == 1  # type: ignore[index]
    assert calls[0]["source"]["selector"]["observed_occurrence_count"] == 2  # type: ignore[index]
    assert calls[0]["source"]["selector"]["selected_occurrence"] == 2  # type: ignore[index]


@pytest.mark.parametrize("citation_role", ["evidence", "copy"])
def test_cli_bind_declared_block_refuses_every_role(
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
            ProjectionClaimBacking(
                identity=ArtifactIdentity(kind="Claim", name="CLM-existing"),
                statement_digest="sha256:" + "8" * 64,
            ),
        ),
        body_digest="sha256:" + hashlib.sha256(body).hexdigest(),
    )
    source.write_bytes(
        render_projection_opening(stamp) + body + b"<!-- /cruxible:block:status -->\n"
    )
    stub = claim_self_source_example().model_dump(mode="json")
    stub["source"] = {"kind": "working_selection", "source_id": "repo.work-items"}
    stub["citation_role"] = citation_role
    _catalog(tmp_path, source)
    payload_file = tmp_path / "stub.json"
    payload_file.write_text(json.dumps(stub))
    calls: list[dict[str, object]] = []

    class StubClient:
        def compile_authoring(
            self,
            instance_id: str,
            *,
            payload: dict[str, object],
            intent_id: str | None,
        ) -> contracts.AuthoringPreflightResult:
            calls.append(payload)
            return stub_preflight_result()

    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: StubClient())
    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "https://authoring.example.test",
            "--instance-id",
            "inst_authoring",
            "authoring",
            "bind",
            "--workspace-root",
            str(tmp_path),
            "--file",
            str(source),
            "--anchor",
            "status: ready",
            "--payload-file",
            str(payload_file),
            "--json",
        ],
    )

    # Every role refuses inside a stamped block: a copy of projection bytes
    # attests them into concrete exactly as evidence would.
    assert result.exit_code == 1
    assert "cruxible.projection.evidence_from_projection" in result.output
    assert calls == []
