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
    latest = ClaimReobservationStore(instance).for_claim(before.identity.name)
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
            coordinate=instance.accepted_coordinate(),
            actor_id="owner",
            now=_START + timedelta(minutes=50),
        )
    assert store.for_claim(current.identity.name) == ()
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
                coordinate=instance.accepted_coordinate(),
                actor_id="owner",
                now=_START + timedelta(minutes=50),
            )
    assert store.for_claim(current.identity.name) == ()
    # The cited Capture itself observed nothing new: it is not a re-observation.
    assert parse_capture_envelope(instance.body_store().read(cited, access=_ACCESS))


def test_the_freshness_lookup_is_one_indexed_probe_per_claim(tmp_path: Path) -> None:
    instance, _owner = _freshness_world(tmp_path, stale_after=_HOUR)
    current = _current_claim(instance)
    _reobserve(instance, _START + timedelta(minutes=50))
    # A record is a verdict input outside the body store: remembered verdict
    # derivations key on its journal generation.
    assert "|reobservation:" in str(verdict_input_fingerprint(instance))
    store = ClaimReobservationStore(instance)
    # Many other lineages' records: what this lineage costs never depends on them.
    with store._locked() as conn:
        conn.executemany(
            "INSERT INTO reobservations VALUES (?,?,?,?,?,?)",
            [
                (
                    f"CLM-{index:032x}",
                    "sha256:" + f"{index:064x}",
                    "sha256:" + "1" * 64,
                    "sha256:" + "2" * 64,
                    "2026-08-16T20:00:00+00:00",
                    "2026-08-16T20:00:00+00:00",
                )
                for index in range(5000)
            ],
        )
        plan = " ".join(
            str(row[-1])
            for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT capture_digest FROM reobservations WHERE claim_id=?",
                (current.identity.name,),
            )
        )
    assert "SEARCH" in plan and "SCAN" not in plan
    found = store.for_claims((current.identity.name, "CLM-" + "f" * 32))
    assert store.probes == 2
    assert store.rows_read == 1
    assert len(found[current.identity.name]) == 1


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
    (record,) = ClaimReobservationStore(instance).for_claim(_current_claim(instance).identity.name)
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


def test_query_fact_rows_carry_recorded_reobservations(tmp_path: Path) -> None:
    instance, _owner = _freshness_world(tmp_path, stale_after=_HOUR)
    assert "reobservations" not in _fact_row(instance).model_dump(mode="json")
    _reobserve(instance, _START + timedelta(minutes=50))
    (item,) = _fact_row(instance).reobservations
    assert item.cited_capture_digest == _fact_row(instance).captures[0].capture_digest
    assert item.observed_at == _START + timedelta(minutes=50)


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
                coordinate=instance.accepted_coordinate(),
                actor_id="owner",
                now=_START + timedelta(minutes=50),
            )
    # The journal is the authority: the fingerprint moved with the append itself,
    # so no remembered verdict is served before (or without) any recovery.
    assert verdict_input_fingerprint(instance) != before
    assert _statuses(instance, claim, at) == "supported"
    # Catch-up and a retry are the same record.
    assert len(store.for_claim(claim.identity.name)) == 1
    store.record(
        claim_id=claim.identity.name,
        capture_digest=digest,
        coordinate=instance.accepted_coordinate(),
        actor_id="owner",
        now=_START + timedelta(minutes=50),
    )
    assert len(store.for_claim(claim.identity.name)) == 1
    assert _verdict(instance, at).verdict == "supported"
    assert _statuses(instance, claim, at) == "supported"
