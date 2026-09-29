"""The write verbs over accepted state: set, add and retire, lowered onto one change set."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.authoring.models import (
    AuthoringClaimStatementV1,
    ChangeSetAuthoringPayloadV1,
    ClaimAuthoringPayloadV2,
    ClaimDependencyDraftsV1,
    SelfSourceBodyV1,
)
from cruxible_client.contracts.captures import COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT
from cruxible_client.contracts.claims import LiteralClaimObject
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_client.contracts.subjects import subject_path
from cruxible_client.contracts.write import PlaybillWriteRequestV1, WriteOutcome
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.authoring import write_verbs
from cruxible_core.service.authoring.write_verbs import WriteCaller, service_playbill_write
from cruxible_core.service.discovery.query_values import read_live_values
from tests.core_support._write_support import (
    CLAIM_TYPES,
    KIND,
    OWNER,
    REPORTS,
    caller,
    seed_write_surface,
)

WI1 = f"{KIND}/wi-1"
WI2 = f"{KIND}/wi-2"
WI3 = f"{KIND}/wi-3"


@pytest.fixture
def instance(tmp_path: Path) -> PlaybillInstance:
    return seed_write_surface(tmp_path)[0]


def _write(
    instance: PlaybillInstance,
    *changes: dict[str, Any],
    because: str = "The writer checked it.",
    who: WriteCaller | None = None,
    **options: Any,
) -> WriteOutcome:
    request = PlaybillWriteRequestV1.model_validate(
        {"because": because, "changes": list(changes), **options}
    )
    return service_playbill_write(instance, request=request, caller=who or caller())


def _set(subject: str, field: str, value: object, **extra: object) -> dict[str, Any]:
    return {"op": "set", "subject": subject, "field": field, "value": value, **extra}


def _add(subject: str, field: str, value: object) -> dict[str, Any]:
    return {"op": "add", "subject": subject, "field": field, "value": value}


def _values(instance: PlaybillInstance, subject: str, field: str) -> list[object]:
    kind, subject_id = subject.split("/", 1)
    return sorted(
        str(item.value)
        for item in read_live_values(
            instance,
            instance.accepted_coordinate(),
            subject_paths=(subject_path(kind, subject_id),),
            predicates=(f"{KIND}.{field}",),
        )
    )


def _refusal(outcome: WriteOutcome) -> Any:
    assert outcome.refusal is not None, outcome
    return outcome.refusal


# -- set ----------------------------------------------------------------------


def test_set_states_a_value_then_revises_the_live_claim_without_its_id(
    instance: PlaybillInstance,
) -> None:
    first = _write(instance, _set(WI1, "status", "ready"))
    assert first.status == "accepted", first
    (change,) = first.changes
    assert (change.op, change.field, change.before, change.after) == (
        "set",
        "status",
        None,
        "ready",
    )
    assert change.revises is None and change.claim is not None
    assert first.coordinate.generation == first.base.generation + 1  # type: ignore[union-attr]

    second = _write(instance, _set(WI1, "status", "done"))
    assert second.status == "accepted", second
    (revised,) = second.changes
    assert revised.revises == change.claim and revised.claim == change.claim
    assert (revised.before, revised.after) == ("ready", "done")
    assert _values(instance, WI1, "status") == ["done"]


def test_the_role_is_inferred_when_only_one_is_permitted_and_required_otherwise(
    instance: PlaybillInstance,
) -> None:
    assert _write(instance, _set(WI1, "title", "Tidy the CLI")).status == "accepted"
    refused = _write(instance, _set(WI1, "priority", "high"))
    assert refused.status == "refused"
    assert _refusal(refused).code == "playbill.write.role_required"
    assert set(_refusal(refused).candidates) == {"normative", "observation"}
    assert _write(instance, _set(WI1, "priority", "high", role="normative")).status == "accepted"
    wrong = _write(instance, _set(WI1, "title", "x", role="normative"))
    assert _refusal(wrong).code == "playbill.write.role_not_permitted"
    assert _refusal(wrong).candidates == ("observation",)


def test_a_wrong_enum_member_refuses_listing_the_members(instance: PlaybillInstance) -> None:
    refused = _write(instance, _set(WI1, "status", "dne"))
    refusal = _refusal(refused)
    assert refusal.code == "playbill.write.value_not_member"
    assert "blocked, done, ready" in refusal.message
    assert refusal.candidates == ("done",)
    assert refusal.change == 0 and refusal.field_path == "changes[0].value"


def test_the_claim_law_names_the_enum_members_too(instance: PlaybillInstance) -> None:
    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    payload = ChangeSetAuthoringPayloadV1(
        members=(
            ClaimAuthoringPayloadV2(
                statement=AuthoringClaimStatementV1(
                    subject=SemanticAddress.whole_artifact(subject_path(KIND, "wi-1")),
                    predicate=f"{KIND}.status",
                    object=LiteralClaimObject(value="dne"),
                    role="observation",
                ),
                rationale="Bypass the write verbs.",
                source=SelfSourceBodyV1(content_base64=base64.b64encode(b"dne").decode()),
                dependency_drafts=ClaimDependencyDraftsV1(),
            ),
        )
    )
    result = coordinator.compile(
        actor=OWNER, payload=payload, canonical_timestamp="2026-09-29T12:00:00.000000Z"
    )
    messages = [item.message for item in result.frontier.diagnostics]
    assert any("its members are: blocked, done, ready" in text for text in messages), messages


def test_wrong_names_refuse_with_the_nearest_names(instance: PlaybillInstance) -> None:
    field = _refusal(_write(instance, _set(WI1, "stauts", "done")))
    assert field.code == "playbill.write.unknown_field" and "status" in field.candidates
    assert field.field_path == "changes[0].field"
    kind = _refusal(_write(instance, _set("project.work_itme/wi-1", "status", "done")))
    assert kind.code == "playbill.write.unknown_kind" and KIND in kind.candidates
    full = _write(instance, _set(WI1, f"{KIND}.status", "done"))
    assert full.status == "accepted" and full.changes[0].field == "status"


def test_set_and_add_respect_the_field_cardinality(instance: PlaybillInstance) -> None:
    many = _refusal(_write(instance, _set(WI1, "governs", WI2)))
    assert many.code == "playbill.write.field_is_many"
    single = _refusal(_write(instance, _add(WI1, "status", "done")))
    assert single.code == "playbill.write.field_is_single"


# -- add ----------------------------------------------------------------------


def test_two_adds_on_one_many_valued_field_land_in_one_change_set(
    instance: PlaybillInstance,
) -> None:
    outcome = _write(instance, _add(WI1, "governs", WI2), _add(WI1, "governs", WI3))
    assert outcome.status == "accepted", outcome
    assert outcome.coordinate.generation == outcome.base.generation + 1  # type: ignore[union-attr]
    claims = {item.claim for item in outcome.changes}
    assert len(claims) == 2 and None not in claims
    assert _values(instance, WI1, "governs") == [WI2, WI3]

    head = instance.accepted_coordinate().git_oid
    again = _write(instance, _add(WI1, "governs", WI2))
    assert again.status == "accepted" and again.proposal is None
    (change,) = again.changes
    assert change.already_live and change.claim in claims and change.after == WI2
    assert change.verdict == "supported"
    assert instance.accepted_coordinate().git_oid == head  # no change set was submitted
    assert _write(instance, _add(WI1, "governs", WI2), dry_run=True).status == "would_accept"

    # In a batch, only the new value is submitted.
    mixed = _write(instance, _add(WI1, "governs", WI2), _set(WI1, "status", "ready"))
    assert mixed.status == "accepted" and mixed.proposal is not None
    assert [item.already_live for item in mixed.changes] == [True, False]


def test_without_the_filled_dispositions_the_same_adds_refuse(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dispositions the verbs fill are exactly what the slot law demands."""

    monkeypatch.setattr(write_verbs, "_with_dispositions", lambda member, _claims: member)
    outcome = _write(instance, _add(WI1, "governs", WI2), _add(WI1, "governs", WI3))
    assert outcome.status == "refused"
    assert _refusal(outcome).code == "playbill.authoring.existing_claim_dispositions_incomplete"


def test_a_relation_value_never_creates_the_subject_it_names(instance: PlaybillInstance) -> None:
    refusal = _refusal(_write(instance, _add(WI1, "governs", f"{KIND}/wi-9")))
    assert refusal.code == "playbill.write.value_subject_not_found"
    assert refusal.candidates  # the nearest existing work items
    kind = _refusal(_write(instance, _add(WI1, "governs", "other.kind/x")))
    assert kind.code == "playbill.write.value_kind_not_admitted"
    # A Subject this same write adds may be named.
    linked = _write(
        instance, _set(f"{KIND}/wi-9", "title", "New item"), _add(WI1, "governs", f"{KIND}/wi-9")
    )
    assert linked.status == "accepted", linked
    assert linked.subjects_added == (f"{KIND}/wi-9",)


# -- subjects -------------------------------------------------------------------


def test_a_missing_subject_of_a_known_kind_is_added_in_the_same_change_set(
    instance: PlaybillInstance,
) -> None:
    outcome = _write(instance, _set(f"{KIND}/wi-new", "title", "Brand new"))
    assert outcome.status == "accepted", outcome
    assert outcome.subjects_added == (f"{KIND}/wi-new",)
    assert outcome.coordinate.generation == outcome.base.generation + 1  # type: ignore[union-attr]
    assert _values(instance, f"{KIND}/wi-new", "title") == ["Brand new"]


# -- exact content ----------------------------------------------------------------


def test_exact_content_takes_the_text_as_value_and_as_its_own_evidence(
    instance: PlaybillInstance,
) -> None:
    text = "Rulings are text.\nThis one ends with a newline.\n"
    outcome = _write(instance, _set(WI1, "ruling", text))
    assert outcome.status == "accepted", outcome
    assert outcome.changes[0].after == text
    (live,) = read_live_values(
        instance,
        instance.accepted_coordinate(),
        subject_paths=(subject_path(KIND, "wi-1"),),
        predicates=(f"{KIND}.ruling",),
    )
    assert live.value == "sha256:" + hashlib.sha256(text.encode()).hexdigest()
    revised = _write(instance, _set(WI1, "ruling", "Replaced."))
    assert revised.status == "accepted" and revised.changes[0].before == text
    assert revised.changes[0].revises == outcome.changes[0].claim

    mismatch = _write(
        instance, _set(WI1, "ruling", "Again.", evidence={"kind": "self", "self": "Again.\n"})
    )
    assert _refusal(mismatch).code == "playbill.write.exact_content_evidence_mismatch"


# -- evidence ---------------------------------------------------------------------


def test_self_evidence_a_field_does_not_admit_lands_uncovered_and_says_so(
    instance: PlaybillInstance,
) -> None:
    """R05: the write succeeds, but the outcome names the verdict, the reason and the fix."""

    outcome = _write(instance, _set(WI1, "measured", 3), surface="cli")
    assert outcome.status == "accepted", outcome
    (change,) = outcome.changes
    assert change.verdict == "uncovered"
    (warning,) = outcome.warnings
    assert warning.code == "playbill.write.verdict_not_supported"
    assert (warning.change, warning.claim, warning.verdict) == (0, change.claim, "uncovered")
    assert warning.admitted_contracts == (REPORTS.identity.name,)
    assert warning.used_contract == COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT.identity.name
    assert "evidence not admitted" in warning.message
    assert REPORTS.identity.name in warning.message
    assert warning.repair is not None and "--capture" in warning.repair
    assert outcome.next == warning.repair
    assert _values(instance, WI1, "measured") == ["3"]

    preview = _write(instance, _set(WI2, "measured", 4), dry_run=True)
    assert preview.status == "would_accept"
    assert preview.changes[0].verdict == "uncovered" and len(preview.warnings) == 1

    supported = _write(instance, _set(WI1, "status", "ready"))
    assert supported.changes[0].verdict == "supported" and supported.warnings == ()


def test_evidence_required_waits_for_a_claim_type_flag(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreachable until F19 adds the flag; the refusal it guards names the contracts."""

    assert write_verbs.requires_captured_evidence(CLAIM_TYPES[-1]) is False
    monkeypatch.setattr(write_verbs, "requires_captured_evidence", lambda _claim_type: True)
    refusal = _refusal(_write(instance, _set(WI1, "measured", 3)))
    assert refusal.code == "playbill.write.evidence_required"
    assert REPORTS.identity.name in refusal.candidates
    captured = _write(
        instance,
        _set(WI1, "measured", 3, evidence={"kind": "capture", "capture": "sha256:" + "a" * 64}),
    )
    assert _refusal(captured).code != "playbill.write.evidence_required"


def test_file_evidence_must_be_observed_by_the_writer(instance: PlaybillInstance) -> None:
    unobserved = _refusal(
        _write(
            instance,
            _set(WI1, "title", "x", evidence={"kind": "file", "file": "notes.md#Title"}),
        )
    )
    assert unobserved.code == "playbill.write.file_evidence_unobserved"


def test_explicit_self_evidence_backs_the_value(instance: PlaybillInstance) -> None:
    outcome = _write(
        instance,
        _set(WI1, "title", "Named", evidence={"kind": "self", "self": "The page says so."}),
    )
    assert outcome.status == "accepted", outcome


# -- stale writes (decision a, Q05) ----------------------------------------------


def test_a_set_refuses_when_its_slot_moved_since_the_read_coordinate(
    instance: PlaybillInstance,
) -> None:
    first = _write(instance, _set(WI1, "status", "ready"))
    read_at = first.coordinate.git_oid
    other = _write(instance, _set(WI1, "status", "blocked"), because="Someone else saw it.")
    assert other.status == "accepted"
    # An unrelated slot moving does not refuse.
    assert _write(instance, _set(WI2, "status", "done"), at=read_at).status == "accepted"

    stale = _write(instance, _set(WI1, "status", "done"), at=read_at)
    refusal = _refusal(stale)
    assert refusal.code == "playbill.write.slot_changed"
    assert "'blocked'" in refusal.message and "owner" in refusal.message
    assert f"generation {other.coordinate.generation}" in refusal.message
    assert refusal.candidates == (first.changes[0].claim,)
    assert "contend" in (refusal.repair or "")

    contended = _write(instance, _set(WI1, "status", "done", contend=True), at=read_at)
    assert contended.status == "accepted", contended
    assert contended.changes[0].contenders_created == (first.changes[0].claim,)
    contested = _refusal(_write(instance, _set(WI1, "status", "ready")))
    assert contested.code == "playbill.write.slot_contested"
    assert len(contested.candidates) == 2


def test_resetting_at_the_new_head_replaces_the_value(instance: PlaybillInstance) -> None:
    first = _write(instance, _set(WI1, "status", "ready"))
    second = _write(instance, _set(WI1, "status", "blocked"))
    again = _write(instance, _set(WI1, "status", "done"), at=second.coordinate.git_oid)
    assert again.status == "accepted"
    assert again.changes[0].revises == first.changes[0].claim


# -- retire -------------------------------------------------------------------------


def test_retire_by_claim_id_or_by_slot(instance: PlaybillInstance) -> None:
    status = _write(instance, _set(WI1, "status", "ready")).changes[0].claim
    title = _write(instance, _set(WI1, "title", "Old")).changes[0].claim
    by_id = _write(instance, {"op": "retire", "target": f"Claim:{status}"})
    assert by_id.status == "accepted", by_id
    assert by_id.changes[0].claim == status and by_id.changes[0].before == "ready"
    by_slot = _write(
        instance, {"op": "retire", "target": {"subject": WI1, "field": "title"}}, because="Gone."
    )
    assert by_slot.status == "accepted" and by_slot.changes[0].claim == title
    assert _values(instance, WI1, "status") == [] and _values(instance, WI1, "title") == []

    again = _refusal(_write(instance, {"op": "retire", "target": status}))
    assert again.code == "playbill.write.claim_not_live"
    empty = _refusal(
        _write(instance, {"op": "retire", "target": {"subject": WI1, "field": "title"}})
    )
    assert empty.code == "playbill.write.slot_empty"


def test_retire_names_the_values_of_an_ambiguous_many_valued_slot(
    instance: PlaybillInstance,
) -> None:
    _write(instance, _add(WI1, "governs", WI2), _add(WI1, "governs", WI3))
    refusal = _refusal(
        _write(instance, {"op": "retire", "target": {"subject": WI1, "field": "governs"}})
    )
    assert refusal.code == "playbill.write.slot_ambiguous" and len(refusal.candidates) == 2


def test_one_write_may_retire_one_contender_and_replace_the_other(
    instance: PlaybillInstance,
) -> None:
    first = _write(instance, _set(WI1, "status", "ready")).changes[0].claim
    second = _write(instance, _set(WI1, "status", "blocked", contend=True)).changes[0].claim
    outcome = _write(
        instance,
        _set(WI1, "status", "done"),
        {"op": "retire", "target": second, "because": "It was a mistake."},
    )
    assert outcome.status == "accepted", outcome
    assert outcome.changes[0].revises == first
    assert _values(instance, WI1, "status") == ["done"]
    current = outcome.changes[0].claim
    # Retiring the live Claim while setting the field states a new one beside nothing.
    fresh = _write(instance, _set(WI1, "status", "ready"), {"op": "retire", "target": current})
    assert fresh.status == "accepted", fresh
    assert fresh.changes[0].revises is None and fresh.changes[0].claim != current
    assert _values(instance, WI1, "status") == ["ready"]
    twice = _write(
        instance,
        {"op": "retire", "target": fresh.changes[0].claim},
        {"op": "retire", "target": {"subject": WI1, "field": "status"}},
    )
    assert _refusal(twice).code == "playbill.write.claim_changed_twice"


# -- dry runs (R12) -----------------------------------------------------------------


def _snapshot(instance: PlaybillInstance) -> dict[str, tuple[int, int, str]]:
    """Every file under the managed root, after background ref upkeep has settled.

    An acceptance queues advisory review-ref upkeep on a worker thread, and the
    first history read after one catches the derived history index up. Neither
    is the dry run's -- any read does the second -- so both are settled before
    the store is compared.
    """

    instance.settled_workspace_advertisement()
    with instance.accepted_history_reader() as history:
        assert history.sequence >= 0
    root = instance.root
    found: dict[str, tuple[int, int, str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            data = path.read_bytes()
            found[str(path.relative_to(root))] = (
                len(data),
                path.stat().st_mtime_ns,
                hashlib.sha256(data).hexdigest(),
            )
    return found


def test_a_dry_run_takes_the_write_path_and_writes_nothing(instance: PlaybillInstance) -> None:
    _write(instance, _set(WI1, "status", "ready"))
    before = _snapshot(instance)
    preview = _write(
        instance,
        _set(WI1, "status", "done"),
        _set(f"{KIND}/wi-new", "ruling", "A new ruling nobody stored yet.\n"),
        dry_run=True,
    )
    assert preview.status == "would_accept", preview
    assert _snapshot(instance) == before
    assert preview.subjects_added == (f"{KIND}/wi-new",)
    assert preview.changes[0].before == "ready" and preview.changes[0].revises is not None
    assert preview.proposal is None
    assert preview.next is not None and preview.coordinate.git_oid in preview.next

    refused = _write(instance, _set(WI1, "status", "dne"), dry_run=True)
    assert refused.status == "would_refuse"
    committed = _write(instance, _set(WI1, "status", "done"), at=preview.coordinate.git_oid)
    assert committed.status == "accepted"
    assert committed.changes[0].revises == preview.changes[0].revises


def test_a_dry_run_reports_a_refusal_found_only_by_preflight(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(write_verbs, "_with_dispositions", lambda member, _claims: member)
    before = _snapshot(instance)
    preview = _write(instance, _add(WI1, "governs", WI2), _add(WI1, "governs", WI3), dry_run=True)
    assert preview.status == "would_refuse"
    assert _refusal(preview).code == "playbill.authoring.existing_claim_dispositions_incomplete"
    assert _snapshot(instance) == before


# -- accept (decision d) --------------------------------------------------------------


def test_independent_approval_leaves_the_write_awaiting_the_named_approvers(
    tmp_path: Path,
) -> None:
    instance, _owner = seed_write_surface(tmp_path, self_approval=False)
    preview = _write(instance, _set(WI1, "status", "ready"), dry_run=True)
    assert preview.status == "would_await_approval"
    assert preview.approval is not None and preview.approval.eligible_approvers == ("reviewer",)

    outcome = _write(instance, _set(WI1, "status", "ready"), surface="cli")
    assert outcome.status == "awaiting_approval", outcome
    assert outcome.proposal is not None
    approval = outcome.approval
    assert approval is not None
    assert approval.reason == "independent_approval_required"
    assert approval.eligible_approvers == ("reviewer",)
    pid = outcome.proposal.proposal_id
    assert approval.approve == (
        f"cruxible playbill proposal approve {pid} --signer-id reviewer --key <reviewer.ed25519>"
    )
    assert approval.activate == f"cruxible playbill proposal activate {pid}"
    assert outcome.next == approval.approve
    assert _values(instance, WI1, "status") == []


@pytest.mark.parametrize(
    ("surface", "approve", "activate"),
    [
        (
            "mcp",
            'cruxible_playbill_approve(proposal_id="{pid}", signer_id="reviewer")',
            'cruxible_playbill_activate(proposal_id="{pid}")',
        ),
        (
            "sdk",
            'pb.proposal("{pid}").approve(signer=<reviewer signer>, '
            'reviewed=pb.proposal("{pid}").review())',
            'pb.proposal("{pid}").accept()',
        ),
    ],
)
def test_the_approve_call_is_rendered_for_the_callers_surface(
    tmp_path: Path, surface: str, approve: str, activate: str
) -> None:
    instance, _owner = seed_write_surface(tmp_path, self_approval=False)
    outcome = _write(instance, _set(WI1, "status", "ready"), surface=surface)
    assert outcome.proposal is not None and outcome.approval is not None
    pid = outcome.proposal.proposal_id
    assert outcome.approval.approve == approve.format(pid=pid)
    assert outcome.approval.activate == activate.format(pid=pid)


def test_accept_never_and_a_tier_without_activation_stop_at_the_proposal(
    instance: PlaybillInstance,
) -> None:
    never = _write(instance, _set(WI1, "status", "ready"), accept="never")
    assert never.status == "awaiting_approval"
    assert never.approval is not None and never.approval.reason == "accept_never"
    assert never.approval.approve is None and never.next == never.approval.activate
    tier = _write(instance, _set(WI2, "status", "ready"), who=caller(may_activate=False))
    assert tier.status == "awaiting_approval"
    assert tier.approval is not None and tier.approval.reason == "activation_not_permitted"
    assert _values(instance, WI1, "status") == [] and _values(instance, WI2, "status") == []


def test_an_unaccepted_read_coordinate_refuses_as_an_outcome(instance: PlaybillInstance) -> None:
    outcome = _write(instance, _set(WI1, "status", "ready"), at="f" * 40)
    assert outcome.status == "refused"
    assert _refusal(outcome).code == "playbill.read.coordinate_not_accepted"
