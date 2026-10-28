"""next: setup rows sort after problems and count apart; each row says since when."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxible_client.contracts.acquisition_policies import (
    acquisition_policy_path,
    render_acquisition_policy,
)
from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactLifecycle
from cruxible_client.contracts.claims import (
    claim_artifact_digest,
    claim_path,
    claim_statement_digest,
    render_claim,
)
from cruxible_client.contracts.procedures.line_specs import (
    line_spec_digest,
    line_spec_path,
    render_line_spec,
)
from cruxible_client.contracts.triggers import CaptureLandingSchedule
from cruxible_core.indexes.history.history_index import HistoryReader
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.service.discovery import next as next_module
from cruxible_core.service.discovery.next import service_playbill_next
from cruxible_core.service.discovery.orient import OrientCaller, service_playbill_orient
from tests.core_support._claim_authoring_support import (
    ExistingStatementHandoffV1,
    service_propose_playbill_claim,
)
from tests.core_support._knowledge_loop_support import activate, authoring, seed_claims
from tests.test_indexes.test_resolution_contracts import _accept_tree
from tests.test_integration.test_next_closed_loop import _current_claim, _request
from tests.test_procedures.test_line_triggers import SELECTOR, line_world
from tests.test_server.test_playbill_line_run_refusals import _acquisition_policy

#: line_world accepts its Line in a change set stamped at this instant.
LINE_ACCEPTED = "2026-08-24T15:00:00+00:00"


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
    assert setup[0].since == LINE_ACCEPTED

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
    # Each Line has been owed its enablement since the generation that accepted it.
    assert [item.since for item in items] == ["2026-08-24T15:30:00+00:00", LINE_ACCEPTED]

    attention = service_playbill_orient(
        instance, evaluation_time=_request(instance).evaluation_time
    ).attention
    assert attention is not None
    assert (attention.next_items, attention.setup_items, attention.top) == (0, 2, ())
    dumped = attention.model_dump(mode="json")
    assert dumped["setup_items"] == 2


def test_since_is_not_part_of_the_item_id(tmp_path: Path) -> None:
    instance, _line, _owner = _line_world(tmp_path)
    (item,) = _rows(instance)
    assert item.since is not None
    assert item.item_id == next_module.playbill_next_item_id(
        item.model_copy(update={"since": None})
    )
    assert "since" in item.model_dump(mode="json")
    assert "since" not in item.model_copy(update={"since": None}).model_dump(mode="json")


def test_an_edit_before_enablement_keeps_since_and_reading_it_walks_no_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, line, owner = _line_world(tmp_path)
    (before,) = _rows(instance)
    assert before.since == LINE_ACCEPTED

    # An ordinary edit before enablement still owes the same enable, owed since
    # the Line was first accepted; unrelated generations move nothing either.
    edited = line.model_copy(
        update={
            "parameters": {"status": "closed"},
            "lifecycle": ArtifactLifecycle(predecessor_digest=line_spec_digest(line).tagged),
        }
    )
    _accept_lines(instance, owner, edited, timestamp="2026-08-24T15:10:00.000000Z", name="edit")
    for index in range(4):
        tree = instance.tree_at(instance.accepted_coordinate().git_oid)
        policy = _acquisition_policy(f"unrelated-{index}")
        tree[acquisition_policy_path(policy.identity.name)] = render_acquisition_policy(policy)
        _accept_tree(
            instance,
            owner,
            tree,
            timestamp=f"2026-08-24T15:2{index}:00.000000Z",
            proposal_name=f"unrelated-{index}",
        )

    accepted_times: list[str] = []
    seeks: list[int] = []
    real_time = type(instance).accepted_evaluation_time
    real_seek = HistoryReader.last_changed

    def counted_time(self, oid):  # type: ignore[no-untyped-def]
        accepted_times.append(oid)
        return real_time(self, oid)

    def counted_seek(self, paths):  # type: ignore[no-untyped-def]
        seeks.append(len(paths))
        return real_seek(self, paths)

    items = _rows(instance)  # the fold catches up outside the counted stamp
    monkeypatch.setattr(type(instance), "accepted_evaluation_time", counted_time)
    monkeypatch.setattr(HistoryReader, "last_changed", counted_seek)
    stamped = next_module._stamp_since(
        instance,
        tuple(item.model_copy(update={"since": None}) for item in items),
        coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
    )
    (after,) = stamped
    assert after.since == LINE_ACCEPTED and after.item_id == before.item_id
    # The Line's instant was kept when the fold opened the finding: the read
    # pays one keyed lookup, no acceptance-time read and no history seek, however
    # many generations accepted history holds.
    assert accepted_times == [] and seeks == []


def _conflict_world(tmp_path: Path):  # type: ignore[no-untyped-def]
    instance, owner = seed_claims(tmp_path)
    first = _current_claim(instance)
    conflicting = service_propose_playbill_claim(
        instance,
        authoring=authoring("wi-42", "blocked", with_claim_type=False).model_copy(
            update={
                "existing_statement_handoffs": (
                    ExistingStatementHandoffV1(
                        statement_digest=claim_statement_digest(first.statement).tagged,
                        disposition="contradict",
                    ),
                )
            }
        ),
        actor_id="owner",
        proposal_name="since-conflict",
        timestamp="2026-08-24T17:00:03.000000Z",
    )
    activate(instance, owner, conflicting)
    return instance, owner, first


def _restate(instance, owner, claim, *, unrelated: int, timestamp: str) -> None:  # type: ignore[no-untyped-def]
    """Accept a successor of ``claim`` that keeps its value, beside unrelated members."""

    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    successor = claim.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(predecessor_digest=claim_artifact_digest(claim).tagged)
        }
    )
    tree[claim_path(claim.identity.name)] = render_claim(successor)
    for index in range(unrelated):
        policy = _acquisition_policy(f"unrelated-{index}")
        tree[acquisition_policy_path(policy.identity.name)] = render_acquisition_policy(policy)
    _accept_tree(instance, owner, tree, timestamp=timestamp, proposal_name="restate")


def _conflict(rows):  # type: ignore[no-untyped-def]
    (row,) = (item for item in rows if item.reason == "claim_conflicted")
    return row


def test_a_conflicts_since_is_its_newest_input(tmp_path: Path) -> None:
    instance, owner, _first = _conflict_world(tmp_path)
    # Unrelated Claims accepted afterwards do not move the conflict's since.
    for index in range(3):
        unrelated = service_propose_playbill_claim(
            instance,
            authoring=authoring(f"wi-{100 + index}", "ready", with_claim_type=False),
            actor_id="owner",
            proposal_name=f"unrelated-{index}",
            timestamp=f"2026-08-24T17:0{index + 1}:10.000000Z",
        )
        activate(instance, owner, unrelated)
    assert _conflict(_rows(instance)).since == "2026-08-24T17:00:03+00:00"


@pytest.mark.parametrize("unrelated", [2, 20])
def test_a_conflicts_since_reads_no_change_set_however_large_its_accepting_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unrelated: int
) -> None:
    from cruxible_core.indexes.history import history_index
    from cruxible_core.ledger import recovery

    instance, owner, first = _conflict_world(tmp_path)
    # A contender's newest input lands in a batch with many unrelated members,
    # then a later generation moves it off the retained head.
    _restate(instance, owner, first, unrelated=unrelated, timestamp="2026-08-24T18:00:00.000000Z")
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    later = _acquisition_policy("later")
    tree[acquisition_policy_path(later.identity.name)] = render_acquisition_policy(later)
    _accept_tree(
        instance, owner, tree, timestamp="2026-08-24T19:00:00.000000Z", proposal_name="later"
    )

    conflict = _conflict(_rows(instance))
    assert conflict.since == "2026-08-24T18:00:00+00:00"
    # The released-record cache evicted: an instant read from a recovered
    # generation would load and validate its whole change set again.
    recovery.reset_released_records()

    parsed: list[int] = []
    seeks: list[int] = []
    real_seek = HistoryReader.last_changed

    def counting(real):  # type: ignore[no-untyped-def]
        def parse(raw, **kwargs):  # type: ignore[no-untyped-def]
            record = real(raw, **kwargs)
            parsed.append(len(record.members))
            return record

        return parse

    def counted_seek(self, paths):  # type: ignore[no-untyped-def]
        seeks.append(len(paths))
        return real_seek(self, paths)

    for module in (recovery, history_index):
        monkeypatch.setattr(
            module, "parse_change_set_record", counting(module.parse_change_set_record)
        )
    monkeypatch.setattr(HistoryReader, "last_changed", counted_seek)
    (restamped,) = next_module._stamp_since(
        instance,
        (conflict.model_copy(update={"since": None}),),
        coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
    )
    assert restamped.since == conflict.since
    # One seek per input (two contenders and their ClaimType); each instant
    # comes from its generation's small memoized facts, so no change-set
    # record, and none of its members, is parsed whatever the batch carried.
    contenders = [
        identity for identity in conflict.related_identities if identity.startswith("Claim:")
    ]
    assert seeks == [len(contenders) + 1]
    assert parsed == []
