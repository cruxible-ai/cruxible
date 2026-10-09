"""Recorded re-observations keep cited content fresh (identity-design-1009).

Restating a Claim on a Capture of content it already cites changes nothing --
no revision, no proposal -- but the newer Capture is recorded on the evidence
plane, and the cited content is as fresh as its latest recorded observation.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.captures import parse_capture_envelope
from cruxible_client.contracts.claim_verdicts import (
    CaptureReobservationEvidence,
    EvidenceFreshnessExpiration,
    evaluate_claim_verdict,
    verify_claim_verdict_freshness,
)
from cruxible_client.contracts.claims import claim_artifact_digest, claim_citation_references
from cruxible_client.contracts.semantic import ContentSpan
from cruxible_core.evidence.claim_reobservations import (
    ClaimReobservationRefused,
    ClaimReobservationStore,
)
from cruxible_core.service.claims.verdict_memo import verdict_input_fingerprint
from cruxible_core.service.discovery.query import build_accepted_query_facts
from cruxible_core.service.evidence.evidence import service_evaluate_playbill_claim_verdict
from cruxible_core.storage.cas import BodyAccessContext
from tests.core_support._claim_authoring_support import (
    DirectClaimAuthoringV1,
    service_propose_playbill_claim,
    service_reobserve_playbill_claim,
)
from tests.test_integration.test_next_closed_loop import (
    DirectForeignSourceSelection,
    _current_claim,
    _freshness_world,
)

_HOUR = 3_600_000_000
_START = datetime(2026, 8, 16, 20, 0, tzinfo=UTC)
_ACCESS = BodyAccessContext(principal_id="reobservation-test", can_read_body=True)


def _stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _restating(instance, *, body: bytes, start: int, end: int) -> DirectClaimAuthoringV1:  # type: ignore[no-untyped-def]
    current = _current_claim(instance)
    stored = instance.body_store().store(body)
    return DirectClaimAuthoringV1(
        statement=current.statement,
        rationale="Observe the still-standing statement again.",
        claim_id=current.identity.name,
        predecessor_artifact_digest=claim_artifact_digest(current).tagged,
        source_selection=DirectForeignSourceSelection(
            logical_source_identity="fixture.freshness",
            span=ContentSpan(content_digest=stored.digest, start_byte=start, end_byte=end),
        ),
    )


def _reobserve(instance, at: datetime, *, recorded_at: datetime | None = None) -> None:  # type: ignore[no-untyped-def]
    service_reobserve_playbill_claim(
        instance,
        authoring=_restating(instance, body=b"status: ready\n", start=8, end=13),
        actor_id="owner",
        timestamp=_stamp(at),
        recorded_at=None if recorded_at is None else _stamp(recorded_at),
    )


def _verdict(instance, at: datetime):  # type: ignore[no-untyped-def]
    return service_evaluate_playbill_claim_verdict(
        instance,
        claim_identity=_current_claim(instance).identity.qualified,
        evaluation_time=at,
    ).verdict


def test_hourly_reobservation_keeps_a_claim_fresh_past_its_captures_expiry(
    tmp_path: Path,
) -> None:
    instance, _owner = _freshness_world(tmp_path, stale_after=_HOUR)
    before = _current_claim(instance)
    assert _verdict(instance, _START + timedelta(hours=3)).verdict == "stale_evidence"

    for hours in (0.9, 1.8, 2.7):
        _reobserve(instance, _START + timedelta(hours=hours))

    # Nothing about the Claim changed: no revision, no new citation.
    assert _current_claim(instance) == before
    verdict = _verdict(instance, _START + timedelta(hours=3))
    assert (verdict.verdict, verdict.currency) == ("supported", "current")
    (expiration,) = verdict.freshness_expirations
    assert expiration.observed_at == _START
    assert expiration.reobserved_at == _START + timedelta(hours=2.7)
    assert expiration.expires_at == _START + timedelta(hours=3.7)
    latest = ClaimReobservationStore(instance).history(before.identity.name)
    assert expiration.reobserved_by == max(latest, key=lambda item: item.observed_at).capture_digest
    # Past the latest re-observation's horizon the content is stale again.
    assert _verdict(instance, _START + timedelta(hours=3.7)).verdict == "stale_evidence"


def test_a_capture_of_different_content_does_not_refresh(tmp_path: Path) -> None:
    instance, _owner = _freshness_world(tmp_path, stale_after=_HOUR)
    current = _current_claim(instance)
    # Same statement, other observed bytes ("status"): a new-content revision is
    # proposed, never a re-observation of the cited content.
    proposed = service_propose_playbill_claim(
        instance,
        authoring=_restating(instance, body=b"status: ready\n", start=0, end=6),
        actor_id="owner",
        proposal_name="other-content",
        timestamp=_stamp(_START + timedelta(minutes=50)),
    )
    cited = {item.capture_digest for item in claim_citation_references(current)}
    (other,) = set(proposed.capture_digests) - cited
    store = ClaimReobservationStore(instance)
    with pytest.raises(ClaimReobservationRefused, match="observed no content"):
        store.record(
            claim_id=current.identity.name,
            capture_digest=other,
            actor_id="owner",
            now=_START + timedelta(minutes=50),
        )
    assert store.history(current.identity.name) == ()
    assert _verdict(instance, _START + timedelta(hours=1)).verdict == "stale_evidence"


def test_a_reobservation_recorded_after_the_evaluation_instant_does_not_count(
    tmp_path: Path,
) -> None:
    instance, _owner = _freshness_world(tmp_path, stale_after=_HOUR)
    # Observed at 1:30 but recorded only at 2:00.
    _reobserve(
        instance,
        _START + timedelta(hours=1.5),
        recorded_at=_START + timedelta(hours=2),
    )
    assert _verdict(instance, _START + timedelta(hours=1.75)).verdict == "stale_evidence"
    later = _verdict(instance, _START + timedelta(hours=2))
    assert (later.verdict, later.currency) == ("supported", "current")


def test_a_forged_or_unadmitted_capture_is_refused(tmp_path: Path) -> None:
    instance, _owner = _freshness_world(tmp_path, stale_after=_HOUR)
    current = _current_claim(instance)
    cited = claim_citation_references(current)[0].capture_digest
    envelope = json.loads(instance.body_store().read(cited, access=_ACCESS))
    # The same envelope re-dated: no daemon produced it.
    envelope["observed_at"] = _stamp(_START + timedelta(minutes=50)).replace(".000000", "")
    forged = instance.body_store().store(json.dumps(envelope).encode("utf-8")).digest
    not_a_capture = instance.body_store().store(b"status: ready").digest
    store = ClaimReobservationStore(instance)
    for digest in (forged, not_a_capture, "sha256:" + "0" * 64):
        with pytest.raises(ClaimReobservationRefused):
            store.record(
                claim_id=current.identity.name,
                capture_digest=digest,
                actor_id="owner",
                now=_START + timedelta(minutes=50),
            )
    assert store.history(current.identity.name) == ()
    # The cited Capture itself observed nothing new: it is not a re-observation.
    assert parse_capture_envelope(instance.body_store().read(cited, access=_ACCESS))


def test_the_freshness_lookup_is_bounded_by_cited_contents_not_history(tmp_path: Path) -> None:
    """One lineage observed thousands of times costs one ordered probe per cited content."""

    from cruxible_core.evidence.claim_reobservations import ReobservationReader

    instance, _owner = _freshness_world(tmp_path, stale_after=_HOUR)
    current = _current_claim(instance)
    _reobserve(instance, _START + timedelta(minutes=50))
    # A record is a verdict input outside the body store: remembered verdict
    # derivations key on its journal.
    assert "|reobservation:" in str(verdict_input_fingerprint(instance))
    store = ClaimReobservationStore(instance)
    (real,) = store.history(current.identity.name)
    start_us = (_START - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(microseconds=1)
    second = 1_000_000

    def row(index: int, content: str, claim_id: str) -> tuple[object, ...]:
        observed = start_us + 3 * index * second
        return (
            claim_id,
            "sha256:" + f"{index:060x}" + content[-4:],
            real.contract_digest,
            content,
            "2026-08-16T20:00:00+00:00",
            "2026-08-16T20:00:30+00:00",
            observed,
            observed + 30 * second,
            100 + index,
        )

    replaced = "sha256:" + "2" * 64  # content a later revision no longer cites
    with store._locked() as conn:
        conn.executemany(
            "INSERT INTO reobservations VALUES (?,?,?,?,?,?,?,?,?)",
            [row(i, real.observed_content_digest, current.identity.name) for i in range(3000)]
            + [row(i, replaced, current.identity.name) for i in range(3000)]
            + [row(i, real.observed_content_digest, f"CLM-{i:032x}") for i in range(2000)],
        )
        plans = [
            " ".join(str(item[-1]) for item in conn.execute("EXPLAIN QUERY PLAN " + sql, args))
            for sql, args in (
                (
                    "SELECT capture_digest FROM reobservations INDEXED BY reobservations_latest "
                    "WHERE claim_id=? AND contract_digest=? AND observed_content_digest=? "
                    "AND observed_us<=? AND recorded_us<=? "
                    "ORDER BY observed_us DESC, capture_digest DESC LIMIT 1",
                    (current.identity.name, "c", "o", 0, 0),
                ),
                (
                    "SELECT MIN(recorded_us) FROM reobservations INDEXED BY reobservations_next "
                    "WHERE claim_id=? AND contract_digest=? AND observed_content_digest=? "
                    "AND recorded_us>?",
                    (current.identity.name, "c", "o", 0),
                ),
            )
        ]
    assert all("SEARCH" in plan and "SCAN" not in plan for plan in plans), plans
    # The full history stays for audit.
    assert len(store.history(current.identity.name)) == 6001

    reader = ReobservationReader.of(instance)
    reader.probes = reader.rows_read = 0
    at = _START + timedelta(hours=2)
    verdict = _verdict(instance, at)
    # Recorded by 2:00 means observed by 1:59:30: the latest such is 3 * 2390 s.
    (expiration,) = verdict.freshness_expirations
    assert expiration.reobserved_at == _START + timedelta(seconds=3 * 2390)
    assert verdict.verdict == "supported"
    # Whether the lineage was re-observed, then one ordered probe and one
    # next-recording probe for the one cited content: never its history.
    assert reader.probes == 4
    assert reader.rows_read == 1


def test_old_verdict_and_receipt_bytes_are_unchanged(tmp_path: Path) -> None:
    instance, _owner = _freshness_world(tmp_path, stale_after=_HOUR)
    at = _START + timedelta(minutes=30)
    before = _verdict(instance, at)
    dumped = before.model_dump_json()
    assert "reobserved" not in dumped
    _reobserve(instance, _START + timedelta(minutes=50))
    # A verdict at an instant before any re-observation was recorded reproduces
    # byte for byte, and its committed expirations still verify.
    assert _verdict(instance, at).model_dump_json() == dumped
    (record,) = ClaimReobservationStore(instance).history(_current_claim(instance).identity.name)
    expiration = EvidenceFreshnessExpiration(
        capture_digest=before.freshness_expirations[0].capture_digest,
        observed_at=_START,
        expires_at=_START + timedelta(hours=1),
    )
    assert set(json.loads(expiration.model_dump_json())) == {
        "tag",
        "capture_digest",
        "observed_at",
        "expires_at",
    }
    reobservation = CaptureReobservationEvidence(
        cited_capture_digest=expiration.capture_digest,
        reobserving_capture_digest=record.capture_digest,
        observed_at=record.observed_at,
        recorded_at=record.recorded_at,
    )
    rule_and_captures = _verdict_inputs(instance)
    verify_claim_verdict_freshness(
        before,
        rule=rule_and_captures[0],
        captures=rule_and_captures[1],
        reobservations=(reobservation,),
    )
    recomputed = evaluate_claim_verdict(
        claim_statement_digest=before.claim_statement_digest,
        rule=rule_and_captures[0],
        evaluation_time=at,
        captures=rule_and_captures[1],
        attestations=(),
        providers={},
        reobservations=(reobservation,),
    )
    assert recomputed.freshness_expirations == before.freshness_expirations


def _fact_row(instance):  # type: ignore[no-untyped-def]
    facts = build_accepted_query_facts(instance, coordinate=instance.accepted_coordinate())
    (row,) = facts.claims
    return row


def _verdict_inputs(instance):  # type: ignore[no-untyped-def]
    row = _fact_row(instance)
    return row.rule, row.captures


def test_query_fact_rows_read_reobservations_at_the_evaluation_instant(tmp_path: Path) -> None:
    instance, _owner = _freshness_world(tmp_path, stale_after=_HOUR)
    at = _START + timedelta(hours=1)
    assert _fact_row(instance).reobservations_at(at) == ()
    _reobserve(instance, _START + timedelta(minutes=50))
    row = _fact_row(instance)
    # How a row reads freshness is not part of its bytes or its equality.
    assert "reobservation" not in row.model_dump_json()
    (item,) = row.reobservations_at(at)
    assert item.cited_capture_digest == row.captures[0].capture_digest
    assert item.observed_at == _START + timedelta(minutes=50)
    # Before the record was made it does not count.
    assert row.reobservations_at(_START + timedelta(minutes=40)) == ()


# -- interrupted appends (review F-010) ---------------------------------------


def _pending_same_content(instance, at: datetime) -> str:  # type: ignore[no-untyped-def]
    """Land a same-content Capture through submit without recording it."""

    saved: list[str] = []
    with pytest.MonkeyPatch.context() as patch:

        def stop_before_record(self, **kwargs):  # type: ignore[no-untyped-def]
            saved.append(kwargs["capture_digest"])

        patch.setattr(ClaimReobservationStore, "record", stop_before_record)
        _reobserve(instance, at)
    (digest,) = saved
    return digest


def _statuses(instance, claim, at: datetime) -> str:  # type: ignore[no-untyped-def]
    from cruxible_client.contracts.projection import AcceptedCoordinate
    from cruxible_core.service.discovery.claim_status import claim_resolution_statuses

    verdicts: dict[str, Any] = {}
    claim_resolution_statuses(
        instance,
        claims=(claim,),
        at=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        evaluation_time=at,
        verdicts_by_identity=verdicts,
    )
    return str(verdicts[claim.identity.qualified].verdict)


@pytest.mark.parametrize("crash", ["after_append", "after_projection_commit"])
def test_an_interrupted_append_never_leaves_a_remembered_verdict_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash: str
) -> None:
    from cruxible_core.service.discovery.claim_status import reset_claim_resolution_memo

    instance, _owner = _freshness_world(tmp_path, stale_after=_HOUR)
    claim = _current_claim(instance)
    digest = _pending_same_content(instance, _START + timedelta(minutes=50))
    at = _START + timedelta(minutes=70)
    reset_claim_resolution_memo()
    # Warm the remembered derivation while the newer Capture is not yet recorded.
    assert _statuses(instance, claim, at) == "stale_evidence"
    before = verdict_input_fingerprint(instance)
    real_apply = ClaimReobservationStore._apply

    def interrupted(conn, payload, sequence, recorded_at):  # type: ignore[no-untyped-def]
        if crash == "after_projection_commit":
            real_apply(conn, payload, sequence, recorded_at)
            conn.commit()
        raise OSError(f"crash {crash}")

    store = ClaimReobservationStore(instance)
    with monkeypatch.context() as local:
        local.setattr(ClaimReobservationStore, "_apply", staticmethod(interrupted))
        with pytest.raises(OSError, match="crash"):
            store.record(
                claim_id=claim.identity.name,
                capture_digest=digest,
                actor_id="owner",
                now=_START + timedelta(minutes=50),
            )
    # The journal is the authority: the fingerprint moved with the append itself,
    # so no remembered verdict is served before (or without) any recovery.
    assert verdict_input_fingerprint(instance) != before
    assert _statuses(instance, claim, at) == "supported"
    # Catch-up and a retry are the same record.
    assert len(store.history(claim.identity.name)) == 1
    store.record(
        claim_id=claim.identity.name,
        capture_digest=digest,
        actor_id="owner",
        now=_START + timedelta(minutes=50),
    )
    assert len(store.history(claim.identity.name)) == 1
    assert _verdict(instance, at).verdict == "supported"
    assert _statuses(instance, claim, at) == "supported"


# -- admission at the live head (review F-011) --------------------------------


def test_a_record_for_a_retired_lineage_is_refused(tmp_path: Path) -> None:
    from cruxible_client.contracts.claims import claim_path, parse_claim
    from tests.core_support._published_world import retire_claim

    instance, owner = _freshness_world(tmp_path, stale_after=_HOUR)
    claim = _current_claim(instance)
    digest = _pending_same_content(instance, _START + timedelta(minutes=50))
    retire_claim(instance, owner, claim.identity.name)
    path = claim_path(claim.identity.name)
    head = instance.blob_at(instance.accepted_coordinate().git_oid, path)
    assert head is not None and parse_claim(head, path=path).lifecycle.state == "retired"
    store = ClaimReobservationStore(instance)
    with pytest.raises(ClaimReobservationRefused, match="retired"):
        store.record(
            claim_id=claim.identity.name,
            capture_digest=digest,
            actor_id="owner",
            now=_START + timedelta(minutes=50),
        )
    assert store.history(claim.identity.name) == ()


def test_the_cited_capture_itself_and_a_capture_observed_later_are_refused(
    tmp_path: Path,
) -> None:
    instance, _owner = _freshness_world(tmp_path, stale_after=_HOUR)
    claim = _current_claim(instance)
    store = ClaimReobservationStore(instance)
    with pytest.raises(ClaimReobservationRefused, match="already cites"):
        store.record(
            claim_id=claim.identity.name,
            capture_digest=claim_citation_references(claim)[0].capture_digest,
            actor_id="owner",
            now=_START + timedelta(hours=1),
        )
    digest = _pending_same_content(instance, _START + timedelta(minutes=50))
    with pytest.raises(ClaimReobservationRefused, match="after the re-observation"):
        store.record(
            claim_id=claim.identity.name,
            capture_digest=digest,
            actor_id="owner",
            now=_START + timedelta(minutes=10),
        )
    assert store.history(claim.identity.name) == ()


def test_same_size_crash_tail_recovery_invalidates_a_remembered_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review F-013 probe: a recovered shorter frame filling the torn tail exactly."""

    import os

    from cruxible_core.exhaust.backends import journal_log_path
    from cruxible_core.service.discovery.claim_status import reset_claim_resolution_memo

    instance, _owner = _freshness_world(tmp_path, stale_after=_HOUR)
    claim = _current_claim(instance)
    digest = _pending_same_content(instance, _START + timedelta(minutes=50))
    store = ClaimReobservationStore(instance)
    log = journal_log_path(store.root, store.stream, "reobservations")
    write = os.write

    def partial(fd: int, content: bytes) -> int:
        if log.exists() and os.fstat(fd).st_ino == log.stat().st_ino:
            # Tear a longer frame at exactly the length a shorter actor's
            # otherwise identical record will occupy.
            write(fd, content[:-1000])
            raise OSError("interrupted journal frame")
        return write(fd, content)

    with monkeypatch.context() as local:
        local.setattr(os, "write", partial)
        with pytest.raises(OSError, match="interrupted journal frame"):
            store.record(
                claim_id=claim.identity.name,
                capture_digest=digest,
                actor_id="x" * 1005,
                now=_START + timedelta(minutes=50),
            )
    assert store.history(claim.identity.name) == ()
    reset_claim_resolution_memo()
    at = _START + timedelta(minutes=70)
    assert _statuses(instance, claim, at) == "stale_evidence"
    torn_size = log.stat().st_size
    store.record(
        claim_id=claim.identity.name,
        capture_digest=digest,
        actor_id="owner",
        now=_START + timedelta(minutes=50),
    )
    assert len(store.history(claim.identity.name)) == 1
    assert log.stat().st_size == torn_size  # the recovered log is the same size
    assert _verdict(instance, at).verdict == "supported"
    assert _statuses(instance, claim, at) == "supported"


# -- the accepted head held through admission and append (review F-014) ------


@pytest.mark.parametrize("change", ["retirement", "citation_replacement"])
def test_an_acceptance_cannot_land_between_admission_and_append(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """An acceptance started mid-admission waits until the record is durable."""

    from concurrent.futures import ThreadPoolExecutor

    from cruxible_client.contracts.claims import claim_path, parse_claim
    from tests.core_support._published_world import retire_claim
    from tests.test_integration.test_next_closed_loop import _revise_claim

    instance, owner = _freshness_world(tmp_path, stale_after=_HOUR)
    claim = _current_claim(instance)
    digest = _pending_same_content(instance, _START + timedelta(minutes=50))
    head_before = instance.accepted_coordinate().git_oid
    admitted = ClaimReobservationStore._admitted
    seen: dict[str, Any] = {}

    def accept() -> None:
        if change == "retirement":
            retire_claim(instance, owner, claim.identity.name)
        else:
            _revise_claim(instance, owner, timestamp="2026-08-16T20:55:00.000000Z")

    with ThreadPoolExecutor(max_workers=1) as pool:

        def interleave(self, **kwargs):  # type: ignore[no-untyped-def]
            result = admitted(self, **kwargs)
            seen["future"] = future = pool.submit(accept)
            # The acceptance cannot finish while the head is held.
            try:
                future.result(timeout=1.0)
            except TimeoutError:
                seen["blocked"] = True
            seen["head_at_append"] = instance.accepted_coordinate().git_oid
            return result

        monkeypatch.setattr(ClaimReobservationStore, "_admitted", interleave)
        store = ClaimReobservationStore(instance)
        store.record(
            claim_id=claim.identity.name,
            capture_digest=digest,
            actor_id="owner",
            now=_START + timedelta(minutes=50),
        )
        seen["future"].result(timeout=60)
    assert seen.get("blocked") is True
    assert seen["head_at_append"] == head_before
    # The record was made against the head it was admitted at; the change landed after.
    assert len(store.history(claim.identity.name)) == 1
    assert instance.accepted_coordinate().git_oid != head_before
    path = claim_path(claim.identity.name)
    after = parse_claim(
        instance.blob_at(instance.accepted_coordinate().git_oid, path) or b"", path=path
    )
    if change == "retirement":
        assert after.lifecycle.state == "retired"
    else:
        assert after.backing.capture_digests != claim.backing.capture_digests


def test_a_head_moved_by_another_process_is_judged_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner = _freshness_world(tmp_path, stale_after=_HOUR)
    claim = _current_claim(instance)
    digest = _pending_same_content(instance, _START + timedelta(minutes=50))
    real = type(instance).holding_accepted_head
    from contextlib import contextmanager

    @contextmanager
    def moved(self):  # type: ignore[no-untyped-def]
        with real(self):
            yield "0" * 64  # the ledger's main is not the verified coordinate

    monkeypatch.setattr(type(instance), "holding_accepted_head", moved)
    store = ClaimReobservationStore(instance)
    with pytest.raises(ClaimReobservationRefused, match="moved"):
        store.record(
            claim_id=claim.identity.name,
            capture_digest=digest,
            actor_id="owner",
            now=_START + timedelta(minutes=50),
        )
    assert store.history(claim.identity.name) == ()


# -- the reader follows its projection file (review F-015) --------------------


@pytest.mark.parametrize("how", ["deleted", "replaced"])
def test_a_warmed_reader_follows_a_rebuilt_projection(tmp_path: Path, how: str) -> None:
    from cruxible_core.evidence.claim_reobservations import ReobservationReader

    instance, _owner = _freshness_world(tmp_path, stale_after=_HOUR)
    claim = _current_claim(instance)
    _reobserve(instance, _START + timedelta(minutes=50))
    assert _verdict(instance, _START + timedelta(minutes=70)).verdict == "supported"
    reader = ReobservationReader.of(instance)
    assert reader.sequence(claim.identity.name) == 1
    store = ClaimReobservationStore(instance)
    if how == "deleted":
        store.path.unlink()
        # Rebuilt from the journal by the next store open.
        assert len(store.history(claim.identity.name)) == 1
    else:
        staged = store.path.with_name("replacement.sqlite3")
        store.path.rename(staged)
        assert len(store.history(claim.identity.name)) == 1  # rebuilt in place
        store.path.unlink()
        staged.rename(store.path)  # an older copy put back: a different file
    _reobserve(instance, _START + timedelta(minutes=100))
    assert len(store.history(claim.identity.name)) == 2
    assert reader.sequence(claim.identity.name) == 2
    verdict = _verdict(instance, _START + timedelta(minutes=130))
    assert verdict.verdict == "supported", verdict
    (expiration,) = verdict.freshness_expirations
    assert expiration.reobserved_at == _START + timedelta(minutes=100)
