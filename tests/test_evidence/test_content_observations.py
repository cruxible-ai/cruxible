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
    ClaimReobservationJournalCorrupt,
    ClaimReobservationStore,
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


def test_a_row_observation_refreshes_a_row_whatever_bytes_carried_it(tmp_path: Path) -> None:
    """Ruling 2026-11-16 (a)-(c): one record per extract step, keyed by its rows.

    New bytes holding the same rows are the same set: a head update, no new
    set member rows, and a Claim depending on a row reads the later call.
    """

    from cruxible_core.evidence.claim_reobservations import ReobservationKey, RowMap

    instance, _owner = _freshness_world(tmp_path, stale_after=_HOUR)
    claim, cited, key = _cited(instance)
    store = ClaimReobservationStore(instance)
    row = "sha256:" + "7" * 64
    other = "sha256:" + "8" * 64
    hours = lambda value: _START + timedelta(hours=value)  # noqa: E731

    def observe(at, capture, earliest, latest):  # type: ignore[no-untyped-def]
        instance.recording_clock = lambda: at
        return store.observe_rows(
            (
                RowMap(
                    capture, "sha256:" + "5" * 64, key.contract_digest, (("/0", row), ("/1", other))
                ),
            ),
            earliest=earliest,
            latest=latest,
            run_id="RUN-x",
            node_id="extract",
            actor_id="o",
        )

    observe(hours(2.2), cited, hours(2), hours(2.1))
    reader = ReobservationReader(instance)
    assert reader.row_of(cited, "sha256:" + "5" * 64, "/0") == (key.contract_digest, row)
    depends = (ReobservationKey(cited, key.contract_digest, row),)
    (evidence,) = reader.latest(claim.identity.name, depends, hours(3)).evidence
    assert evidence.observed_at == hours(2)
    with store._locked() as conn:  # noqa: SLF001
        members = conn.execute("SELECT COUNT(*) FROM observed_sets").fetchone()[0]
    # The same rows in new bytes (another Capture): the same set, a later call.
    observe(hours(4.2), _OTHER, hours(4), hours(4.1))
    with store._locked() as conn:  # noqa: SLF001
        assert conn.execute("SELECT COUNT(*) FROM observed_sets").fetchone()[0] == members
        assert conn.execute("SELECT COUNT(*) FROM observation_sets").fetchone()[0] == 1
    (evidence,) = reader.latest(claim.identity.name, depends, hours(5)).evidence
    assert evidence.observed_at == hours(4)
    assert reader.row_of(_OTHER, "sha256:" + "5" * 64, "/1") == (key.contract_digest, other)
    # Nothing was recorded per Claim.
    assert store.history(claim.identity.name) == ()


def test_an_older_call_recorded_later_never_moves_freshness_back(tmp_path: Path) -> None:
    """F-003: a key's freshness is the running maximum of its calls' earliest
    instants, at head, at a historical instant and at a read position."""

    from cruxible_core.storage.read_positions import ReadPositions, reading_at

    instance, _owner = _freshness_world(tmp_path, stale_after=_HOUR)
    claim, cited, key = _cited(instance)
    store = ClaimReobservationStore(instance)
    mine = [ObservedContent(key.contract_digest, key.observed_content_digest, cited)]
    other = [*mine, ObservedContent(key.contract_digest, "sha256:" + "1" * 64, _OTHER)]
    hours = lambda value: _START + timedelta(hours=value)  # noqa: E731
    first = _observe(store, hours(2.2), mine, earliest=hours(2), latest=hours(2.1))
    # The same set, an older call recorded later.
    _observe(store, hours(2.4), mine, earliest=hours(1), latest=hours(1.1))
    # Another set holding the key: a call that started earlier still but ended later.
    _observe(store, hours(2.6), other, earliest=hours(1.5), latest=hours(2.5))
    reader = ReobservationReader(instance)
    keys = (key,)
    for at in (hours(2.3), hours(2.5), hours(3)):
        (evidence,) = reader.latest(claim.identity.name, keys, at).evidence
        assert evidence.observed_at == hours(2), at
    # Recorded at the first record's position, a read sees that record alone.
    assert first is not None
    with reading_at(ReadPositions(landing_log="", landing=0, reobs=first)):
        (evidence,) = reader.latest(claim.identity.name, keys, hours(3)).evidence
    assert evidence.observed_at == hours(2)
    assert _verdict(instance, hours(2.7)).freshness_expirations[0].reobserved_at == hours(2)
    # Retention counts from the greatest latest instant, whichever call held it.
    assert reader.latest_observation(key.contract_digest, key.observed_content_digest) == hours(2.5)


def test_a_settlement_selects_the_observation_made_while_the_version_was_live(
    tmp_path: Path,
) -> None:
    """F-002: an observation after the version's retirement never hides an
    earlier one made while it was live; neither counts for a deadline before it."""

    from cruxible_client.contracts.claims import claim_artifact_digest
    from tests.core_support._published_world import retire_claim

    instance, owner = _freshness_world(tmp_path, stale_after=_HOUR)
    claim, cited, key = _cited(instance)
    version = claim_artifact_digest(claim).tagged
    store = ClaimReobservationStore(instance)
    mine = [ObservedContent(key.contract_digest, key.observed_content_digest, cited)]
    hours = lambda value: _START + timedelta(hours=value)  # noqa: E731
    live_at = instance.accepted_coordinate().git_oid
    _observe(store, hours(2.2), mine, earliest=hours(2), latest=hours(2.1))
    retire_claim(instance, owner, claim.identity.name)
    _observe(store, hours(3.2), mine, earliest=hours(3), latest=hours(3.1))
    reader = ReobservationReader(instance)
    found = reader.latest_version_record(
        claim.identity.name, version, observed_by=hours(4), live_at=live_at, keys=(key,)
    )
    assert found is not None and found.record.observed_at == hours(2)
    assert (
        reader.latest_version_record(
            claim.identity.name, version, observed_by=hours(2.05), live_at=live_at, keys=(key,)
        )
        is None
    )


def test_a_settlement_finds_the_best_observation_inside_a_later_live_interval(
    tmp_path: Path,
) -> None:
    """Review 2b F-003: the global best lies before the version's interval.

    The version (here the retired artifact, live from its generation on) saw
    a delayed call with an older earliest instant after a fresher one; the
    settlement reads the fresher (90), never the latest-recorded (50), and
    never the one before the interval (100).
    """

    from cruxible_client.contracts.claims import claim_artifact_digest, claim_path, parse_claim
    from tests.core_support._published_world import retire_claim

    instance, owner = _freshness_world(tmp_path, stale_after=_HOUR)
    claim, cited, key = _cited(instance)
    store = ClaimReobservationStore(instance)
    mine = [ObservedContent(key.contract_digest, key.observed_content_digest, cited)]
    hours = lambda value: _START + timedelta(hours=value)  # noqa: E731
    _observe(store, hours(1.2), mine, earliest=hours(1), latest=hours(1.1))
    retire_claim(instance, owner, claim.identity.name)
    head = instance.accepted_coordinate()
    path = claim_path(claim.identity.name)
    version = claim_artifact_digest(parse_claim(instance.tree_at(head.git_oid)[path], path=path))
    _observe(store, hours(1.5), mine, earliest=hours(0.9), latest=hours(1.4))
    _observe(store, hours(1.6), mine, earliest=hours(0.5), latest=hours(1.5))
    reader = ReobservationReader(instance)
    found = reader.latest_version_record(
        claim.identity.name,
        version.tagged,
        observed_by=hours(3),
        live_at=head.git_oid,
        keys=(key,),
    )
    assert found is not None and found.record.observed_at == hours(0.9)


def _vm_steps(reader: ReobservationReader, call):  # type: ignore[no-untyped-def]
    """SQLite VM steps one reader call costs (the progress handler at every op)."""

    reader.content_observed()  # connect and catch up first
    conn = reader._conn  # noqa: SLF001
    assert conn is not None
    steps = [0]

    def count() -> int:
        steps[0] += 1
        return 0

    conn.set_progress_handler(count, 1)
    try:
        call()
    finally:
        conn.set_progress_handler(None, 1)
    return steps[0]


def test_freshness_reads_do_not_grow_with_observation_history(tmp_path: Path) -> None:
    """F-004: head freshness, the remembered-verdict sequence, retention and a
    settlement read do the same SQLite work at 100 and 1,000 unchanged calls."""

    from cruxible_client.contracts.claims import claim_artifact_digest
    from cruxible_client.contracts.temporal import format_datetime

    costs = {}
    for size in (100, 1_000):
        root = tmp_path / str(size)
        root.mkdir()
        instance, _owner = _freshness_world(root, stale_after=_HOUR)
        claim, cited, key = _cited(instance)
        store = ClaimReobservationStore(instance)
        mine = [ObservedContent(key.contract_digest, key.observed_content_digest, cited)]
        sequence = _observe(
            store, _START, mine, earliest=_START - timedelta(seconds=2), latest=_START
        )
        assert sequence is not None
        with store._locked() as conn:  # noqa: SLF001
            (set_digest,) = {
                row[0] for row in conn.execute("SELECT set_digest FROM observation_sets")
            }
        # One unrelated call recorded a month after it ended (F-007): its delay
        # must not widen any deadline read of this key over the whole history.
        unrelated = [ObservedContent(key.contract_digest, "sha256:" + "9" * 64, _OTHER)]
        sequence = _observe(
            store,
            _START,
            unrelated,
            earliest=_START - timedelta(days=31),
            latest=_START - timedelta(days=30),
        )
        assert sequence is not None
        # The same call's history, projected as unchanged calls would leave it.
        with store._locked() as conn:  # noqa: SLF001
            generation = conn.execute("SELECT generation FROM instants LIMIT 1").fetchone()[0]
            for index in range(1, size):
                at = _START + timedelta(minutes=index)
                store._apply_shared(  # noqa: SLF001
                    conn,
                    {
                        "tag": "cruxible-content-observation-v1",
                        "set_digest": set_digest,
                        "earliest": format_datetime(at - timedelta(seconds=2)),
                        "latest": format_datetime(at),
                        "node_id": "fetch",
                    },
                    sequence + index,
                    at,
                    generation=generation,
                )
        reader = ReobservationReader(instance)
        name = claim.identity.name
        version = claim_artifact_digest(claim).tagged
        live_at = instance.accepted_coordinate().git_oid
        end = _START + timedelta(days=30)
        costs[size] = {
            "latest": _vm_steps(reader, lambda: reader.latest(name, (key,), end)),
            "sequence": _vm_steps(reader, lambda: reader.sequence(name, (key,))),
            "retention": _vm_steps(
                reader,
                lambda: reader.latest_observation(key.contract_digest, key.observed_content_digest),
            ),
            "historical": _vm_steps(
                reader, lambda: reader.latest(name, (key,), _START + timedelta(minutes=50))
            ),
        }

        # A settlement read's SQL; the record it selects is a projected one with
        # no journal record behind it, which the read then refuses.
        def settle() -> None:
            try:
                reader.latest_version_record(
                    name,
                    version,
                    observed_by=_START + timedelta(minutes=40),
                    live_at=live_at,
                    keys=(key,),
                )
            except ClaimReobservationJournalCorrupt:
                pass

        costs[size]["settlement"] = _vm_steps(reader, settle)
    for name, small in costs[100].items():
        assert small > 0, (name, costs)
        assert costs[1_000][name] <= small + 64, (name, costs)
