"""next: setup rows sort after problems and count apart from them."""

from __future__ import annotations

from pathlib import Path

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.procedures.line_specs import (
    line_spec_path,
    render_line_spec,
)
from cruxible_client.contracts.triggers import CaptureLandingSchedule
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.service.discovery.next import service_playbill_next
from cruxible_core.service.discovery.orient import OrientCaller, service_playbill_orient
from tests.test_indexes.test_resolution_contracts import _accept_tree
from tests.test_integration.test_next_closed_loop import _request
from tests.test_procedures.test_line_triggers import SELECTOR, line_world


def _line_world(tmp_path: Path):  # type: ignore[no-untyped-def]
    instance, line, _procedure, owner = line_world(
        tmp_path, CaptureLandingSchedule(event=SELECTOR), with_owner=True
    )
    return instance, line, owner


def _accept_lines(instance, owner, *lines, timestamp: str, name: str) -> None:  # type: ignore[no-untyped-def]
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    tree.update({line_spec_path(line.identity.name): render_line_spec(line) for line in lines})
    _accept_tree(instance, owner, tree, timestamp=timestamp, proposal_name=name)


def _second_line(line, name: str):  # type: ignore[no-untyped-def]
    return line.model_copy(update={"identity": ArtifactIdentity(kind="Line", name=name)})


def _open_proposal(instance, line) -> None:  # type: ignore[no-untyped-def]
    """An admitted, unactivated proposal: work waiting on its author, a real problem row."""

    base = instance.accepted_coordinate()
    tree = instance.tree_at(base.git_oid)
    pending = _second_line(line, "pending-line")
    tree[line_spec_path(pending.identity.name)] = render_line_spec(pending)
    proposed = instance.proposal_service().submit(
        actor=AuthenticatedActor(actor_id="owner"),
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/pending-line", proposed_base_oid=base.git_oid
        ),
        candidate_tree=tree,
        timestamp="2026-08-24T16:00:00.000000Z",
    )
    assert proposed.candidate is not None


def _rows(instance, **caller):  # type: ignore[no-untyped-def]
    return service_playbill_next(instance, request=_request(instance), **caller).items


def test_setup_rows_sort_after_problems_and_count_apart_from_them(tmp_path: Path) -> None:
    instance, line, _owner = _line_world(tmp_path)
    _open_proposal(instance, line)

    items = _rows(instance, caller_principal_id="owner")
    severities = [item.severity for item in items]
    setup = [item for item in items if item.severity == "setup"]
    problems = [item for item in items if item.severity != "setup"]
    assert [item.reason for item in setup] == ["line_not_enabled"]
    assert problems and all(item.reason.startswith("proposal_") for item in problems)
    # Every setup row comes after every problem, whatever its subject sorts as.
    assert severities == [*(item.severity for item in problems), "setup"]

    # orient's attention counts problems only; setup rows are counted apart and
    # never named in `top`, so a fresh instance's choices do not read as trouble.
    attention = service_playbill_orient(
        instance,
        caller=OrientCaller("owner", "admin"),
        evaluation_time=_request(instance).evaluation_time,
    ).attention
    assert attention is not None
    assert attention.next_items == len(problems)
    assert attention.setup_items == 1
    assert not any("line_not_enabled" in line for line in attention.top)


def test_a_fresh_instance_with_only_setup_rows_has_no_problems(tmp_path: Path) -> None:
    instance, line, owner = _line_world(tmp_path)
    _accept_lines(
        instance,
        owner,
        _second_line(line, "second"),
        timestamp="2026-08-24T15:30:00.000000Z",
        name="second-line",
    )

    items = _rows(instance)
    assert [(item.severity, item.reason, item.subject_identity) for item in items] == [
        ("setup", "line_not_enabled", "Line:second"),
        ("setup", "line_not_enabled", "Line:trigger-test"),
    ]

    attention = service_playbill_orient(
        instance, evaluation_time=_request(instance).evaluation_time
    ).attention
    assert attention is not None
    assert (attention.next_items, attention.setup_items, attention.top) == (0, 2, ())
    dumped = attention.model_dump(mode="json")
    assert dumped["setup_items"] == 2
