"""The terminal lifecycle state of one governed instance."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_client.contracts.documents import (
    DocumentAuthority,
    DocumentLifecycle,
    DocumentShell,
    render_document,
)
from cruxible_client.contracts.errors import PlaybillInstanceDecommissioned
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.instance import DESCRIPTOR_FILE, PlaybillInstance
from cruxible_core.playbill.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.playbill.service.subjects import service_list_playbill_subjects
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

    # Reads keep serving at the accepted coordinate.
    assert service_list_playbill_subjects(instance).subjects == ()
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
