"""An ``unsure`` examined attestation holds a contested row until its basis changes.

An agent that examined a Claim and will not force a judgment it is not
confident in leaves the state contested. The row is parked while what the agent
looked at is unchanged, and comes back the moment it is not.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from cruxible_client.contracts.captures import build_working_selection_capture
from cruxible_client.contracts.claims import claim_statement_digest
from cruxible_core.indexes.projection import AcceptedCoordinate
from cruxible_core.service.discovery.next import (
    DEFAULT_UNSURE_HOLD,
    PlaybillNextRequestV2,
    service_playbill_next,
)
from cruxible_core.service.evidence.claim_attestations import service_append_claim_attestation
from tests.core_support._claim_authoring_support import (
    ExistingStatementHandoffV1,
    service_propose_playbill_claim,
)
from tests.core_support._knowledge_loop_support import activate, authoring, seed_claims
from tests.test_authoring.test_authoring_existing_capture import shared_capture_world
from tests.test_claims.test_claim_attestation_service import RECORDED_AT
from tests.test_claims.test_claim_attestation_service import _request as _attestation
from tests.test_integration.test_next_closed_loop import (
    EVALUATION_TIME,
    _access,
    _current_claim,
    _foreign_world,
    _freshness_world,
)


def _next(instance, *, at: datetime = EVALUATION_TIME):  # type: ignore[no-untyped-def]
    return service_playbill_next(
        instance,
        request=PlaybillNextRequestV2(
            at=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
            evaluation_time=at,
            access_profile=_access(),
        ),
    )


def _rows(result, reason: str, subject: str | None = None):  # type: ignore[no-untyped-def]
    return [
        item
        for item in result.items
        if item.reason == reason and (subject is None or item.subject_identity == subject)
    ]


def _attest(  # type: ignore[no-untyped-def]
    instance,
    owner,
    claim,
    root: Path,
    *,
    stance: str = "unsure",
    at: datetime,
    valid_until: datetime | None = None,
    captures: tuple[str, ...] | None = None,
    basis: str = "examined_existing",
) -> None:
    service_append_claim_attestation(
        instance,
        request=_attestation(
            instance,
            owner,
            claim.identity.name,
            root,
            basis=basis,
            stance=stance,
            captures=() if captures is None and basis == "examined_existing" else captures,
            attested_at=at,
            valid_until=valid_until,
        ),
        actor_id="owner",
        recorded_at=at,
    )


def test_a_conflict_is_held_only_once_every_contender_was_examined(tmp_path: Path) -> None:
    instance, owner = seed_claims(tmp_path)
    first = _current_claim(instance)
    activate(
        instance,
        owner,
        service_propose_playbill_claim(
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
            proposal_name="holds-conflict",
            timestamp="2026-08-24T17:00:03.000000Z",
        ),
    )
    (conflict,) = _rows(_next(instance), "claim_conflicted")
    first, second = (
        claim
        for claim in _all_claims(instance)
        if claim.identity.qualified in conflict.repair.arguments["claim_ids"]
    )

    _attest(instance, owner, first, tmp_path, at=EVALUATION_TIME - timedelta(minutes=2))
    # One contender examined is not the conflict examined.
    assert _rows(_next(instance), "claim_conflicted")
    assert _next(instance).status.held == 0

    _attest(instance, owner, second, tmp_path, at=EVALUATION_TIME - timedelta(minutes=1))
    held = _next(instance)
    assert not _rows(held, "claim_conflicted")
    assert held.status.held == 1

    # Changing one's mind ends that hold, and the conflict is back.
    _attest(instance, owner, second, tmp_path, stance="support", at=EVALUATION_TIME)
    assert _rows(_next(instance), "claim_conflicted")


def _all_claims(instance):  # type: ignore[no-untyped-def]
    from cruxible_core.service.claims.claims import _claim_from_view, service_list_playbill_claims

    return tuple(_claim_from_view(view) for view in service_list_playbill_claims(instance).claims)


def test_an_uncovered_hold_lapses_after_the_default_or_its_own_validity(tmp_path: Path) -> None:
    instance, owner, *_rest = _foreign_world(tmp_path, bind=False)
    claim = _current_claim(instance)
    subject = claim.identity.qualified
    assert _rows(_next(instance), "claim_uncovered", subject)

    _attest(instance, owner, claim, tmp_path, at=EVALUATION_TIME)
    assert not _rows(_next(instance), "claim_uncovered", subject)
    assert _rows(
        _next(instance, at=EVALUATION_TIME + DEFAULT_UNSURE_HOLD), "claim_uncovered", subject
    )

    # An explicit validity window replaces the default.
    _attest(
        instance,
        owner,
        claim,
        tmp_path,
        at=EVALUATION_TIME + timedelta(seconds=1),
        valid_until=EVALUATION_TIME + timedelta(days=2),
    )
    assert not _rows(_next(instance, at=EVALUATION_TIME + timedelta(days=1)), "claim_uncovered")
    assert _rows(
        _next(instance, at=EVALUATION_TIME + timedelta(days=3)), "claim_uncovered", subject
    )


def test_a_stale_evidence_hold_covers_only_expiries_it_could_have_seen(tmp_path: Path) -> None:
    instance, owner = _freshness_world(tmp_path)
    at_expiry = datetime(2026, 8, 16, 20, 0, 10, tzinfo=UTC)
    claim = _current_claim(instance)
    subject = claim.identity.qualified
    (row,) = _rows(_next(instance, at=at_expiry), "claim_stale_evidence", subject)
    expired_at = datetime.fromisoformat(row.detail["last_expired_at"].replace("Z", "+00:00"))

    # Examined before the evidence expired: that hold did not see this row.
    _attest(instance, owner, claim, tmp_path, at=expired_at - timedelta(seconds=1))
    assert _rows(_next(instance, at=at_expiry), "claim_stale_evidence", subject)

    _attest(instance, owner, claim, tmp_path, at=at_expiry)
    held = _next(instance, at=at_expiry)
    assert not _rows(held, "claim_stale_evidence", subject)
    assert held.status.held == 1


def test_an_unreviewed_capture_is_held_until_newer_evidence_arrives(tmp_path: Path) -> None:
    (instance, owner, _actor, _first, claim_id, _coordinator, _payload) = shared_capture_world(
        tmp_path
    )
    claim = next(claim for claim in _all_claims(instance) if claim.identity.name == claim_id)
    first = _capture(instance, claim_id, b"first new observation")
    _attest(
        instance,
        owner,
        claim,
        tmp_path,
        basis="new_capture",
        captures=(first,),
        at=RECORDED_AT,
    )
    after = RECORDED_AT + timedelta(hours=1)
    assert _rows(_next(instance, at=after), "claim_new_evidence_unreviewed")

    _attest(instance, owner, claim, tmp_path, at=RECORDED_AT + timedelta(minutes=1))
    assert not _rows(_next(instance, at=after), "claim_new_evidence_unreviewed")

    second = _capture(instance, claim_id, b"second new observation")
    _attest(
        instance,
        owner,
        claim,
        tmp_path,
        basis="new_capture",
        captures=(second,),
        at=RECORDED_AT + timedelta(minutes=2),
    )
    # The capture the hold never saw comes back; the one it saw stays parked.
    (row,) = _rows(_next(instance, at=after), "claim_new_evidence_unreviewed")
    assert row.detail["capture_digest"] == second


def _capture(instance, claim_id: str, selected: bytes) -> str:  # type: ignore[no-untyped-def]
    source = instance.body_store().store(selected)
    return build_working_selection_capture(
        store=instance.body_store(),
        actor_id="owner",
        claim_id=claim_id,
        rationale="A new independent source observation awaits adjudication.",
        observed_at=RECORDED_AT,
        accepted_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        source_id="repo.work-items",
        coordinate={
            "source_byte_length": len(selected),
            "source_content_digest": source.digest,
        },
        selector={"anchor": "new", "start_byte": 0, "end_byte": len(selected)},
        selected_content=selected,
    ).capture_digest


def test_a_hold_read_before_a_later_change_of_mind_still_holds_then(tmp_path: Path) -> None:
    instance, owner, *_rest = _foreign_world(tmp_path, bind=False)
    claim = _current_claim(instance)
    subject = claim.identity.qualified
    unsure_at = EVALUATION_TIME - timedelta(minutes=10)
    _attest(instance, owner, claim, tmp_path, at=unsure_at)
    _attest(instance, owner, claim, tmp_path, stance="support", at=unsure_at + timedelta(minutes=1))

    # Before the support, the unsure hold was in force; after it, it is not.
    between = _next(instance, at=unsure_at + timedelta(seconds=30))
    assert not _rows(between, "claim_uncovered", subject)
    assert between.status.held == 1
    assert _rows(_next(instance), "claim_uncovered", subject)


def test_a_later_statement_never_changes_which_one_held_at_an_earlier_time(
    tmp_path: Path,
) -> None:
    instance, owner, *_rest = _foreign_world(tmp_path, bind=False)
    claim = _current_claim(instance)
    subject = claim.identity.qualified
    base = EVALUATION_TIME - timedelta(hours=1)
    # Appended in this order, asserted out of it: the later append is the
    # principal's latest word, as the folded door reads it.
    _attest(instance, owner, claim, tmp_path, at=base + timedelta(minutes=10))
    _attest(instance, owner, claim, tmp_path, stance="support", at=base + timedelta(minutes=5))
    read_at = base + timedelta(minutes=20)
    before = _next(instance, at=read_at)
    assert _rows(before, "claim_uncovered", subject) and before.status.held == 0

    # A statement asserted after the read time forces the whole-chain read,
    # which must choose the same statement the fold did.
    _attest(instance, owner, claim, tmp_path, stance="support", at=base + timedelta(minutes=30))
    after = _next(instance, at=read_at)
    assert _rows(after, "claim_uncovered", subject) and after.status.held == 0
