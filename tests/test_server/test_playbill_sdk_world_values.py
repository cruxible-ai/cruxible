"""World.values against a real daemon: every admitted field, and the Claims it serves.

``World.values`` reads Claim values through the served ``query`` verb. These
run the SDK over an in-process HTTP app on a real instance: what is under test
is the request the SDK builds (a kind with more fields than one query may
select) and what the service records about the Claims it served.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from cruxible_client.authoring.sdk import Cruxible
from cruxible_client.contracts.claim_types import claim_type_path, render_claim_type
from cruxible_client.contracts.compact_query import QUERY_MAX_SELECT
from cruxible_client.transport.http import CruxibleClient
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.runtime import playbill_api
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.runtime.playbill_manager import get_playbill_manager
from cruxible_core.service.authoring.documents import service_activate_playbill_proposal
from tests.core_support._write_support import (
    CLAIM_TYPES,
    KIND,
    _claim_type,
    seed_write_vocabulary,
)

WI1 = f"{KIND}/wi-1"
TITLE = f"{KIND}.title"
#: Enough extra string fields that the kind admits more than one query can select.
EXTRA_FIELDS = tuple(f"extra_{index:02d}" for index in range(QUERY_MAX_SELECT + 1))


def _accept_extra_fields(instance: PlaybillInstance, *, actor_id: str) -> None:
    base = instance.accepted_coordinate()
    tree = instance.tree_at(base.git_oid)
    for field in EXTRA_FIELDS:
        claim_type = _claim_type(field, literal_schema={"type": "string"})
        tree[claim_type_path(claim_type.predicate)] = render_claim_type(claim_type)
    proposed = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id=actor_id),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/{actor_id}/wide-vocabulary",
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


def _sdk(client: TestClient, instance_id: str, tmp_path: Path) -> Cruxible:
    transport = CruxibleClient(base_url="http://testserver")
    transport._client._client = client  # type: ignore[attr-defined]  # noqa: SLF001
    workspace = tmp_path / "sdk-workspace"
    workspace.mkdir(exist_ok=True)
    return Cruxible._from_client(  # noqa: SLF001
        transport,
        instance_id=instance_id,
        workspace=workspace,
        clock=lambda: datetime(2026, 9, 29, 12, tzinfo=UTC),
    )


@pytest.fixture
def served(playbill_http: tuple[TestClient, str, Path]) -> tuple[TestClient, str, str]:
    client, instance_id, _key = playbill_http
    actor = client.get(f"/api/v1/{instance_id}/whoami").json()["actor_id"]
    seed_write_vocabulary(get_playbill_manager().get(instance_id), actor_id=actor)
    return client, instance_id, actor


def test_values_reads_every_field_of_a_kind_wider_than_one_select(
    served: tuple[TestClient, str, str], tmp_path: Path
) -> None:
    client, instance_id, actor = served
    _accept_extra_fields(get_playbill_manager().get(instance_id), actor_id=actor)
    pb = _sdk(client, instance_id, tmp_path)
    title = pb.set(WI1, "title", "Tidy the CLI", because="Named in review.")
    last = pb.set(WI1, EXTRA_FIELDS[-1], "the last field", because="Named in review.")
    world = pb.world()
    # Every instance also seeds the builtin semantic.alias and
    # semantic.distinct_from ClaimTypes, and the world keys its work items by
    # the project.work_item.slug ClaimType.
    admitted = len(CLAIM_TYPES) + len(EXTRA_FIELDS) + 3
    assert len(world.predicates) == admitted > QUERY_MAX_SELECT

    values = world.values(subjects=[WI1])

    assert {(item.predicate, item.value, item.claim) for item in values} == {
        (TITLE, "Tidy the CLI", title.changes[0].claim),
        (f"{KIND}.{EXTRA_FIELDS[-1]}", "the last field", last.changes[0].claim),
    }


def test_values_records_each_claim_it_served(
    served: tuple[TestClient, str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Consumption receipts name every Claim a query page served, at its coordinate."""

    client, instance_id, _actor = served
    pb = _sdk(client, instance_id, tmp_path)
    title = pb.set(WI1, "title", "Tidy the CLI", because="Named in review.").changes[0].claim
    status = pb.set(WI1, "status", "ready", because="Checked.").changes[0].claim
    monkeypatch.setenv("CRUXIBLE_CONSUMPTION_RECEIPTS", "on")
    recorded: list[tuple[str, dict[str, Any], tuple[str, ...]]] = []

    def spy(
        _instance: Any, *, context: Any, operation: str, coordinate: Any, artifacts: Any
    ) -> tuple[()]:
        names = tuple(identity.qualified for identity, _digest in artifacts)
        recorded.append((operation, coordinate.model_dump(mode="json"), names))
        return ()

    monkeypatch.setattr(playbill_api, "record_consumption", spy)
    world = pb.world()

    values = world.values(subjects=[WI1])

    assert {item.claim for item in values} == {title, status}
    served = [entry for entry in recorded if entry[0] == "playbill.claim.get"]
    assert {name for _op, _at, names in served for name in names} == {
        f"Claim:{title}",
        f"Claim:{status}",
    }
    assert {str(at["git_oid"]) for _op, at, _names in served} == {world.coordinate.git_oid}

    # A query that names no Claims (claims=False) still served the ones behind its cells.
    recorded.clear()
    page = pb.query(KIND, where=[{"field": "subject_id", "eq": "wi-1"}], select=["title"]).page
    assert "claims" not in page.rows[0]
    assert [names for op, _at, names in recorded if op == "playbill.claim.get"] == [
        (f"Claim:{title}",)
    ]


def test_a_long_value_is_typed_truncated_text_and_reads_whole_where_it_was_previewed(
    served: tuple[TestClient, str, str], tmp_path: Path
) -> None:
    """A preview is a declared type on every read that cuts it, never a loose dict.

    World.values, query rows and Claim cells, get history and the write outcome
    all carry the same TruncatedText; a short value is unchanged. A preview that
    one Claim at one generation backs names that exact read, so it whole-reads
    to the value it previewed even after the value was revised; the documented
    SDK call, ``cx.read_whole(item.value)``, makes it.
    """

    from cruxible_client.authoring.sdk_types import WholeValueUnavailable
    from cruxible_client.contracts.compact_query import QueryClaim
    from cruxible_client.contracts.get_reads import GetHistory
    from cruxible_client.contracts.read_values import GET_SUMMARY_TEXT_MAX_CHARS, TruncatedText

    client, instance_id, _actor = served
    cx = _sdk(client, instance_id, tmp_path)
    first_title = "A title long enough to cut. " * 30
    second_title = "A revised title, also long. " * 30
    assert len(first_title) > GET_SUMMARY_TEXT_MAX_CHARS
    first = cx.set(WI1, "title", first_title, because="Named in review.").changes[0]
    cx.set(WI1, "status", "ready", because="Checked.")

    values = {value.predicate: value for value in cx.world().values(subjects=[WI1])}
    item = values[TITLE]
    assert isinstance(item.value, TruncatedText)
    assert (item.value.preview, item.value.length) == (
        first_title[:GET_SUMMARY_TEXT_MAX_CHARS],
        len(first_title),
    )
    assert item.value.read_whole is not None and item.value.read_whole.ref == item.claim
    assert values[f"{KIND}.status"].value == "ready"
    # The documented whole-value call, exactly as documented.
    assert cx.read_whole(item.value) == first_title
    assert first.after is not None and isinstance(first.after, TruncatedText)
    assert first.after.read_whole is None  # the value this write sent

    second = cx.set(WI1, "title", second_title, because="Renamed.").changes[0]

    # The earlier preview still reads back the value it previewed, not the head's.
    assert cx.read_whole(item.value) == first_title
    # A write's before previews the value it replaced, and reads back that value.
    assert isinstance(second.before, TruncatedText)
    assert cx.read_whole(second.before) == first_title
    # An older revision's preview whole-reads to its own value.
    history = cx.get(item.claim, detail="history").value
    assert isinstance(history, GetHistory)
    by_revision = {revision.revision: revision.value for revision in history.revisions}
    assert all(isinstance(value, TruncatedText) for value in by_revision.values())
    assert cx.read_whole(by_revision[1]) == first_title  # type: ignore[arg-type]
    assert cx.read_whole(by_revision[2]) == second_title  # type: ignore[arg-type]

    page = cx.query(
        KIND, where=[{"field": "subject_id", "eq": "wi-1"}], select=["title"], claims=True
    ).page
    (row,) = page.rows
    cell = row["title"]
    assert isinstance(cell, TruncatedText) and cell.read_whole is None
    with pytest.raises(WholeValueUnavailable):
        cx.read_whole(cell)
    cells = row["claims"]
    assert isinstance(cells, dict)
    (claim,) = cells["title"]
    assert isinstance(claim, QueryClaim) and isinstance(claim.value, TruncatedText)
    assert cx.read_whole(claim.value) == second_title


#: Object values shaped like the summary markers: each must read back as itself.
MARKER_SHAPED = {
    "wi-1": {"preview": "cover", "length": 600},
    "wi-2": {"preview": "cover", "length": 600, "truncated": True},
    "wi-3": {"exact_content": "binary", "content_digest": "sha256:" + "a" * 64},
}


def _accept_object_field(instance: PlaybillInstance, *, actor_id: str) -> None:
    base = instance.accepted_coordinate()
    tree = instance.tree_at(base.git_oid)
    claim_type = _claim_type("meta", literal_schema={"type": "object"})
    tree[claim_type_path(claim_type.predicate)] = render_claim_type(claim_type)
    proposed = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id=actor_id),
        request=ProposalAdmissionRequest(
            target_ref=f"refs/proposals/{actor_id}/object-field",
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


def test_object_values_shaped_like_markers_read_back_as_themselves(
    served: tuple[TestClient, str, str], tmp_path: Path
) -> None:
    """A marker-shaped object Claim is never a TruncatedText or ExactContentRef.

    Through the summary model, the HTTP/SDK round trip, get's card and its CLI
    rendering, a compact query row and Claim cell, and ``World.values``.
    """

    from cruxible_client.contracts.compact_query import QueryClaim
    from cruxible_client.contracts.get_display import get_value_display
    from cruxible_client.contracts.get_reads import GetClaimCard, GetRequest
    from cruxible_client.contracts.read_values import ShownObject, summary_value

    client, instance_id, actor = served
    _accept_object_field(get_playbill_manager().get(instance_id), actor_id=actor)
    pb = _sdk(client, instance_id, tmp_path)
    instance = get_playbill_manager().get(instance_id)
    claims: dict[str, str] = {}
    for subject, value in MARKER_SHAPED.items():
        # The write verbs take scalar values; an object Claim is authored whole.
        submitted = pb.claim(
            subject=f"{KIND}/{subject}",
            predicate=f"{KIND}.meta",
            value=value,
            role="observation",
            rationale="Recorded.",
            self_source="Recorded.",
        ).submit()
        proposal_id = submitted.proposal.proposal_id
        receipt = service_activate_playbill_proposal(
            instance, proposal_id=proposal_id, activated_by=actor
        )
        assert receipt.status == "accepted"
        pb.refresh()
        (claim,) = (
            item.claim
            for item in pb.world().values(subjects=[f"{KIND}/{subject}"])
            if item.predicate == f"{KIND}.meta"
        )
        claims[subject] = claim

    for subject, value in MARKER_SHAPED.items():
        shown = ShownObject(object=value)
        # Model construction and a JSON round trip.
        row = QueryClaim(
            claim=claims[subject],
            value=summary_value(value),
            verdict="supported",
            status="accepted",
            role="observation",
        )
        assert row.value == shown
        assert QueryClaim.model_validate_json(row.model_dump_json()).value == shown
        assert row.model_dump(mode="json")["value"] == {"object": value}
        # The SDK's Claim view holds the value itself.
        assert pb.get(claims[subject]).value.value == value
        # get's card over HTTP, and its CLI rendering.
        card = pb._client.get(  # noqa: SLF001
            instance_id, request=GetRequest(ref=claims[subject])
        ).card
        assert isinstance(card, GetClaimCard) and card.value == shown
        rendered = get_value_display(card.model_dump(mode="json")["value"])
        assert not rendered.truncated and "chars;" not in rendered.text
        assert json.loads(rendered.text) == value
    # A query row cell and its Claim cells.
    page = pb.query(KIND, select=["meta"], claims=True).page
    by_subject = {row["subject_id"]: row for row in page.rows}
    for subject, value in MARKER_SHAPED.items():
        assert by_subject[subject]["meta"] == ShownObject(object=value)
        (cell,) = by_subject[subject]["claims"]["meta"]
        assert cell.value == ShownObject(object=value)
    # World.values.
    values = pb.world().values(subjects=[f"{KIND}/{subject}" for subject in MARKER_SHAPED])
    meta = {item.subject: item.value for item in values if item.predicate == f"{KIND}.meta"}
    assert meta == {
        f"{KIND}/{subject}": ShownObject(object=value) for subject, value in MARKER_SHAPED.items()
    }


def test_a_marker_shaped_object_without_its_discriminator_is_never_a_marker() -> None:
    """Construction from a bare object refuses rather than reading it as a marker."""

    from pydantic import ValidationError

    from cruxible_client.contracts.compact_query import QueryClaim

    with pytest.raises(ValidationError):
        QueryClaim(
            claim="CLM-" + "a" * 32,
            value={"preview": "cover", "length": 600},  # type: ignore[arg-type]
            verdict="supported",
            status="accepted",
            role="observation",
        )
