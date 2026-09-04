"""The typed world facade against a live daemon: define, state, accept, read back."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from cruxible_client import Playbill
from cruxible_client.authoring.sdk_types import (
    AbsentSubject,
    Cardinality,
    ClaimObjectKind,
    LiteralSchemaError,
    PendingClaimTypeRef,
    PendingSubjectRef,
)
from cruxible_client.authoring.world_stub import STUB_HEADER_TAG
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.attestations import ApprovalStatement
from cruxible_client.contracts.claim_types import ClaimType
from cruxible_client.contracts.policies import (
    ClaimAdmissionPolicyV1,
    ClaimEvidenceAdmissionPolicyV1,
    ClaimResolutionPolicyV1,
)
from cruxible_client.contracts.subjects import SubjectShell
from cruxible_client.transport.http import CruxibleClient
from cruxible_core.cli.main import cli
from cruxible_core.playbill.signing import LocalEd25519ApprovalSigner

PACKAGE_KIND = "sec.package"
VULNERABILITY_KIND = "sec.vulnerability"
SEVERITY = "sec.vuln.severity"
AFFECTS = "sec.vuln.affects_package"


def _workspace(root: Path) -> Path:
    workspace = root / "world-workspace"
    (workspace / ".playbill").mkdir(parents=True)
    (workspace / "corpus").mkdir()
    (workspace / "corpus" / "advisory.md").write_text("# advisory\n", encoding="utf-8")
    (workspace / ".playbill" / "sources.yaml").write_text(
        "tag: playbill-source-catalog-v1\n"
        "catalog_kind: portable\n"
        "entries:\n"
        "  - name: corpus.advisory\n"
        "    locator: corpus/advisory.md\n"
        "    document_id: advisory\n"
        "    document_kind: note\n"
        "    title: Advisory\n"
        "    media_type: text/markdown\n"
        "    governance_scope: [Document:advisory]\n",
        encoding="utf-8",
    )
    return workspace


def _severity_type() -> ClaimType:
    return ClaimType(
        identity=ArtifactIdentity(kind="ClaimType", name=SEVERITY),
        predicate=SEVERITY,
        allowed_subject_kinds=(VULNERABILITY_KIND,),
        object_kind="literal",
        literal_schema={"type": "string", "enum": ["high", "low"]},
        cardinality="one",
        permitted_roles=("observation",),
        evidence_admission_policy=ClaimEvidenceAdmissionPolicyV1(),
        admission_policy=ClaimAdmissionPolicyV1(),
        resolution_policy=ClaimResolutionPolicyV1(
            cardinality="one",
            eligible_verdicts=("supported",),
            selector="only_contender",
        ),
    )


def _affects_type() -> ClaimType:
    return ClaimType(
        identity=ArtifactIdentity(kind="ClaimType", name=AFFECTS),
        predicate=AFFECTS,
        allowed_subject_kinds=(VULNERABILITY_KIND,),
        object_kind="subject",
        allowed_object_subject_kinds=(PACKAGE_KIND,),
        cardinality="many",
        permitted_roles=("observation",),
        evidence_admission_policy=ClaimEvidenceAdmissionPolicyV1(),
        admission_policy=ClaimAdmissionPolicyV1(),
        resolution_policy=ClaimResolutionPolicyV1(
            cardinality="many",
            eligible_verdicts=("supported",),
            selector="all",
        ),
    )


def _shell(subject_kind: str, subject_id: str) -> SubjectShell:
    return SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name=f"{subject_kind}/{subject_id}"),
        subject_kind=subject_kind,
        subject_id=subject_id,
        lifecycle=ArtifactLifecycle(),
    )


def _approve_and_activate(
    client: TestClient,
    instance_id: str,
    private_key_path: Path,
    proposal_id: str,
) -> None:
    challenge = client.post(
        f"/api/v1/{instance_id}/playbill/proposals/{proposal_id}/approval-challenge",
        json={"signer_id": "reviewer"},
    )
    assert challenge.status_code == 200, challenge.text
    body = challenge.json()
    signer = LocalEd25519ApprovalSigner.open(
        signer_id="reviewer",
        private_key_path=private_key_path,
        expected_public_key=body["signer_principal"]["public_key"],
        forbidden_roots=(),
    )
    attestation = signer.sign(ApprovalStatement.model_validate(body["statement"]))
    approved = client.post(
        f"/api/v1/{instance_id}/playbill/proposals/{proposal_id}/approvals",
        json={"attestation": attestation.model_dump(mode="json")},
    )
    assert approved.status_code == 200, approved.text
    activated = client.post(f"/api/v1/{instance_id}/playbill/proposals/{proposal_id}/activate")
    assert activated.status_code == 200, activated.text
    assert activated.json()["status"] == "accepted"


@pytest.fixture
def connection(
    playbill_http: tuple[TestClient, str, Path],
    tmp_path: Path,
) -> tuple[Playbill, TestClient, str, Path]:
    http, instance_id, private_key_path = playbill_http
    transport = CruxibleClient(base_url="http://cruxible")
    transport._client = http  # type: ignore[assignment]
    playbill = Playbill._from_client(
        transport,
        instance_id=instance_id,
        workspace=_workspace(tmp_path),
    )
    return playbill, http, instance_id, private_key_path


def _accept_vocabulary(
    playbill: Playbill,
    http: TestClient,
    instance_id: str,
    private_key_path: Path,
) -> None:
    """Land the vocabulary and one Subject the later sets read."""

    draft = playbill.changes(rationale="Open the vulnerability vocabulary.")
    draft.claim_type(_severity_type())
    draft.claim_type(_affects_type())
    draft.subject(_shell(VULNERABILITY_KIND, "cve-2026-69247"))
    intent = draft.prepare()
    assert not intent.refused, intent.diagnostics
    submitted = intent.submit()
    assert submitted._candidate_status is not None
    proposal_id = submitted._candidate_status.proposal_id
    assert proposal_id is not None
    _approve_and_activate(http, instance_id, private_key_path, proposal_id)
    playbill.refresh()


def test_a_same_set_definition_lands_with_the_claim_that_reads_it_in_one_generation(
    connection: tuple[Playbill, TestClient, str, Path],
) -> None:
    """A ref to an unaccepted Subject must not refuse against the base tree."""

    playbill, http, instance_id, private_key_path = connection
    _accept_vocabulary(playbill, http, instance_id, private_key_path)

    world = playbill.world()
    vulnerability = world.sec.vulnerability["cve-2026-69247"]
    assert world.sec.vuln.severity.cardinality is Cardinality.ONE
    assert world.sec.vuln.affects_package.object_kind is ClaimObjectKind.SUBJECT

    draft = playbill.changes(rationale="Name the package this advisory affects.")
    package = draft.subject(world.sec.package.define("click"))
    assert isinstance(package, PendingSubjectRef)
    draft.claim(
        subject=vulnerability,
        predicate=world.sec.vuln.affects_package,
        value=package,
        role="observation",
        rationale="The advisory names this package.",
        supported_by=None,
        copied_from=None,
        self_source="affects: click\n",
        qualifier=None,
        effective_period=None,
        revises=None,
        dispositions={},
        publish_to=None,
        subject_definition=None,
        claim_type_definition=None,
    )
    draft.claim(
        subject=vulnerability,
        predicate=world.sec.vuln.severity,
        value=world.sec.vuln.severity.high,
        role="observation",
        rationale="The advisory rates this vulnerability.",
        supported_by=None,
        copied_from=None,
        self_source="severity: high\n",
        qualifier=None,
        effective_period=None,
        revises=None,
        dispositions={},
        publish_to=None,
        subject_definition=None,
        claim_type_definition=None,
    )
    intent = draft.prepare()
    assert not intent.refused, intent.diagnostics
    assert len(intent._raw["payload"]["members"]) == 3

    submitted = intent.submit()
    assert submitted._candidate_status is not None
    proposal_id = submitted._candidate_status.proposal_id
    assert proposal_id is not None
    _approve_and_activate(http, instance_id, private_key_path, proposal_id)
    playbill.refresh()

    landed = playbill.world()
    assert landed.sec.package["click"].address == "sec.package/click"
    claims = landed.sec.vulnerability["cve-2026-69247"].claims
    assert {claim.predicate for claim in claims} == {AFFECTS, SEVERITY}
    assert landed.sec.vulnerability["cve-2026-69247"].severity[0].value == "high"


def test_a_same_set_claim_type_ref_states_a_claim_under_the_type_it_defines(
    connection: tuple[Playbill, TestClient, str, Path],
) -> None:
    playbill, http, instance_id, private_key_path = connection
    _accept_vocabulary(playbill, http, instance_id, private_key_path)

    draft = playbill.changes(rationale="Open the severity note slot and use it once.")
    note_type = _severity_type().model_copy(
        update={
            "identity": ArtifactIdentity(kind="ClaimType", name="sec.vuln.note"),
            "predicate": "sec.vuln.note",
            "literal_schema": {"type": "string"},
        }
    )
    predicate = draft.claim_type(note_type)
    assert isinstance(predicate, PendingClaimTypeRef)
    draft.claim(
        subject=f"{VULNERABILITY_KIND}/cve-2026-69247",
        predicate=predicate,
        value="Escalated by the platform leads.",
        role="observation",
        rationale="The runbook records the escalation.",
        supported_by=None,
        copied_from=None,
        self_source="note: escalated\n",
        qualifier=None,
        effective_period=None,
        revises=None,
        dispositions={},
        publish_to=None,
        subject_definition=None,
        claim_type_definition=None,
    )
    intent = draft.prepare()

    assert not intent.refused, intent.diagnostics
    submitted = intent.submit()
    assert submitted._candidate_status is not None
    proposal_id = submitted._candidate_status.proposal_id
    assert proposal_id is not None
    _approve_and_activate(http, instance_id, private_key_path, proposal_id)
    playbill.refresh()

    assert "sec.vuln.note" in playbill.world().predicates


def test_the_world_reads_the_accepted_vocabulary_and_refuses_what_it_does_not_name(
    connection: tuple[Playbill, TestClient, str, Path],
) -> None:
    playbill, http, instance_id, private_key_path = connection
    _accept_vocabulary(playbill, http, instance_id, private_key_path)

    world = playbill.world()

    assert world.kinds == (PACKAGE_KIND, VULNERABILITY_KIND)
    assert world.predicates == (AFFECTS, SEVERITY)
    assert world.unstructured_predicates == ()
    assert world.sec.vuln.severity.members == ("high", "low")
    with pytest.raises(LiteralSchemaError):
        world.sec.vuln.severity("critical")
    with pytest.raises(AbsentSubject):
        world.sec.package["click"]


def test_the_cli_leaf_writes_a_coordinate_stamped_stub_for_the_live_world(
    connection: tuple[Playbill, TestClient, str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    playbill, http, instance_id, private_key_path = connection
    _accept_vocabulary(playbill, http, instance_id, private_key_path)
    transport = playbill._client
    monkeypatch.setattr("cruxible_core.cli.commands._common._get_client", lambda: transport)
    out_path = tmp_path / "generated" / "world.pyi"

    result = CliRunner().invoke(
        cli,
        [
            "--server-url",
            "http://cruxible",
            "--instance-id",
            instance_id,
            "playbill",
            "world",
            "stub",
            "--out",
            str(out_path),
        ],
    )

    assert result.exit_code == 0, result.output
    rendered = out_path.read_text(encoding="utf-8")
    assert rendered.startswith(f"# {STUB_HEADER_TAG}:")
    assert f"#   git_oid          {playbill.coordinate.git_oid}" in rendered
    assert "class _W_sec__vuln__severity(ClaimTypeRef):" in rendered
    assert "    high: LiteralValue" in rendered
    assert rendered == playbill.world().stub()
    # The stub the daemon's own world renders is a file a type checker reads.
    ast.parse(rendered)
