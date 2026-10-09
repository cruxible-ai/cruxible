"""Neutral resurfacing of earlier refusals of the same statement on the same evidence.

A proposed Claim whose statement and cited Captures match a Claim retired (any
reason) or a Claim of a proposal a reviewer rejected carries a typed receipt of
that decision, exactly as recorded. Nothing refuses it; new evidence matches
nothing; the lookup is keyed, so its work does not grow with history.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.authoring.models import (
    AuthoringClaimStatement,
    ChangeSetAuthoringPayload,
    ClaimAuthoringPayload,
    ClaimDependencyDrafts,
    ExistingCaptureCitationSource,
)
from cruxible_client.contracts.claims import LiteralClaimObject, claim_path
from cruxible_client.contracts.semantic import ContentSpan
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.proposals.proposals import AuthenticatedActor
from cruxible_core.runtime.instance import PlaybillInstance
from cruxible_core.service.discovery.next import _prior_refusal_detail
from cruxible_core.service.proposals.proposals import (
    service_reject_playbill_proposal,
    service_withdraw_playbill_proposal,
)
from cruxible_core.service.proposals.review import (
    render_playbill_proposal_review_pointer,
    service_review_playbill_proposal,
)
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._claim_authoring_support import (
    DirectForeignSourceSelection,
    service_propose_playbill_claim,
)
from tests.core_support._knowledge_loop_support import (
    PREDICATE,
    TIMESTAMP,
    activate,
    authoring,
    seed_claims,
    subject_address,
    subject_shell,
)
from tests.core_support._retirement_support import activate_submitted, retirement_member

SOURCE_ID = "fixture.work-items"
REJECTED_AT = "2026-08-16T20:30:00.000000Z"
RETIRED_AT = "2026-08-16T20:40:00.000000Z"
LATER = "2026-08-16T20:50:00.000000Z"
ACCESS = BodyAccessContext(principal_id="reviewer", can_read_body=True)


def _propose(
    instance: PlaybillInstance,
    subject_id: str,
    value: str,
    *,
    body: bytes,
    rationale: str,
    actor_id: str = "owner",
    timestamp: str = TIMESTAMP,
) -> Any:
    stored = instance.body_store().store(body)
    return service_propose_playbill_claim(
        instance,
        authoring=authoring(subject_id, value, with_claim_type=False).model_copy(
            update={
                "rationale": rationale,
                "source_selection": DirectForeignSourceSelection(
                    logical_source_identity=SOURCE_ID,
                    span=ContentSpan(
                        content_digest=stored.digest, start_byte=0, end_byte=len(body)
                    ),
                ),
            }
        ),
        actor_id=actor_id,
        proposal_name=f"{subject_id}-{value}",
        timestamp=timestamp,
    )


class _Cited:
    """A proposal citing an existing Capture, shaped like the fixture's proposals."""

    def __init__(self, proposal_id: str, claim_path: str) -> None:
        self.proposal_id = proposal_id
        self.claim_path = claim_path


def _cite(
    instance: PlaybillInstance,
    subject_id: str,
    value: str,
    *,
    capture_digest: str,
    rationale: str,
    timestamp: str = LATER,
    with_subject: bool = False,
) -> _Cited:
    """Propose one Claim citing an already retained Capture: the same evidence again."""

    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    created = coordinator.create(
        actor=actor,
        payload=ChangeSetAuthoringPayload(
            members=(
                ClaimAuthoringPayload(
                    statement=AuthoringClaimStatement(
                        subject=subject_address(subject_id),
                        predicate=PREDICATE,
                        object=LiteralClaimObject(value=value),
                        role="observation",
                    ),
                    rationale=rationale,
                    source=ExistingCaptureCitationSource(capture_digest=capture_digest),
                    citation_role="evidence",
                    dependency_drafts=ClaimDependencyDrafts(
                        subject=subject_shell(subject_id) if with_subject else None
                    ),
                ),
            ),
            rationale=rationale,
        ),
        canonical_timestamp=timestamp,
    )
    submitted = coordinator.submit(created.intent.intent_id, actor=actor)
    proposal_id = submitted.status.proposal_id
    assert proposal_id is not None, submitted.intent.last_preflight
    evidence = instance.proposal_evidence()
    candidate = evidence.read_candidate(evidence.read_evaluation(proposal_id).candidate_digest)  # type: ignore[arg-type]
    (path,) = [item.path for item in candidate.members if item.artifact_kind == "claim"]
    return _Cited(proposal_id, path)


def _proposal_id(proposed: Any) -> str:
    if isinstance(proposed, _Cited):
        return proposed.proposal_id
    return str(proposed.proposal.proposal.admission.proposal_id)


def _receipts(instance: PlaybillInstance, proposed: Any) -> tuple[Any, ...]:
    review = service_review_playbill_proposal(
        instance, proposal_id=_proposal_id(proposed), access=ACCESS
    )
    (member,) = [item for item in review.members if item.path == proposed.claim_path]
    return member.prior_refusals


def _reject(instance: PlaybillInstance, proposed: Any, reason: str) -> None:
    result = service_reject_playbill_proposal(
        instance,
        proposal_id=_proposal_id(proposed),
        actor_id="reviewer",
        reason=reason,
        rejected_at=REJECTED_AT,
    )
    assert result.status == "rejected"


def _claim_id(proposed: Any) -> str:
    return str(proposed.claim_path.rsplit("/", 1)[1].removesuffix(".json"))


def _retire(instance: PlaybillInstance, owner: Any, claim_id: str, reason: str) -> str:
    """Retire one accepted Claim with a recorded rationale; return the retiring proposal."""

    coordinator = AuthoringIntentCoordinator.for_instance(instance)
    actor = AuthenticatedActor(actor_id="owner")
    created = coordinator.create(
        actor=actor,
        payload=ChangeSetAuthoringPayload(
            members=(retirement_member(instance, claim_id, reason=reason),),  # type: ignore[arg-type]
            rationale="The upstream feed misreported this item.",
        ),
        canonical_timestamp=RETIRED_AT,
    )
    retired = coordinator.submit(created.intent.intent_id, actor=actor)
    activate_submitted(instance, owner, retired)
    assert retired.status.proposal_id is not None
    return retired.status.proposal_id


def test_a_rejected_statement_on_the_same_evidence_resurfaces_neutrally(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)
    first = _propose(
        instance, "wi-44", "ready", body=b"status: ready (44)", rationale="First read."
    )
    _reject(instance, first, "the feed was stale that day")
    record = instance.proposal_evidence().read_withdrawal(_proposal_id(first))
    assert record is not None and record.kind == "rejected"
    (refused,) = record.refused_claims
    assert refused.path == first.claim_path

    again = _cite(
        instance,
        "wi-44",
        "ready",
        capture_digest=first.capture_digest,
        rationale="Read again.",
        with_subject=True,
    )

    (receipt,) = _receipts(instance, again)
    assert receipt.kind == "proposal_rejected"
    assert receipt.proposal_id == _proposal_id(first)
    assert receipt.claim_id == _claim_id(first)
    assert (receipt.decided_by, receipt.decided_at) == ("reviewer", REJECTED_AT)
    assert receipt.rationale == "the feed was stale that day"
    assert receipt.reason_code is None and receipt.producer is None
    # The new proposal is ordinary -- nothing refused it -- and its review names
    # the receipt as recorded, without interpreting it.
    review = service_review_playbill_proposal(
        instance, proposal_id=_proposal_id(again), access=ACCESS
    )
    rendered = render_playbill_proposal_review_pointer(review)
    assert "Earlier refusals of the same statement on the same evidence:" in rendered
    assert "rejected by reviewer" in rendered and "the feed was stale that day" in rendered
    # The queue row counts it; the rejected proposal never matches itself.
    assert _prior_refusal_detail(instance, _proposal_id(again)) == {"prior_refusal_count": 1}
    assert _prior_refusal_detail(instance, _proposal_id(first)) == {}


def test_new_evidence_another_value_or_an_earlier_proposal_matches_nothing(
    tmp_path: Path,
) -> None:
    instance, _owner = seed_claims(tmp_path)
    first = _propose(instance, "wi-44", "ready", body=b"status: ready (44)", rationale="One.")
    _reject(instance, first, "not yet")

    new_evidence = _propose(
        instance,
        "wi-44",
        "ready",
        body=b"status: ready (44, re-read)",
        rationale="Two.",
        timestamp=LATER,
    )
    other_value = _cite(
        instance,
        "wi-44",
        "blocked",
        capture_digest=first.capture_digest,
        rationale="Three.",
        with_subject=True,
    )
    # Admitted before the rejection was recorded: not a prior refusal.
    earlier = _cite(
        instance,
        "wi-44",
        "ready",
        capture_digest=first.capture_digest,
        rationale="Four.",
        timestamp="2026-08-16T20:10:00.000000Z",
        with_subject=True,
    )

    assert _receipts(instance, new_evidence) == ()
    assert _receipts(instance, other_value) == ()
    assert _receipts(instance, earlier) == ()


def test_a_withdrawal_is_not_a_refusal(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)
    first = _propose(instance, "wi-44", "ready", body=b"status: ready (44)", rationale="One.")
    service_withdraw_playbill_proposal(
        instance,
        proposal_id=_proposal_id(first),
        actor_id="owner",
        reason="superseded by my own edit",
        withdrawn_at=REJECTED_AT,
    )
    again = _cite(
        instance,
        "wi-44",
        "ready",
        capture_digest=first.capture_digest,
        rationale="Two.",
        with_subject=True,
    )
    assert _receipts(instance, again) == ()


@pytest.mark.parametrize("reason", ["was-wrong", "superseded"])
def test_a_retired_claim_resurfaces_with_its_recorded_reason(tmp_path: Path, reason: str) -> None:
    instance, owner = seed_claims(tmp_path)
    accepted = _propose(
        instance, "wi-44", "ready", body=b"status: ready (44)", rationale="Accepted read."
    )
    activate(instance, owner, accepted)
    retiring = _retire(instance, owner, _claim_id(accepted), reason)

    again = _cite(
        instance, "wi-44", "ready", capture_digest=accepted.capture_digest, rationale="Again."
    )

    (receipt,) = _receipts(instance, again)
    assert receipt.kind == "claim_retired"
    assert receipt.claim_id == _claim_id(accepted)
    assert receipt.reason_code == reason
    assert (receipt.decided_by, receipt.decided_at) == ("owner", RETIRED_AT)
    assert receipt.rationale == "The upstream feed misreported this item."
    assert receipt.proposal_id == retiring
    assert _prior_refusal_detail(instance, _proposal_id(again)) == {"prior_refusal_count": 1}


def _counted_lookup(instance: PlaybillInstance, proposal_id: str) -> Counter[str]:
    index = instance.refusal_index()
    # Warm: key the proposal and catch the index up to the head.
    index.prior_refusals(instance, proposal_id)
    blobs = PlaybillInstance.blobs_at
    proposal_blobs = PlaybillInstance.proposal_blobs
    work: Counter[str] = Counter()

    def counted(self: Any, oid: str, paths: Any) -> Any:
        paths = tuple(paths)
        work["blobs"] += len(paths)
        return blobs(self, oid, paths)

    def counted_proposal(self: Any, oid: str, paths: Any, **kwargs: Any) -> Any:
        paths = tuple(paths)
        work["blobs"] += len(paths)
        return proposal_blobs(self, oid, paths, **kwargs)

    before = (index.generations_indexed, index.key_probes, index.rows_read, index.rebuilds)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(PlaybillInstance, "blobs_at", counted)
        patch.setattr(PlaybillInstance, "proposal_blobs", counted_proposal)
        index.prior_refusals(instance, proposal_id)
    after = (index.generations_indexed, index.key_probes, index.rows_read, index.rebuilds)
    work.update(
        dict(
            zip(
                ("generations", "probes", "rows", "rebuilds"),
                (b - a for a, b in zip(before, after, strict=True)),
                strict=True,
            )
        )
    )
    return work


def _grown(tmp_path: Path, size: int) -> tuple[PlaybillInstance, Any]:
    """The seeded world plus ``size`` unrelated Claims accepted and then retired."""

    tmp_path.mkdir(parents=True, exist_ok=True)
    instance, owner = seed_claims(tmp_path)
    for index in range(size):
        other = _propose(
            instance,
            f"wi-{100 + index}",
            "ready",
            body=f"status: ready ({index})".encode(),
            rationale=f"Other {index}.",
        )
        activate(instance, owner, other)
        _retire(instance, owner, _claim_id(other), "was-wrong")
    return instance, owner


def test_the_lookup_does_not_grow_with_history(tmp_path: Path) -> None:
    """The same lookup costs the same after 2 and 8 unrelated retirements."""

    counts = []
    for size in (2, 8):
        instance, owner = _grown(tmp_path / f"h{size}", size)
        accepted = _propose(
            instance, "wi-44", "ready", body=b"status: ready (44)", rationale="Accepted."
        )
        activate(instance, owner, accepted)
        _retire(instance, owner, _claim_id(accepted), "was-wrong")
        again = _cite(
            instance, "wi-44", "ready", capture_digest=accepted.capture_digest, rationale="Again."
        )
        assert len(_receipts(instance, again)) == 1
        counts.append(_counted_lookup(instance, _proposal_id(again)))
    assert counts[0] == counts[1]
    assert counts[0]["generations"] == 0 and counts[0]["rebuilds"] == 0
    assert counts[0]["probes"] == 1 and counts[0]["rows"] == 1 and counts[0]["blobs"] == 0


def test_an_acceptance_indexes_only_its_own_generation(tmp_path: Path) -> None:
    """Once built, the index is kept current at acceptance, one generation at a time."""

    instance, owner = _grown(tmp_path, 2)
    accepted = _propose(instance, "wi-44", "ready", body=b"status: ready (44)", rationale="A.")
    activate(instance, owner, accepted)
    index = instance.refusal_index()
    index.catch_up(instance)
    built = index.generations_indexed
    # Activation through the service door: the retirement is indexed as it lands.
    _retire(instance, owner, _claim_id(accepted), "was-wrong")
    assert index.generations_indexed - built == 1
    with sqlite3.connect(f"file:{index.path}?mode=ro", uri=True) as connection:
        rows = connection.execute(
            "SELECT kind, claim_id FROM refusals WHERE kind='claim_retired' AND claim_id=?",
            (_claim_id(accepted),),
        ).fetchall()
    assert rows == [("claim_retired", _claim_id(accepted))]
    index.catch_up(instance)
    assert index.generations_indexed - built == 1


def test_a_deleted_index_is_rebuilt_from_history_and_rejection_records(tmp_path: Path) -> None:
    instance, owner = seed_claims(tmp_path)
    first = _propose(instance, "wi-44", "ready", body=b"status: ready (44)", rationale="One.")
    _reject(instance, first, "the feed was stale that day")
    again = _cite(
        instance,
        "wi-44",
        "ready",
        capture_digest=first.capture_digest,
        rationale="Two.",
        with_subject=True,
    )
    assert len(_receipts(instance, again)) == 1

    index = instance.refusal_index()
    index.close()
    for suffix in ("", "-wal", "-shm"):
        Path(str(index.path) + suffix).unlink(missing_ok=True)

    (receipt,) = _receipts(instance, again)
    assert receipt.kind == "proposal_rejected"
    assert receipt.rationale == "the feed was stale that day"
    assert claim_path(receipt.claim_id) == first.claim_path


def test_a_rebuild_finds_retirements_from_the_projection_not_a_history_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, owner = seed_claims(tmp_path)
    accepted = _propose(instance, "wi-44", "ready", body=b"status: ready (44)", rationale="A.")
    activate(instance, owner, accepted)
    _retire(instance, owner, _claim_id(accepted), "was-wrong")
    again = _cite(
        instance, "wi-44", "ready", capture_digest=accepted.capture_digest, rationale="Again."
    )
    index = instance.refusal_index()
    assert index.path.exists() is False

    def no_walk(self: PlaybillInstance) -> Any:
        raise AssertionError("the refusal index walked accepted history")

    monkeypatch.setattr(PlaybillInstance, "accepted_history", no_walk)
    (receipt,) = index.prior_refusals(instance, _proposal_id(again))[again.claim_path]

    assert index.rebuilds == 1 and index.generations_indexed == 0
    assert (receipt.kind, receipt.reason_code, receipt.decided_at) == (
        "claim_retired",
        "was-wrong",
        RETIRED_AT,
    )
