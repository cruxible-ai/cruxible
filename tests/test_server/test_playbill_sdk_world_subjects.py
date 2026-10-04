"""Which Subjects a World knows, against a real daemon: live, complete, at one coordinate.

A World lists Subjects through the served ``query`` verb. These run the SDK
over an in-process HTTP app on a real instance, because what is under test is
what the service's queries bind: retired Subject shells, and the server's
result ceiling.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cruxible_client.authoring.sdk import Playbill
from cruxible_client.authoring.sdk_types import AbsentSubject
from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.compact_query import PlaybillQueryRequest
from cruxible_client.contracts.subjects import (
    parse_subject,
    render_subject,
    subject_digest,
    subject_path,
)
from cruxible_client.transport.http import CruxibleClient
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
from cruxible_core.service.discovery import compact_query as compact_module
from tests.core_support._write_support import KIND, seed_write_vocabulary


def _retire_subject(instance: PlaybillInstance, subject_id: str, *, actor_id: str) -> None:
    """Accept one change set that retires a Subject shell, through the ordinary path."""

    base = instance.accepted_coordinate()
    tree = instance.tree_at(base.git_oid)
    path = subject_path(KIND, subject_id)
    shell = parse_subject(tree[path], path=path)
    tree[path] = render_subject(
        shell.model_copy(
            update={
                "lifecycle": ArtifactLifecycle(
                    state="retired", predecessor_digest=subject_digest(shell).tagged
                )
            }
        )
    )
    proposed = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id=actor_id),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/{actor_id}/retire-{subject_id}",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=tree,
        timestamp="2026-09-29T11:59:30.000000Z",
    )
    assert proposed.candidate is not None, proposed.evaluation
    receipt = service_activate_playbill_proposal(
        instance, proposal_id=proposed.admission.proposal_id, activated_by=actor_id
    )
    assert receipt.status == "accepted"


@pytest.fixture
def pb(playbill_http: tuple[TestClient, str, Path], tmp_path: Path) -> Playbill:
    """wi-1, wi-2 and wi-3 accepted, then wi-3 retired."""

    client, instance_id, _key = playbill_http
    actor = client.get(f"/api/v1/{instance_id}/playbill/whoami").json()["actor_id"]
    instance = get_playbill_manager().get(instance_id)
    seed_write_vocabulary(instance, actor_id=actor)
    _retire_subject(instance, "wi-3", actor_id=actor)
    transport = CruxibleClient(base_url="http://testserver")
    transport._client._client = client  # type: ignore[attr-defined]  # noqa: SLF001
    workspace = tmp_path / "sdk-workspace"
    workspace.mkdir()
    return Playbill._from_client(  # noqa: SLF001
        transport,
        instance_id=instance_id,
        workspace=workspace,
        clock=lambda: datetime(2026, 9, 29, 12, tzinfo=UTC),
    )


def _query(pb: Playbill, **fields: object) -> object:
    return pb._client.query_playbill(  # noqa: SLF001
        pb._instance_id,  # noqa: SLF001
        request=PlaybillQueryRequest.model_validate(fields),
    )


def test_a_world_does_not_resurrect_a_retired_subject(pb: Playbill) -> None:
    world = pb.world()
    items = world.project.work_item

    assert items.subject_ids == ("wi-1", "wi-2")
    assert "wi-3" not in items
    with pytest.raises(AbsentSubject) as absent:
        items["wi-3"]
    assert absent.value.subject_id == "wi-3"


def test_query_lists_live_subjects_and_marks_retired_ones_on_request(pb: Playbill) -> None:
    live = _query(pb, kind=KIND, select=["subject_id"])
    assert [row["subject_id"] for row in live.rows] == ["wi-1", "wi-2"]  # type: ignore[attr-defined]
    assert all("lifecycle" not in row for row in live.rows)  # type: ignore[attr-defined]

    every = _query(pb, kind=KIND, select=["subject_id"], status=["live", "retired"])
    assert [(row["subject_id"], row["lifecycle"]) for row in every.rows] == [  # type: ignore[attr-defined]
        ("wi-1", "live"),
        ("wi-2", "live"),
        ("wi-3", "retired"),
    ]


def test_a_world_reads_every_subject_past_the_server_ceiling(
    pb: Playbill, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A capped answer continues as a new window; it is never taken as complete."""

    monkeypatch.setattr(compact_module, "COMPACT_QUERY_MAX_RESULTS", 1)
    capped = _query(pb, kind=KIND, select=["subject_id"])
    assert capped.capped == ("max_results=1",)  # type: ignore[attr-defined]

    items = pb.world().project.work_item
    assert items.subject_ids == ("wi-1", "wi-2")
    assert items["wi-2"].address == f"{KIND}/wi-2"


def test_a_retired_subject_takes_no_place_under_the_ceiling(
    pb: Playbill,
    playbill_http: tuple[TestClient, str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live-only answer under a ceiling of one is the first live Subject, not empty."""

    client, instance_id, _key = playbill_http
    actor = client.get(f"/api/v1/{instance_id}/playbill/whoami").json()["actor_id"]
    _retire_subject(get_playbill_manager().get(instance_id), "wi-1", actor_id=actor)
    monkeypatch.setattr(compact_module, "COMPACT_QUERY_MAX_RESULTS", 1)

    first = _query(pb, kind=KIND, select=["subject_id"])

    assert [row["subject_id"] for row in first.rows] == ["wi-2"]  # type: ignore[attr-defined]
    assert first.capped == ()  # type: ignore[attr-defined]
