"""PC-A1 Subject wire, law, proposal, projection, history, and explanation tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.artifacts import (
    ArtifactAuthority,
    ArtifactIdentity,
    ArtifactLifecycle,
)
from cruxible_client.contracts.errors import ProposalAdmissionError, SubjectFormatError
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import (
    SubjectShell,
    parse_subject,
    render_subject,
    subject_digest,
    subject_path,
)
from cruxible_core.playbill.actor_context import TransportCapability
from cruxible_core.playbill.cas import BodyAccessContext
from cruxible_core.playbill.service.documents import (
    service_activate_playbill_proposal,
    service_submit_playbill_approval,
)
from cruxible_core.playbill.service.explain import (
    PlaybillExplainResult,
    service_explain_playbill_subject,
)
from cruxible_core.playbill.service.subjects import (
    service_get_playbill_subject,
    service_list_playbill_subjects,
    service_playbill_subject_history,
    service_propose_playbill_subject,
)
from cruxible_core.storage.playbill_projection import canonical_logical_export
from tests.test_playbill.test_activation import _sign
from tests.test_service.test_playbill_documents import TIMESTAMP, _instance

SUBJECT_PATH = "subjects/project.work_item/wi-123.yaml"
SUBJECT_IDENTITY = "Subject:project.work_item/wi-123"


def _shell(
    *,
    lifecycle: ArtifactLifecycle = ArtifactLifecycle(),
) -> SubjectShell:
    return SubjectShell(
        identity=ArtifactIdentity(kind="Subject", name="project.work_item/wi-123"),
        subject_kind="project.work_item",
        subject_id="wi-123",
        authority=ArtifactAuthority(
            propose_roles=("owner",),
            approve_roles=("owner",),
        ),
        lifecycle=lifecycle,
    )


def _accept(instance, approver, shell: SubjectShell, *, name: str = "subject"):
    inspection = service_propose_playbill_subject(
        instance,
        shell=shell,
        actor_id="owner",
        proposal_name=name,
        timestamp=TIMESTAMP,
    )
    candidate = inspection.proposal.candidate
    assert candidate is not None
    approval = _sign(
        approver,
        candidate.candidate_digest,
        candidate.candidate.parent_semantic_root,
    )
    service_submit_playbill_approval(
        instance,
        proposal_id=inspection.proposal.admission.proposal_id,
        attestation=approval.attestation,
        authenticated_submitter=approver.principal.principal_id,
    )
    activated = service_activate_playbill_proposal(
        instance,
        proposal_id=inspection.proposal.admission.proposal_id,
    )
    assert activated.status == "accepted"
    return inspection


def test_subject_wire_digest_and_path_are_canonical_and_golden() -> None:
    shell = _shell()
    wire = render_subject(shell)
    assert parse_subject(wire, path=SUBJECT_PATH) == shell
    assert subject_path(shell.subject_kind, shell.subject_id) == SUBJECT_PATH
    golden = json.loads(
        (Path(__file__).parents[1] / "goldens" / "playbill" / "subject-v1.json").read_text()
    )
    assert json.loads(wire) == golden["wire"]
    assert subject_digest(shell).tagged == golden["artifact_digest"]

    with pytest.raises(SubjectFormatError, match="identity/path"):
        parse_subject(wire, path="subjects/project.work_item/wi-999.yaml")


def test_subject_is_identity_only_and_refuses_properties_or_metadata() -> None:
    payload = _shell().model_dump(mode="json")
    with pytest.raises(ValidationError, match="properties"):
        SubjectShell.model_validate({**payload, "properties": {"status": "ready"}})
    with pytest.raises(ValidationError, match="metadata"):
        SubjectShell.model_validate({**payload, "metadata": {"label": "Work item"}})
    with pytest.raises(ValidationError, match="identity"):
        SubjectShell.model_validate(
            {
                **payload,
                "identity": {"kind": "Subject", "name": "project.work_item/wi-999"},
            }
        )


def test_subject_roles_are_dormant_but_transport_capability_remains_required(
    tmp_path: Path,
) -> None:
    instance, _owner, _reviewer = _instance(tmp_path)
    capabilities: tuple[TransportCapability, ...] = ("administer", "propose")
    accepted = service_propose_playbill_subject(
        instance,
        shell=_shell(),
        actor_id="reviewer",
        proposal_name="unauthorized-subject",
        timestamp=TIMESTAMP,
        capabilities=capabilities,
    )
    assert accepted.proposal.candidate is not None

    with pytest.raises(ProposalAdmissionError, match="propose capability"):
        service_propose_playbill_subject(
            instance,
            shell=_shell(),
            actor_id="owner",
            proposal_name="transport-refusal",
            timestamp=TIMESTAMP,
            capabilities=("administer",),
        )


def test_subject_acceptance_rebuild_history_and_explanation(tmp_path: Path) -> None:
    instance, _owner, reviewer = _instance(tmp_path)
    _accept(instance, reviewer, _shell())
    accepted = instance.accepted_coordinate()

    subject = service_get_playbill_subject(instance, identity=SUBJECT_IDENTITY)
    assert subject.coordinate.git_oid == accepted.git_oid
    assert subject.envelope["kind"] == "subject"
    assert subject.envelope["revision"] == 1
    assert {fact["schema_id"] for fact in subject.facts} >= {
        "playbill.subject.attestation_coverage",
        "playbill.subject.governance",
        "playbill.subject.history",
        "playbill.subject.identity",
        "playbill.subject.lifecycle",
        "playbill.subject.provenance",
        "playbill.subject.references",
    }
    assert service_list_playbill_subjects(instance).subjects == (subject,)

    history = service_playbill_subject_history(instance, identity=SUBJECT_IDENTITY)
    assert len(history.entries) == 1
    assert history.entries[0].artifact_digest == subject_digest(_shell()).tagged

    explained = service_explain_playbill_subject(
        instance,
        subject=SemanticAddress.whole_artifact(SUBJECT_PATH),
        at=subject.coordinate,
        detail="evidence",
        access=BodyAccessContext(principal_id="reader"),
    )
    assert isinstance(explained, PlaybillExplainResult)
    assert explained.source_mapping is None
    assert explained.redactions == ()
    assert explained.attestation_coverage["coverage_binding"]["coverage"] == (
        "containing_change_set"
    )


def test_subject_successor_uses_exact_predecessor_and_projection_revision(tmp_path: Path) -> None:
    instance, _owner, reviewer = _instance(tmp_path)
    initial = _shell()
    _accept(instance, reviewer, initial)
    retired = _shell(
        lifecycle=ArtifactLifecycle(
            state="retired",
            predecessor_digest=subject_digest(initial).tagged,
        )
    )
    _accept(instance, reviewer, retired, name="retire-subject")

    subject = service_get_playbill_subject(instance, identity=SUBJECT_IDENTITY)
    assert subject.envelope["revision"] == 2
    history = service_playbill_subject_history(instance, identity=SUBJECT_IDENTITY)
    assert [entry.lifecycle_state for entry in history.entries] == ["live", "retired"]
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        logical = canonical_logical_export(projection.index_path)
    live = next(table for table in logical["tables"] if table["name"] == "live_identities")
    assert live["rows"] == []
