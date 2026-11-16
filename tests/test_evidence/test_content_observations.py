"""Content observations and transitions keep cited content fresh (rulings 1a, 1b, 1d).

A fetch is recorded once per acquiring call, keyed by (contract, observed
content), never per Claim; a Claim citing any Capture of that content reads it
as its freshness. A claims terminal that found its Claims unchanged in new
bytes records one transition from the cited content, except the Claims that
changed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from cruxible_client.contracts.claims import claim_citation_references
from cruxible_core.evidence.claim_reobservations import (
    ClaimReobservationStore,
    ContentTransition,
    ObservedContent,
    ReobservationReader,
    cited_content_keys,
)
from cruxible_core.service.evidence.evidence import service_evaluate_playbill_claim_verdict
from tests.test_integration.test_next_closed_loop import _current_claim, _freshness_world

_HOUR = 3_600_000_000
_START = datetime(2026, 8, 16, 20, 0, tzinfo=UTC)
_OTHER = "sha256:" + "e" * 64


def _verdict(instance, at: datetime):  # type: ignore[no-untyped-def]
    return service_evaluate_playbill_claim_verdict(
        instance,
        claim_identity=_current_claim(instance).identity.qualified,
        evaluation_time=at,
    ).verdict


def _cited(instance):  # type: ignore[no-untyped-def]
    claim = _current_claim(instance)
    cited = claim_citation_references(claim)[0].capture_digest
    (key,) = cited_content_keys(
        instance, coordinate=instance.accepted_coordinate(), capture_digests=(cited,)
    )
    return claim, cited, key


def _observe(store, at: datetime, contents, *, earliest: datetime, latest: datetime):  # type: ignore[no-untyped-def]
    store.instance.recording_clock = lambda: at
    return store.observe(
        contents, earliest=earliest, latest=latest, run_id="RUN-x", node_id="fetch", actor_id="o"
    )


def test_one_observation_per_call_refreshes_every_claim_citing_the_content(
    tmp_path: Path,
) -> None:
    instance, _owner = _freshness_world(tmp_path, stale_after=_HOUR)
    claim, cited, key = _cited(instance)
    store = ClaimReobservationStore(instance)
    assert _verdict(instance, _START + timedelta(hours=3)).verdict == "stale_evidence"
    contents = [
        ObservedContent(key.contract_digest, key.observed_content_digest, cited),
        ObservedContent(key.contract_digest, "sha256:" + "1" * 64, _OTHER),
    ]
    # The call's earliest member instant is what freshness reads (ruling 1b).
    _observe(
        store,
        _START + timedelta(hours=2.5),
        contents,
        earliest=_START + timedelta(hours=2),
        latest=_START + timedelta(hours=2.2),
    )
    verdict = _verdict(instance, _START + timedelta(hours=2.6))
    assert (verdict.verdict, verdict.currency) == ("supported", "current")
    (expiration,) = verdict.freshness_expirations
    assert expiration.reobserved_at == _START + timedelta(hours=2)
    assert expiration.reobserved_by == cited
    # Nothing was recorded per Claim.
    assert store.history(claim.identity.name) == ()
    # Recorded after the evaluation instant: not applicable then.
    assert _verdict(instance, _START + timedelta(hours=2.4)).verdict == "stale_evidence"
    # The same contents fetched again: one more point, no new body or set row.
    bodies = sum(1 for path in (instance.root / "cas").rglob("*") if path.is_file())
    _observe(
        store,
        _START + timedelta(hours=4),
        list(reversed(contents)),
        earliest=_START + timedelta(hours=3.5),
        latest=_START + timedelta(hours=3.9),
    )
    with store._locked() as conn:  # noqa: SLF001
        assert conn.execute("SELECT COUNT(*) FROM observed_sets").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM observation_points").fetchone()[0] == 2
    added = sum(1 for path in (instance.root / "cas").rglob("*") if path.is_file()) - bodies
    assert added == 1  # the journal record's own payload; the set body is held
    later = _verdict(instance, _START + timedelta(hours=4))
    assert later.freshness_expirations[0].reobserved_at == _START + timedelta(hours=3.5)
    # Retention counts from the latest instant of the latest call (ruling 1c).
    reader = ReobservationReader.of(instance)
    assert reader.latest_observation(
        key.contract_digest, key.observed_content_digest
    ) == _START + timedelta(hours=3.9)


def test_a_transition_rechecks_the_cited_content_except_the_changed_claims(
    tmp_path: Path,
) -> None:
    instance, _owner = _freshness_world(tmp_path, stale_after=_HOUR)
    claim, _cited_capture, key = _cited(instance)
    store = ClaimReobservationStore(instance)
    moved = ContentTransition(
        contract_digest=key.contract_digest,
        from_content_digest=key.observed_content_digest,
        to_capture_digest=_OTHER,
        to_content_digest="sha256:" + "2" * 64,
    )
    instance.recording_clock = lambda: _START + timedelta(hours=2)
    first = store.transition(
        [moved],
        excepted=[claim.identity.name],
        earliest=_START + timedelta(hours=1.5),
        latest=_START + timedelta(hours=1.6),
        run_id="RUN-a",
        node_id="claims",
        actor_id="o",
    )
    # This Claim changed in the new bytes: the transition does not refresh it.
    assert _verdict(instance, _START + timedelta(hours=2.1)).verdict == "stale_evidence"
    instance.recording_clock = lambda: _START + timedelta(hours=3)
    second = store.transition(
        [moved],
        excepted=[],
        earliest=_START + timedelta(hours=2.5),
        latest=_START + timedelta(hours=2.6),
        run_id="RUN-b",
        node_id="claims",
        actor_id="o",
    )
    assert second != first
    verdict = _verdict(instance, _START + timedelta(hours=3))
    assert verdict.verdict == "supported"
    assert verdict.freshness_expirations[0].reobserved_at == _START + timedelta(hours=2.5)
    # The same moves again are already recorded: nothing new is appended.
    instance.recording_clock = lambda: _START + timedelta(hours=3.2)
    assert (
        store.transition(
            [moved],
            excepted=[],
            earliest=_START + timedelta(hours=3.1),
            latest=_START + timedelta(hours=3.1),
            run_id="RUN-c",
            node_id="claims",
            actor_id="o",
        )
        == second
    )
    # Later fetches of the new bytes keep re-checking the old content's Claims.
    _observe(
        store,
        _START + timedelta(hours=4),
        [ObservedContent(key.contract_digest, moved.to_content_digest, _OTHER)],
        earliest=_START + timedelta(hours=3.8),
        latest=_START + timedelta(hours=3.9),
    )
    later = _verdict(instance, _START + timedelta(hours=4))
    assert later.verdict == "supported"
    assert later.freshness_expirations[0].reobserved_at == _START + timedelta(hours=3.8)
    assert store.history(claim.identity.name) == ()
