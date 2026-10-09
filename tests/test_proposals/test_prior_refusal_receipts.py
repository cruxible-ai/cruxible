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
from cruxible_core.storage.cas import BodyAccessContext, ContentAddressedBodyStore
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


def test_identical_bytes_captured_again_are_the_same_evidence(tmp_path: Path) -> None:
    """A fresh Capture of identical bytes (another envelope, another time) matches."""

    instance, owner = seed_claims(tmp_path)
    accepted = _propose(instance, "wi-44", "ready", body=b"status: ready (44)", rationale="A.")
    activate(instance, owner, accepted)
    _retire(instance, owner, _claim_id(accepted), "was-wrong")

    again = _propose(
        instance,
        "wi-44",
        "ready",
        body=b"status: ready (44)",
        rationale="Captured again.",
        timestamp=LATER,
    )

    assert again.capture_digests != accepted.capture_digests
    (receipt,) = _receipts(instance, again)
    assert (receipt.kind, receipt.claim_id, receipt.reason_code) == (
        "claim_retired",
        _claim_id(accepted),
        "was-wrong",
    )


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

    read = ContentAddressedBodyStore.read

    def counted_read(self: Any, digest: str, **kwargs: Any) -> bytes:
        work["cas_reads"] += 1
        return read(self, digest, **kwargs)

    before = (index.generations_indexed, index.key_probes, index.rows_read, index.rebuilds)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(PlaybillInstance, "blobs_at", counted)
        patch.setattr(PlaybillInstance, "proposal_blobs", counted_proposal)
        patch.setattr(ContentAddressedBodyStore, "read", counted_read)
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
    assert counts[0]["cas_reads"] == 0


def test_a_read_after_an_acceptance_indexes_only_its_new_generation(tmp_path: Path) -> None:
    """Acceptance does nothing for the index; the next read indexes just what landed."""

    instance, owner = _grown(tmp_path, 2)
    accepted = _propose(instance, "wi-44", "ready", body=b"status: ready (44)", rationale="A.")
    activate(instance, owner, accepted)
    index = instance.refusal_index()
    index.catch_up(instance)
    built = index.generations_indexed
    _retire(instance, owner, _claim_id(accepted), "was-wrong")
    assert index.generations_indexed == built  # activation left the index alone
    index.catch_up(instance)
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


def test_an_index_from_an_earlier_layout_or_key_is_rebuilt(tmp_path: Path) -> None:
    """Rows keyed or laid out by an earlier build are never read: the file rebuilds."""

    instance, owner = seed_claims(tmp_path)
    accepted = _propose(instance, "wi-44", "ready", body=b"status: ready (44)", rationale="A.")
    activate(instance, owner, accepted)
    _retire(instance, owner, _claim_id(accepted), "was-wrong")
    again = _cite(
        instance, "wi-44", "ready", capture_digest=accepted.capture_digest, rationale="Again."
    )
    index = instance.refusal_index()
    assert len(index.prior_refusals(instance, _proposal_id(again))) == 1

    # Another match-key domain recorded in progress: rebuilt in place.
    with sqlite3.connect(index.path) as connection:
        connection.execute(
            "UPDATE refusal_progress SET key_domain='cruxible-claim-refusal-match-v1'"
        )
    rebuilds = index.rebuilds
    assert len(index.prior_refusals(instance, _proposal_id(again))) == 1
    assert index.rebuilds == rebuilds + 1

    # The pre-domain layout (progress without key_domain): dropped and rebuilt.
    index.close()
    with sqlite3.connect(index.path) as connection:
        connection.execute("DROP TABLE refusal_progress")
        connection.execute(
            "CREATE TABLE refusal_progress (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
            "instance_id TEXT NOT NULL, sequence INTEGER NOT NULL CHECK(sequence>=0), "
            "generation_root TEXT NOT NULL) STRICT"
        )
    (receipt,) = index.prior_refusals(instance, _proposal_id(again))[again.claim_path]
    assert (receipt.kind, receipt.reason_code) == ("claim_retired", "was-wrong")


# --- review findings F-001..F-003: the cache never blocks, races, or forgets ----


def test_a_damaged_index_file_is_rebuilt_and_review_still_works(tmp_path: Path) -> None:
    """F-001: torn bytes in the derived file rebuild it; review and approval prep work."""

    from cruxible_core.service.proposals.review import render_playbill_proposal_review

    instance, _owner = seed_claims(tmp_path)
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
    index.path.write_bytes(b"torn derived SQLite projection")

    (receipt,) = _receipts(instance, again)
    assert receipt.rationale == "the feed was stale that day"
    review = service_review_playbill_proposal(
        instance, proposal_id=_proposal_id(again), access=ACCESS
    )
    assert "the feed was stale that day" in render_playbill_proposal_review(review)


def test_an_index_that_cannot_be_rebuilt_reports_no_receipts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-001: if even the rebuilt file fails, reads report no receipts, never an error."""

    from cruxible_core.proposals.refusal_index import RefusalIndex

    instance, _owner = seed_claims(tmp_path)
    first = _propose(instance, "wi-44", "ready", body=b"status: ready (44)", rationale="One.")
    _reject(instance, first, "not yet")
    again = _cite(
        instance,
        "wi-44",
        "ready",
        capture_digest=first.capture_digest,
        rationale="Two.",
        with_subject=True,
    )

    def broken(self: RefusalIndex, connection: Any, instance: Any) -> None:
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(RefusalIndex, "_catch_up", broken)
    assert _receipts(instance, again) == ()
    assert _prior_refusal_detail(instance, _proposal_id(again)) == {}


def test_a_losing_concurrent_rejection_never_sets_the_lookup_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-002: two reviewers reject at once; only the close that won is indexed.

    The 20:30 rejection indexes its row and pauses; the competing 20:59 one is
    let run before it may publish. Whatever the interleaving, a proposal admitted
    at 20:50 sees the rejection that actually landed, at 20:30.
    """

    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from cruxible_core.proposals import refusal_index

    instance, _owner = seed_claims(tmp_path)
    first = _propose(instance, "wi-44", "ready", body=b"status: ready (44)", rationale="One.")
    winner_indexed, loser_indexed, winner_go, loser_go = Event(), Event(), Event(), Event()
    original = refusal_index.RefusalIndex.record_rejection

    def ordered(self: Any, instance: Any, record: Any) -> None:
        original(self, instance, record)
        if record.reason == "winner":
            winner_indexed.set()
            winner_go.wait(10)
        else:
            loser_indexed.set()
            loser_go.wait(10)

    monkeypatch.setattr(refusal_index.RefusalIndex, "record_rejection", ordered)

    def reject(reason: str, at: str) -> Any:
        return service_reject_playbill_proposal(
            instance,
            proposal_id=_proposal_id(first),
            actor_id="reviewer",
            reason=reason,
            rejected_at=at,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(reject, "winner", REJECTED_AT)
        assert winner_indexed.wait(10)
        loser = pool.submit(reject, "loser", "2026-08-16T20:59:00.000000Z")
        loser_indexed.wait(2)  # serialized closes never get here before the winner lands
        winner_go.set()
        assert winner.result(10).status == "rejected"
        loser_go.set()
        try:
            loser.result(10)
        except Exception:  # noqa: BLE001 - a loser may refuse; the index must not care
            pass

    record = instance.proposal_evidence().read_withdrawal(_proposal_id(first))
    assert record is not None and record.withdrawn_at == REJECTED_AT
    between = _cite(
        instance,
        "wi-44",
        "ready",
        capture_digest=first.capture_digest,
        rationale="Between.",
        timestamp="2026-08-16T20:50:00.000000Z",
        with_subject=True,
    )
    (receipt,) = _receipts(instance, between)
    assert (receipt.decided_at, receipt.rationale) == (REJECTED_AT, "winner")
    index = instance.refusal_index()
    with sqlite3.connect(f"file:{index.path}?mode=ro", uri=True) as connection:
        times = connection.execute(
            "SELECT DISTINCT decided_at FROM refusals WHERE kind='proposal_rejected'"
        ).fetchall()
    assert times == [(REJECTED_AT,)]


def _v1_key(claim: Any) -> str:
    """The first key: statement plus Capture envelope digests, domain v1."""

    from cruxible_client.contracts.canonical import Sha256Value, typed_digest

    statement = claim.statement
    return typed_digest(
        Sha256Value,
        "cruxible-claim-refusal-match-v1",
        {
            "subject": statement.subject.model_dump(mode="json"),
            "predicate": statement.predicate,
            "qualifier": statement.qualifier,
            "object": statement.object.model_dump(mode="json"),
            "evidence": sorted(set(claim.backing.capture_digests)),
        },
    ).tagged


def _write_key_only_rejection(instance: PlaybillInstance, proposed: Any) -> None:
    """A rejection record in the first, key-only shape, as the first build wrote it."""

    from cruxible_client.contracts.claims import parse_claim
    from cruxible_client.contracts.proposal_models import ProposalWithdrawalRecord

    evaluation = instance.proposal_evidence().read_evaluation(_proposal_id(proposed))
    assert evaluation.evaluated_tree_oid is not None
    content = instance.proposal_tree(evaluation.evaluated_tree_oid)[proposed.claim_path]
    claim = parse_claim(content, path=proposed.claim_path)
    record = ProposalWithdrawalRecord.model_validate(
        {
            "proposal_id": _proposal_id(proposed),
            "actor_id": "reviewer",
            "reason": "the feed was stale that day",
            "withdrawn_at": REJECTED_AT,
            "kind": "rejected",
            "refused_claims": [
                {
                    "tag": "cruxible-refused-claim-v1",
                    "claim_id": claim.identity.name,
                    "path": proposed.claim_path,
                    "match_key": _v1_key(claim),
                }
            ],
        }
    )
    instance.proposal_evidence().write_withdrawal(record)


def test_a_key_only_rejection_is_rekeyed_in_the_current_domain(tmp_path: Path) -> None:
    """F-003: a first-shape record is re-derived from its retained candidate on rebuild.

    The re-proposal is a fresh Capture of identical content, so only a key in
    the current (content) domain can find it.
    """

    instance, _owner = seed_claims(tmp_path)
    first = _propose(instance, "wi-44", "ready", body=b"status: ready (44)", rationale="One.")
    _write_key_only_rejection(instance, first)
    again = _propose(
        instance, "wi-44", "ready", body=b"status: ready (44)", rationale="Two.", timestamp=LATER
    )
    assert again.capture_digests != first.capture_digests

    (receipt,) = _receipts(instance, again)
    assert (receipt.kind, receipt.proposal_id) == ("proposal_rejected", _proposal_id(first))


def test_a_key_only_rejection_whose_candidate_is_gone_is_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-003: without its candidate a key-only record cannot be keyed; it is dropped."""

    instance, _owner = seed_claims(tmp_path)
    first = _propose(instance, "wi-44", "ready", body=b"status: ready (44)", rationale="One.")
    _write_key_only_rejection(instance, first)
    again = _propose(
        instance, "wi-44", "ready", body=b"status: ready (44)", rationale="Two.", timestamp=LATER
    )
    released = _proposal_id(first)
    blobs = PlaybillInstance.proposal_blobs

    def gone(self: Any, oid: str, paths: Any, *, proposal_id: str | None = None) -> Any:
        if proposal_id == released:
            raise RuntimeError("candidate released")
        return blobs(self, oid, paths, proposal_id=proposal_id)

    monkeypatch.setattr(PlaybillInstance, "proposal_blobs", gone)
    assert _prior_refusal_detail(instance, _proposal_id(again)) == {}


def test_a_rejection_recorded_under_an_earlier_domain_is_rekeyed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F-003: rows and progress from an earlier key domain rebuild with current keys."""

    from cruxible_core.proposals import refusal_index

    instance, _owner = seed_claims(tmp_path)
    first = _propose(instance, "wi-44", "ready", body=b"status: ready (44)", rationale="One.")
    with monkeypatch.context() as patch:
        patch.setattr(refusal_index, "claim_refusal_key", lambda _i, claim: _v1_key(claim))
        patch.setattr(
            refusal_index, "CLAIM_REFUSAL_MATCH_KEY_DOMAIN", "cruxible-claim-refusal-match-v1"
        )
        _reject(instance, first, "the feed was stale that day")
    again = _cite(
        instance,
        "wi-44",
        "ready",
        capture_digest=first.capture_digest,
        rationale="Again.",
        with_subject=True,
    )
    (receipt,) = _receipts(instance, again)
    assert receipt.rationale == "the feed was stale that day"
