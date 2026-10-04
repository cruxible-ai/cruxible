"""The reads that replace the cut surfaces: head, orient sections, get refs, query dimensions."""

from __future__ import annotations

import functools
from pathlib import Path

import pytest

from cruxible_client.contracts.write import WriteRequest
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring.write_verbs import service_playbill_write
from cruxible_core.service.discovery.orient import service_playbill_head
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._write_support import KIND, caller, seed_write_surface

_ACCESS = BodyAccessContext(principal_id="reader", can_read_body=True)


def _write(instance: PlaybillInstance, *changes: dict[str, object]) -> list[str]:
    request = WriteRequest.model_validate({"because": "test", "changes": list(changes)})
    outcome = service_playbill_write(instance, request=request, caller=caller())
    assert outcome.status == "accepted", outcome
    return [str(change.claim) for change in outcome.changes]


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> PlaybillInstance:
    instance, _owner = seed_write_surface(tmp_path_factory.mktemp("read-cut"))
    return instance


def test_head_names_the_coordinate_and_generation_and_resolves_at(
    world: PlaybillInstance,
) -> None:
    head = service_playbill_head(world)
    history = world.accepted_history()

    assert head.instance == world.descriptor.instance_id
    assert head.coordinate.git_oid == world.accepted_coordinate().git_oid
    assert head.generation == history[-1].sequence
    earlier = service_playbill_head(world, at=history[0].oid[:12])
    assert earlier.generation == history[0].sequence
    assert earlier.coordinate.git_oid == history[0].oid


def test_orient_pages_the_principal_registry_and_get_reads_one_principal(
    world: PlaybillInstance,
) -> None:
    from cruxible_client.contracts.get_reads import GetRequest
    from cruxible_core.service.discovery.get import service_playbill_get
    from cruxible_core.service.discovery.orient import service_playbill_orient

    result = service_playbill_orient(world, section="principals", surface="mcp")

    ids = [row.principal_id for row in result.principals or ()]
    assert "daemon" in ids and "owner" in ids
    assert result.next[0] == f'cruxible_playbill_get(ref="Principal:{ids[0]}")'
    card = service_playbill_get(world, request=GetRequest(ref="Principal:owner"), access=_ACCESS)
    assert card.kind == "principal" and card.card is not None
    assert card.card.model_dump()["status"] == "active"
    proof = service_playbill_get(
        world, request=GetRequest(ref="Principal:owner", detail="proof"), access=_ACCESS
    )
    assert proof.proof is not None and proof.proof["record"]["principal_id"] == "owner"
    with pytest.raises(Exception) as refused:
        service_playbill_get(world, request=GetRequest(ref="Principal:ownr"), access=_ACCESS)
    assert "Principal:owner" in str(getattr(refused.value, "candidates", "")) or "owner" in str(
        refused.value
    )


def test_orient_pages_policies_in_force_and_get_reads_the_approval_policy(
    world: PlaybillInstance,
) -> None:
    from cruxible_client.contracts.get_reads import GetRequest
    from cruxible_core.service.claims.policies import service_playbill_policies_in_force
    from cruxible_core.service.discovery.get import service_playbill_get
    from cruxible_core.service.discovery.orient import service_playbill_orient

    result = service_playbill_orient(world, section="policies", limit=500)
    rows = result.policies or ()
    expected = service_playbill_policies_in_force(world).policies
    assert [row.model_dump(mode="json") for row in rows] == [
        row.model_dump(mode="json") for row in expected
    ]
    approval = next(row for row in rows if row.policy_kind == "approval_policy")
    assert approval.declaring_artifact_identity == "ApprovalPolicy:instance"

    card = service_playbill_get(
        world, request=GetRequest(ref="ApprovalPolicy:instance"), access=_ACCESS
    )
    assert card.kind == "approval_policy" and card.card is not None
    assert card.card.model_dump()["mode"] == approval.policy["mode"]
    by_path = service_playbill_get(
        world, request=GetRequest(ref="governance/approval-policy.json"), access=_ACCESS
    )
    assert by_path.ref == "ApprovalPolicy:instance"
    history = service_playbill_get(
        world,
        request=GetRequest(ref="ApprovalPolicy:instance", detail="history"),
        access=_ACCESS,
    )
    assert history.history is not None and history.history.revisions


def test_orient_counts_claims_by_status_including_retired(tmp_path: Path) -> None:
    from cruxible_core.service.discovery.orient import service_playbill_orient

    instance, _owner = seed_write_surface(tmp_path)
    first = _write(
        instance, {"op": "set", "subject": f"{KIND}/wi-1", "field": "status", "value": "ready"}
    )
    _write(instance, {"op": "set", "subject": f"{KIND}/wi-2", "field": "status", "value": "done"})
    _write(instance, {"op": "retire", "target": first[0]})

    result = service_playbill_orient(instance)

    assert result.artifacts is not None and result.artifacts.claims is not None
    counts = result.artifacts.claims
    assert counts.retired == 1
    assert counts.accepted + counts.conflicted + counts.overturned + counts.refused == 1
    # The section and kind views carry no counts; only the default map does.
    assert service_playbill_orient(instance, section="documents").artifacts is None


def _contended(tmp_path: Path) -> tuple[PlaybillInstance, str]:
    """A work item whose status slot has a winner and a later, contradicting contender."""

    from tests.core_support._knowledge_loop_support import seed_claims
    from tests.test_integration.test_next_closed_loop import _current_claim
    from tests.test_service.test_get_reads import _contend

    instance, owner = seed_claims(tmp_path)
    first = _current_claim(instance)
    _contend(instance, owner, first, "blocked", "read-cut-conflict")
    return instance, first.identity.name


def _query_at(instance: PlaybillInstance, **fields: object):  # type: ignore[no-untyped-def]
    return _query(instance, **fields)


def _query(instance: PlaybillInstance, **fields: object):  # type: ignore[no-untyped-def]
    from cruxible_client.contracts.compact_query import QueryRequest
    from cruxible_core.service.discovery.compact_query import service_playbill_query

    return service_playbill_query(instance, request=QueryRequest.model_validate(fields))


def test_query_cells_show_the_slot_answer_and_name_each_claims_status(tmp_path: Path) -> None:
    from tests.core_support._knowledge_loop_support import EVALUATION_TIME, SUBJECT_KIND

    instance, winner = _contended(tmp_path)
    # Before the contender's evidence is observed, resolution selects the first.
    where = [{"field": "subject_id", "eq": "wi-42"}]
    _query = functools.partial(_query_at, evaluation_time=EVALUATION_TIME)

    # Live: the slot's answer, as get shows it; the set-aside contender is not a value.
    (row,) = _query(instance, kind=SUBJECT_KIND, where=where, select=["status"]).rows
    assert row["status"] == "ready" and "contested" not in row["flags"]
    assert "claims" not in row

    # Opt in to the Claims resolution set aside, and to each cell's Claims.
    (row,) = _query(
        instance,
        kind=SUBJECT_KIND,
        where=where,
        select=["status"],
        status=["live", "overturned", "refused"],
        claims=True,
    ).rows
    assert sorted(row["status"]) == ["blocked", "ready"]
    cell = {item["value"]: item for item in row["claims"]["status"]}
    assert cell["ready"]["claim"] == winner and cell["ready"]["status"] == "accepted"
    assert cell["blocked"]["status"] in {"overturned", "refused"}
    assert {item["role"] for item in cell.values()} <= {"normative", "observation"}

    # Only the set-aside ones.
    (row,) = _query(
        instance,
        kind=SUBJECT_KIND,
        where=where,
        select=["status"],
        status=["overturned", "refused"],
    ).rows
    assert row["status"] == "blocked"


def test_query_lists_retired_claims_by_status(world: PlaybillInstance) -> None:
    title = _write(
        world, {"op": "set", "subject": f"{KIND}/wi-3", "field": "title", "value": "Old"}
    )
    _write(world, {"op": "retire", "target": title[0]})
    where = [{"field": "subject_id", "eq": "wi-3"}]

    (live,) = _query(world, kind=KIND, where=where, select=["title"]).rows
    assert live["title"] is None
    (row,) = _query(
        world, kind=KIND, where=where, select=["title"], status=["retired"], claims=True
    ).rows
    assert row["title"] == "Old"
    (claim,) = row["claims"]["title"]
    assert claim == {
        "claim": title[0],
        "value": "Old",
        "verdict": "retired",
        "status": "retired",
        "role": claim["role"],
    }


def test_query_flags_show_an_uncovered_verdict(world: PlaybillInstance) -> None:
    from cruxible_core.service.discovery.read_flags import verdict_flags

    assert verdict_flags("uncovered", "accepted") == ("uncovered",)
    assert verdict_flags("supported", "accepted") == ()


def test_status_and_claims_refuse_outside_a_compact_kind_query(world: PlaybillInstance) -> None:
    from cruxible_core.service.read_refusals import ReadRefusalError

    for fields in (
        {"contains": "x", "claims": True},
        {"kind": "ClaimType", "status": ["retired"]},
        {"kind": KIND, "budgets": {"max_results": 1, "max_traversal_depth": 0}},
        {"kind": KIND, "receipt": "full"},
    ):
        with pytest.raises(ReadRefusalError) as refused:
            _query(world, **fields)
        assert refused.value.error_code == "playbill.query.mode_invalid", fields


def test_query_cursors_are_short_and_continue_the_same_listing(world: PlaybillInstance) -> None:
    from cruxible_core.service.list_pages import (
        ListCursorMismatch,
        ListCursorStale,
    )

    first = _query(world, kind=KIND, limit=1)
    assert first.truncated and first.next_cursor is not None
    assert len(first.next_cursor) < 80
    second = _query(world, kind=KIND, limit=1, cursor=first.next_cursor)
    assert second.rows[0]["subject_id"] != first.rows[0]["subject_id"]
    assert second.receipt.coordinate == first.receipt.coordinate
    assert second.receipt.evaluation_time == first.receipt.evaluation_time

    with pytest.raises(ListCursorMismatch):
        _query(world, kind=KIND, limit=1, select=["status"], cursor=first.next_cursor)
    with pytest.raises(ListCursorMismatch):
        _query(world, kind=KIND, limit=1, cursor="not-a-cursor")
    stale = first.next_cursor.rsplit(".", 2)
    with pytest.raises(ListCursorStale):
        _query(world, kind=KIND, limit=1, cursor=f"{stale[0]}.000000000000.{stale[2]}")


@pytest.fixture(scope="module")
def named(tmp_path_factory: pytest.TempPathFactory) -> PlaybillInstance:
    from tests.test_service.test_playbill_orient import _seeded_world

    return _seeded_world(tmp_path_factory.mktemp("read-cut-named"))


def test_a_named_query_takes_budgets_up_to_its_maximum_and_a_full_receipt(
    named: PlaybillInstance,
) -> None:
    from cruxible_core.service.discovery.query import service_run_playbill_query
    from cruxible_core.service.read_refusals import ReadRefusalError
    from tests.core_support._knowledge_loop_support import QUERY_NAME

    compact = _query(named, name=QUERY_NAME)
    assert compact.receipt.replay is None

    full = _query(named, name=QUERY_NAME, receipt="full")
    replay = full.receipt.replay
    assert replay is not None
    assert replay.definition_path.endswith(".json") and replay.result.verdict == "completed"
    run = service_run_playbill_query(
        named,
        name=QUERY_NAME,
        evaluation_time=full.receipt.evaluation_time,
    )
    # The replay receipt is the run's own result and execution receipt.
    assert replay.result.model_dump(mode="json") == run.result.model_dump(mode="json")
    assert replay.execution.model_dump(mode="json") == run.receipt.model_dump(mode="json")
    assert [row.bindings for row in replay.result.rows] == [row.bindings for row in run.result.rows]

    one = _query(
        named, name=QUERY_NAME, budgets={"max_results": 1, "max_traversal_depth": 0}, receipt="full"
    )
    assert len(one.rows) == 1 and one.receipt.replay is not None
    assert one.receipt.replay.result.budgets.max_results == 1
    with pytest.raises(ReadRefusalError) as refused:
        _query(named, name=QUERY_NAME, budgets={"max_results": 51, "max_traversal_depth": 0})
    assert "ceiling" in str(refused.value) or "maximum" in str(refused.value)
