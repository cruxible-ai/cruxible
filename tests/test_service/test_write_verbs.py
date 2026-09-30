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
from cruxible_core.service.proposals.proposals import service_list_playbill_proposals
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


@pytest.mark.parametrize(
    ("field", "value"),
    [("labels", "urgent"), ("governs", WI2)],
    ids=["literal", "relation"],
)
def test_an_add_beside_the_retirement_of_the_same_value_states_it_anew(
    instance: PlaybillInstance, field: str, value: str
) -> None:
    """A value retired in the same write is not live, so adding it back is a real add."""

    old = _write(instance, _add(WI1, field, value)).changes[0].claim
    outcome = _write(instance, {"op": "retire", "target": old}, _add(WI1, field, value))
    assert outcome.status == "accepted", outcome
    retired, added = outcome.changes
    assert retired.claim == old
    assert not added.already_live and added.claim not in (None, old)
    assert outcome.proposal is not None
    assert _values(instance, WI1, field) == [value]


@pytest.mark.parametrize(
    ("options", "status"),
    [({"dry_run": True}, "would_accept"), ({"accept": "never"}, "awaiting_approval")],
    ids=["dry_run", "accept_never"],
)
def test_an_already_live_add_keeps_its_verdict_beside_a_pending_change(
    instance: PlaybillInstance, options: dict[str, Any], status: str
) -> None:
    """R05 holds for a no-op member of a batch that is not yet accepted."""

    live = _write(instance, _add(WI1, "labels", "urgent")).changes[0]
    assert live.verdict == "uncovered"
    outcome = _write(
        instance, _add(WI1, "labels", "urgent"), _set(WI1, "status", "ready"), **options
    )
    assert outcome.status == status, outcome
    present, fresh = outcome.changes
    assert present.already_live and present.claim == live.claim
    assert (present.verdict, fresh.verdict) == ("uncovered", "supported")
    (warning,) = outcome.warnings
    assert (warning.change, warning.claim, warning.verdict) == (0, live.claim, "uncovered")


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


class _Sent(Exception):
    def __init__(self, request: Any) -> None:
        self.request = request


class _Recorder:
    """A client that keeps the request the SDK would send, and sends nothing."""

    def playbill_set(self, _instance_id: str, *, request: Any) -> Any:
        raise _Sent(request)

    def playbill_write(self, _instance_id: str, *, request: Any) -> Any:
        raise _Sent(request)


def test_every_sdk_evidence_repair_runs_against_the_real_builders(
    instance: PlaybillInstance, tmp_path: Path
) -> None:
    """The rendered repair is a call the SDK takes, and it sends the change it names."""

    from cruxible_client.authoring.sdk import Playbill
    from cruxible_client.contracts.write import CaptureEvidence

    digest = "sha256:" + "b" * 64
    outcome = _write(
        instance,
        _set(WI1, "measured", 3),
        _add(WI1, "labels", "urgent"),
        surface="sdk",
    )
    assert outcome.status == "accepted", outcome
    assert [item.change for item in outcome.warnings] == [0, 1]
    for warning in outcome.warnings:
        assert warning.repair is not None
        placeholder = f"<digest of a Capture under {REPORTS.identity.name}>"
        assert placeholder in warning.repair
        pb = Playbill(
            client=_Recorder(),  # type: ignore[arg-type]
            instance_id="inst",
            workspace=tmp_path,
            access_profile="governed_write",  # type: ignore[arg-type]
            clock=None,
        )
        with pytest.raises(_Sent) as sent:
            exec(  # noqa: S102 - the repair is the code under test
                warning.repair.replace(placeholder, digest),
                {"pb": pb, "CaptureEvidence": CaptureEvidence},
            )
        request = sent.value.request
        assert request.because == "The writer checked it."
        (change,) = request.changes if hasattr(request, "changes") else (request.change(),)
        written = outcome.changes[warning.change]
        assert (change.op, change.subject, change.field) == (
            written.op,
            written.subject,
            written.field,
        )
        assert change.evidence == CaptureEvidence(capture=digest)


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


@pytest.mark.parametrize("slot", ["replaced", "filled"])
@pytest.mark.parametrize("pinned", [False, True], ids=["unpinned", "pinned"])
def test_a_competing_set_between_planning_and_submit_refuses_leaving_nothing_to_activate(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch, pinned: bool, slot: str
) -> None:
    """The slot is held to the planned coordinate through admission, not only before it."""

    subject = WI1 if slot == "replaced" else WI2
    _write(instance, _set(WI1, "status", "ready"))
    read_at = instance.accepted_coordinate().git_oid
    real = write_verbs._coordinator
    interleaved: list[WriteOutcome | None] = []

    def coordinator(target: PlaybillInstance, claim_ids: Any) -> Any:
        # Planning is done; a competing set lands before this write is admitted.
        if not interleaved:
            interleaved.append(None)
            interleaved[0] = _write(
                target, _set(subject, "status", "blocked"), because="Someone else."
            )
        return real(target, claim_ids)

    monkeypatch.setattr(write_verbs, "_coordinator", coordinator)
    options = {"at": read_at} if pinned else {}
    outcome = _write(instance, _set(subject, "status", "done"), **options)
    assert interleaved[0] is not None and interleaved[0].status == "accepted"
    refusal = _refusal(outcome)
    assert refusal.code == "playbill.write.slot_changed", refusal
    assert "'blocked'" in refusal.message
    assert _values(instance, subject, "status") == ["blocked"]
    # Nothing this write proposed is left for anyone to activate.
    assert service_list_playbill_proposals(instance, status="open").entries == ()


@pytest.mark.parametrize("pinned", [False, True], ids=["unpinned", "pinned"])
def test_a_retire_by_slot_whose_slot_gained_a_contender_before_admission_is_withdrawn(
    instance: PlaybillInstance, monkeypatch: pytest.MonkeyPatch, pinned: bool
) -> None:
    """Admission passes (the retired Claim did not move), so the proposal is withdrawn."""

    _write(instance, _set(WI1, "status", "ready"))
    read_at = instance.accepted_coordinate().git_oid
    real = write_verbs._coordinator
    interleaved: list[bool] = []

    def coordinator(target: PlaybillInstance, claim_ids: Any) -> Any:
        if not interleaved:
            interleaved.append(True)
            contender = _write(target, _set(WI1, "status", "blocked", contend=True))
            assert contender.status == "accepted", contender
        return real(target, claim_ids)

    monkeypatch.setattr(write_verbs, "_coordinator", coordinator)
    options = {"at": read_at} if pinned else {}
    outcome = _write(
        instance, {"op": "retire", "target": {"subject": WI1, "field": "status"}}, **options
    )
    assert _refusal(outcome).code == "playbill.write.slot_changed"
    assert outcome.proposal is not None and outcome.proposal.state == "withdrawn"
    assert _values(instance, WI1, "status") == ["blocked", "ready"]
    assert service_list_playbill_proposals(instance, status="open").entries == ()


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

    An acceptance queues advisory review-ref upkeep on a worker thread; that is
    not the dry run's, so it is settled first. The derived history index is
    deliberately left as it is: after an acceptance it is behind the head, and a
    dry run must leave it that way.
    """

    instance.settled_workspace_advertisement()
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


@pytest.mark.parametrize("cache", ["cold", "behind_head"])
def test_a_dry_run_leaves_a_cold_or_behind_history_index_as_it_found_it(
    instance: PlaybillInstance, cache: str
) -> None:
    """The dry run's history reads build nothing on disk, however stale the index."""

    read_at = instance.accepted_coordinate().git_oid
    _write(instance, _set(WI1, "status", "ready"))
    instance.settled_workspace_advertisement()
    kept = {path: path.read_bytes() for path in instance.root.rglob("history.sqlite3*")}
    assert kept
    if cache == "behind_head":
        _write(instance, _set(WI2, "status", "ready"))
        instance.settled_workspace_advertisement()
    for path in instance.root.rglob("history.sqlite3*"):
        path.unlink()
    if cache == "behind_head":
        # The index as it stood one generation ago: behind the head.
        for path, data in kept.items():
            path.write_bytes(data)
    before = _snapshot(instance)
    preview = _write(
        instance,
        _set(WI3, "status", "done"),
        _add(WI1, "governs", WI2),
        at=read_at,
        dry_run=True,
    )
    assert preview.status == "would_accept", preview
    assert _snapshot(instance) == before
    stale = _write(instance, _set(WI1, "status", "done"), at=read_at, dry_run=True)
    assert _refusal(stale).code == "playbill.write.slot_changed"
    assert _snapshot(instance) == before


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


def test_a_contest_row_offers_one_keep_option_per_contender_and_picks_none(
    instance: PlaybillInstance,
) -> None:
    """The next row names no winner: each option keeps one contender and retires the rest."""

    from datetime import UTC, datetime

    from cruxible_client.contracts.captures import CanonicalDurationV1
    from cruxible_core.coverage.contracts import CoverageAccessProfileV1
    from cruxible_core.indexes.projection import AcceptedCoordinate
    from cruxible_core.service.discovery.next import PlaybillNextRequestV1, service_playbill_next

    first = _write(instance, _set(WI1, "status", "ready")).changes[0].claim
    second = _write(instance, _set(WI1, "status", "blocked", contend=True)).changes[0].claim

    def conflict_rows() -> list[Any]:
        request = PlaybillNextRequestV1(
            at=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
            evaluation_time=datetime.now(UTC),
            access_profile=CoverageAccessProfileV1(
                profile_id="write-contest", permitted_access_classes=("instance", "public")
            ),
            expiring_within=CanonicalDurationV1(microseconds=604_800_000_000),
        )
        return [
            item
            for item in service_playbill_next(instance, request=request).items
            if item.reason == "claim_conflicted"
        ]

    (row,) = conflict_rows()
    repair = row.repair
    assert repair.operation == "playbill.write"
    assert repair.command is None  # no single command: the choice is the caller's
    assert "changes" not in repair.arguments
    options = {item["keep"]: item["changes"] for item in repair.arguments["options"]}
    assert options == {
        first: [{"op": "retire", "target": second}],
        second: [{"op": "retire", "target": first}],
    }

    # Each option is a runnable write; running one resolves the contest.
    outcome = _write(instance, *options[second], because="The later reading is right.")
    assert outcome.status == "accepted", outcome
    assert conflict_rows() == []
    assert _values(instance, WI1, "status") == ["blocked"]
