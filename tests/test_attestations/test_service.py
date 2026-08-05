"""D2-D5 and D8 service semantics."""
# mypy: disable-error-code=no-untyped-def

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone

import pytest

import cruxible_core.service.attestations as attestation_service
from cruxible_core.attestation.store import AttestationStore
from cruxible_core.attestation.types import (
    AttestationRecordResult,
    AttestationStance,
    ClaimKey,
    CorroborationSummary,
)
from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import ConfigError, DataValidationError
from cruxible_core.graph.assertion_state import relationship_assertion_from_metadata
from cruxible_core.graph.evidence import EvidenceRef
from cruxible_core.graph.types import RelationshipInstance, mint_claim_id
from cruxible_core.query.continuation import (
    StaleContinuationError,
    decode_continuation_token,
    mint_continuation_token,
    validate_continuation_token,
)
from cruxible_core.service import (
    service_attest,
    service_attestation_queue,
    service_corroboration_summaries,
    service_list,
    service_list_attestations,
    service_resolve_attestation,
)
from cruxible_core.service.attestations import attach_corroboration_summaries
from cruxible_core.storage.sqlite import SQLiteGraphRepository
from tests.test_attestations.conftest import actor, add_live_claim, evidence

OBSERVED_AT = datetime(2026, 7, 24, 11, 0, tzinfo=timezone.utc)
CLAIM_KEY = ("protected_by", "Service", "svc-1", "Control", "ctl-1")


def _attest(
    instance: CruxibleInstance,
    stance: AttestationStance,
    *,
    observer: str = "observer",
    evidence_refs: Sequence[EvidenceRef | Mapping[str, object]] | None = None,
    edge_key: int | None = None,
    claim_id: str | None = None,
    properties: dict[str, object] | None = None,
    idempotency_key: str | None = None,
    note: str | None = None,
    observed_at: datetime = OBSERVED_AT,
) -> AttestationRecordResult:
    return service_attest(
        instance,
        relationship_type="protected_by",
        from_type="Service",
        from_id="svc-1",
        to_type="Control",
        to_id="ctl-1",
        stance=stance,
        evidence_refs=(
            [evidence(f"{observer}-{stance}")]
            if evidence_refs is None and stance != "unsure"
            else evidence_refs or []
        ),
        observed_at=observed_at,
        actor_context=actor(observer),
        edge_key=edge_key,
        claim_id=claim_id,
        properties=properties,
        idempotency_key=idempotency_key,
        note=note,
    )


def _config_digest(instance: CruxibleInstance) -> str:
    from cruxible_core.workflow.compiler import compute_lock_config_digest

    return compute_lock_config_digest(instance.load_config())


def _edge_payload(instance: CruxibleInstance) -> dict[str, object]:
    """One serialized edge payload with corroboration attached, as a read returns it."""
    relationship = instance.load_graph().get_relationship(
        "Service", "svc-1", "Control", "ctl-1", "protected_by"
    )
    assert relationship is not None
    payload = relationship.model_dump(mode="json")
    attach_corroboration_summaries(instance, [payload])
    return payload


def test_absent_support_creates_pending_with_required_properties(
    attestation_instance: CruxibleInstance,
) -> None:
    result = _attest(
        attestation_instance,
        "support",
        properties={"severity": "high"},
        edge_key=999,
    )
    relationship = attestation_instance.load_graph().get_relationship(
        "Service",
        "svc-1",
        "Control",
        "ctl-1",
        "protected_by",
    )
    assert result.created_claim is True
    assert result.attestation.claim_state_at_record == "pending"
    assert relationship is not None
    assert relationship.properties == {"severity": "high"}
    assert relationship_assertion_from_metadata(relationship.metadata).review.status == "pending"
    assert relationship.metadata.evidence is not None
    assert relationship.metadata.evidence.evidence_refs == result.attestation.evidence_refs
    listed = service_list_attestations(attestation_instance, claim_key=CLAIM_KEY)
    # EVER-OR-NEVER: this record stamped a claim_id at record time, so the
    # target-identity comparison is id-vs-id and the stale caller-supplied
    # edge_key (999) is never consulted. Comparing the per-load edge_key on a
    # record that has a stable identity would report a mismatch after any
    # reload, which is noise, not signal.
    assert listed.items[0].target_identity_mismatch_kind == "claim_id"
    assert listed.items[0].target_identity_mismatch is False
    assert listed.items[0].attestation.claim_id == relationship.claim_id
    attached = _attest(attestation_instance, "contradict")
    assert attached.created_claim is False
    assert attached.attestation.claim_state_at_record == "pending"


def test_absent_refusals_are_receipted_and_leave_no_observation(
    attestation_instance: CruxibleInstance,
) -> None:
    absent_stances: tuple[AttestationStance, ...] = ("contradict", "unsure")
    for stance in absent_stances:
        with pytest.raises(ConfigError, match="only support"):
            _attest(attestation_instance, stance)
    with pytest.raises(ConfigError, match="cannot create pending claim"):
        service_attest(
            attestation_instance,
            relationship_type="protected_by",
            from_type="Service",
            from_id="missing",
            to_type="Control",
            to_id="ctl-1",
            stance="support",
            evidence_refs=[evidence("missing-endpoint")],
            observed_at=OBSERVED_AT,
            actor_context=actor("observer"),
            properties={"severity": "high"},
        )
    assert service_list_attestations(attestation_instance).total == 0


def test_attach_preserves_state_warns_on_properties_and_dedupes_retries(
    attestation_instance: CruxibleInstance,
) -> None:
    relationship = add_live_claim(attestation_instance)
    before = relationship.model_dump(mode="json")
    first = _attest(
        attestation_instance,
        "support",
        properties={"severity": "ignored"},
        idempotency_key="retry-1",
    )
    replay = _attest(
        attestation_instance,
        "support",
        properties={"severity": "ignored"},
        idempotency_key="retry-1",
    )
    after = attestation_instance.load_graph().get_relationship(
        "Service",
        "svc-1",
        "Control",
        "ctl-1",
        "protected_by",
    )
    assert first.attestation.claim_state_at_record == "live"
    assert first.warnings == ["properties ignored because the claim tuple already exists"]
    assert replay.idempotent_replay is True
    assert replay.attestation.attestation_id == first.attestation.attestation_id
    assert replay.receipt_id == first.receipt_id
    assert service_list_attestations(attestation_instance).total == 1
    assert after is not None
    assert after.model_dump(mode="json") == before


def test_attach_records_non_live_claim_state(
    attestation_instance: CruxibleInstance,
) -> None:
    add_live_claim(attestation_instance)
    graph = attestation_instance.load_graph()
    relationship = graph.get_relationship(
        "Service",
        "svc-1",
        "Control",
        "ctl-1",
        "protected_by",
    )
    assert relationship is not None
    relationship.metadata.assertion.lifecycle.status = "inactive"
    assert graph.update_relationship_state(
        "Service",
        "svc-1",
        "Control",
        "ctl-1",
        "protected_by",
        metadata=relationship.metadata,
    )
    attestation_instance.save_graph(graph)
    result = _attest(attestation_instance, "support")
    assert result.attestation.claim_state_at_record == "inactive"


def test_create_refusal_retries_as_attach_after_in_transaction_race(
    attestation_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_load = SQLiteGraphRepository.load_graph
    original_create = attestation_service._create_pending_claim
    load_count = 0

    def load_with_race(self: SQLiteGraphRepository):
        nonlocal load_count
        graph = original_load(self)
        load_count += 1
        if load_count == 2:
            relationship, created = original_create(
                graph,
                config=attestation_instance.load_config(),
                claim_key=CLAIM_KEY,
                properties={"severity": "high"},
                evidence_refs=[evidence("race-winner")],
                actor_context=actor("race-winner"),
                receipt_id="RCP-race-winner",
            )
            assert created is True
            self.upsert_relationships([relationship])
        return graph

    def lose_create(*args: object, **kwargs: object):
        raise DataValidationError("same tuple appeared concurrently")

    monkeypatch.setattr(SQLiteGraphRepository, "load_graph", load_with_race)
    monkeypatch.setattr(attestation_service, "_create_pending_claim", lose_create)
    result = _attest(
        attestation_instance,
        "support",
        properties={"severity": "high"},
    )
    assert result.created_claim is False
    assert result.attestation.claim_state_at_record == "pending"
    # BOTH warnings: the attach, and the properties it silently discarded.
    # The properties warning used to be an ``elif`` on the create branch, so
    # exactly the path that ignored the caller's properties was the one path
    # that never said so.
    assert result.warnings == [
        "pending claim appeared during create; attached to existing claim",
        "properties ignored because the claim tuple already exists",
    ]


def test_property_change_buckets_stale_content_and_zero_elides_empty_claims(
    attestation_instance: CruxibleInstance,
) -> None:
    old_claim = add_live_claim(attestation_instance, severity="high")
    _attest(attestation_instance, "support")
    changed_claim = add_live_claim(attestation_instance, severity="low")
    summary = service_corroboration_summaries(attestation_instance, [changed_claim])[CLAIM_KEY]
    assert summary.support_count == 0
    assert summary.stale_content.support_count == 1
    listing = service_list_attestations(attestation_instance, claim_key=CLAIM_KEY)
    assert listing.items[0].stale_content is True

    other = old_claim.model_copy(
        update={
            "from_id": "no-attestations",
            "properties": {"severity": "high"},
        }
    )
    assert service_corroboration_summaries(attestation_instance, [other]) == {}


def test_bulk_edge_read_uses_one_batched_summary_query(
    attestation_instance: CruxibleInstance,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    add_live_claim(attestation_instance)
    empty = service_list(attestation_instance, "edges")
    assert "corroboration" not in empty.items[0]
    _attest(attestation_instance, "support")
    calls = 0
    original = AttestationStore.summaries_for_claims

    def counted(
        self: AttestationStore,
        claim_digests: Mapping[ClaimKey, str],
    ) -> dict[ClaimKey, CorroborationSummary]:
        nonlocal calls
        calls += 1
        return original(self, claim_digests)

    monkeypatch.setattr(AttestationStore, "summaries_for_claims", counted)
    result = service_list(attestation_instance, "edges")
    assert calls == 1
    assert result.items[0]["corroboration"]["support_count"] == 1


def test_queue_and_disposition_lifecycle_with_latest_wins(
    attestation_instance: CruxibleInstance,
) -> None:
    claim = add_live_claim(attestation_instance)
    first = _attest(attestation_instance, "contradict", observer="same-actor")
    _attest(attestation_instance, "contradict", observer="same-actor")
    _attest(attestation_instance, "unsure", observer="other-actor")
    unchanged = attestation_instance.load_graph().get_relationship(
        "Service",
        "svc-1",
        "Control",
        "ctl-1",
        "protected_by",
    )
    assert unchanged is not None
    assert relationship_assertion_from_metadata(unchanged.metadata).review.status == "unreviewed"

    queue = service_attestation_queue(attestation_instance)
    assert queue.total == 1
    assert queue.items[0].open_contradict_count == 2
    assert queue.items[0].distinct_contradicting_actor_count == 1
    summary = service_corroboration_summaries(attestation_instance, [claim])[CLAIM_KEY]
    assert summary.contradict_count == 2
    assert summary.unsure_count == 1
    assert summary.distinct_actor_count == 2
    assert summary.last_contradicted_at == OBSERVED_AT
    assert summary.open_contradiction is True

    service_resolve_attestation(
        attestation_instance,
        first.attestation.attestation_id,
        verdict="upheld",
        actor_context=actor("reviewer"),
    )
    assert service_attestation_queue(attestation_instance).items[0].open_contradict_count == 1
    service_resolve_attestation(
        attestation_instance,
        first.attestation.attestation_id,
        verdict="invalidated",
        actor_context=actor("reviewer", "op-review-again"),
    )
    summary = service_corroboration_summaries(attestation_instance, [claim])[CLAIM_KEY]
    assert summary.contradict_count == 1
    assert summary.invalidated_count == 1

    second_id = next(
        item.attestation.attestation_id
        for item in service_list_attestations(
            attestation_instance,
            claim_key=CLAIM_KEY,
            stance="contradict",
        ).items
        if item.attestation.attestation_id != first.attestation.attestation_id
    )
    service_resolve_attestation(
        attestation_instance,
        second_id,
        verdict="corrected",
        follow_up_receipt_id=first.receipt_id,
        actor_context=actor("reviewer", "op-review-second"),
    )
    assert service_attestation_queue(attestation_instance).total == 0


def test_tuple_removal_surfaces_unresolved_and_excludes_summary(
    attestation_instance: CruxibleInstance,
) -> None:
    claim = add_live_claim(attestation_instance)
    _attest(attestation_instance, "support")
    graph = attestation_instance.load_graph()
    graph.remove_relationship(
        "Service",
        "svc-1",
        "Control",
        "ctl-1",
        "protected_by",
    )
    attestation_instance.save_graph(graph)
    listed = service_list_attestations(attestation_instance, claim_key=CLAIM_KEY)
    assert listed.items[0].unresolved_target is True
    assert service_corroboration_summaries(attestation_instance, []) == {}
    assert service_attestation_queue(attestation_instance).total == 0
    assert claim.relationship_type == "protected_by"


def test_actor_is_required_at_service_boundary(
    attestation_instance: CruxibleInstance,
) -> None:
    add_live_claim(attestation_instance)
    with pytest.raises(ConfigError, match="actor context is required"):
        service_attest(
            attestation_instance,
            relationship_type="protected_by",
            from_type="Service",
            from_id="svc-1",
            to_type="Control",
            to_id="ctl-1",
            stance="support",
            evidence_refs=[evidence()],
            observed_at=OBSERVED_AT,
            actor_context=None,
        )


def test_refusals_persist_mutation_receipts(
    attestation_instance: CruxibleInstance,
) -> None:
    with pytest.raises(ConfigError, match="only support") as excinfo:
        _attest(attestation_instance, "contradict")
    receipt_id = getattr(excinfo.value, "mutation_receipt_id", None)
    assert receipt_id is not None
    store = attestation_instance.get_receipt_store()
    try:
        assert store.get_receipt(receipt_id) is not None
    finally:
        store.close()


def test_idempotency_key_is_scoped_per_actor(
    attestation_instance: CruxibleInstance,
) -> None:
    add_live_claim(attestation_instance)
    first = _attest(
        attestation_instance, "support", observer="observer-a", idempotency_key="shared-key"
    )
    second = _attest(
        attestation_instance, "support", observer="observer-b", idempotency_key="shared-key"
    )
    assert first.attestation.attestation_id != second.attestation.attestation_id
    assert second.idempotent_replay is False
    assert service_list_attestations(attestation_instance).total == 2


def test_idempotent_replay_refuses_divergent_request(
    attestation_instance: CruxibleInstance,
) -> None:
    add_live_claim(attestation_instance)
    original = _attest(attestation_instance, "support", idempotency_key="diverge-key")
    with pytest.raises(ConfigError, match="diverges from the original"):
        _attest(attestation_instance, "contradict", idempotency_key="diverge-key")
    with pytest.raises(ConfigError, match="diverges from the original"):
        _attest(
            attestation_instance,
            "support",
            evidence_refs=[evidence("different")],
            idempotency_key="diverge-key",
        )
    replay = _attest(attestation_instance, "support", idempotency_key="diverge-key")
    assert replay.idempotent_replay is True
    assert replay.attestation.attestation_id == original.attestation.attestation_id


class TestReplayDivergenceCoversEveryPersistedField:
    """The replay diff used to compare 2 of the record's fields, not all of them.

    ``note``, ``observed_at`` and ``edge_key`` are persisted on
    ``AttestationRecord`` and diverged SILENTLY: a reused key carrying a
    different note or a different observation time returned the original record
    as an "idempotent replay" and dropped the second, distinct observation. The
    stronger shape existed in ``service/resolution_contracts`` and was
    back-ported.
    """

    def test_divergent_note_refuses(self, attestation_instance: CruxibleInstance) -> None:
        add_live_claim(attestation_instance)
        _attest(attestation_instance, "support", idempotency_key="note-key", note="first")
        with pytest.raises(ConfigError, match="diverges from the original.*note"):
            _attest(
                attestation_instance,
                "support",
                idempotency_key="note-key",
                note="a materially different reading",
            )

    def test_note_appearing_where_there_was_none_refuses(
        self, attestation_instance: CruxibleInstance
    ) -> None:
        """None -> a note is a divergence too; it is new content on a reused key."""
        add_live_claim(attestation_instance)
        _attest(attestation_instance, "support", idempotency_key="note-none-key")
        with pytest.raises(ConfigError, match="note"):
            _attest(
                attestation_instance,
                "support",
                idempotency_key="note-none-key",
                note="added after the fact",
            )

    def test_divergent_observed_at_refuses(self, attestation_instance: CruxibleInstance) -> None:
        add_live_claim(attestation_instance)
        _attest(attestation_instance, "support", idempotency_key="clock-key")
        with pytest.raises(ConfigError, match="diverges from the original.*observed_at"):
            _attest(
                attestation_instance,
                "support",
                idempotency_key="clock-key",
                observed_at=OBSERVED_AT - timedelta(days=30),
            )

    def test_divergent_edge_key_refuses(self, attestation_instance: CruxibleInstance) -> None:
        add_live_claim(attestation_instance)
        _attest(attestation_instance, "support", idempotency_key="edge-key-key")
        with pytest.raises(ConfigError, match="diverges from the original.*edge_key"):
            _attest(
                attestation_instance,
                "support",
                idempotency_key="edge-key-key",
                edge_key=4242,
            )

    def test_an_identical_replay_with_a_note_still_replays(
        self, attestation_instance: CruxibleInstance
    ) -> None:
        """The widened diff must not turn honest replays into refusals."""
        add_live_claim(attestation_instance)
        original = _attest(
            attestation_instance, "support", idempotency_key="same-key", note="same note"
        )
        replay = _attest(
            attestation_instance, "support", idempotency_key="same-key", note="same note"
        )
        assert replay.idempotent_replay is True
        assert replay.attestation.attestation_id == original.attestation.attestation_id

    def test_a_pre_identity_record_replays_after_its_edge_key_was_repointed(
        self, attestation_instance: CruxibleInstance
    ) -> None:
        """A repointed per-load key must NOT read as a divergence.

        ``edge_key`` is a per-load counter, not a stable identity: pulls and any
        other graph re-materialization can hand the same edge a different key.
        A pre-identity record (``claim_id`` NULL, from before claim minting)
        carries whatever key was current when it was recorded, so manufacturing
        the replay's key from the CURRENT relationship and diffing the two
        refused honest, unchanged, tuple-first replays on historical data.

        Seeded through the store to produce a record the current write path can
        no longer make -- which is the whole point.
        """
        relationship = add_live_claim(attestation_instance)
        recorded = _attest(attestation_instance, "support", idempotency_key="legacy-key")

        # Rewrite the stored row into its pre-identity shape: no claim_id, and a
        # stale edge_key from a load that no longer exists.
        stale_edge_key = (relationship.edge_key or 0) + 77
        store = attestation_instance.get_attestation_store()
        try:
            store._conn.execute(
                "UPDATE attestations SET claim_id = NULL, edge_key = ? WHERE attestation_id = ?",
                (stale_edge_key, recorded.attestation.attestation_id),
            )
            store._conn.commit()
        finally:
            store.close()

        replay = _attest(attestation_instance, "support", idempotency_key="legacy-key")

        assert replay.idempotent_replay is True
        assert replay.attestation.attestation_id == recorded.attestation.attestation_id

    def test_an_explicit_edge_key_still_diverges_on_a_pre_identity_record(
        self, attestation_instance: CruxibleInstance
    ) -> None:
        """Relaxing the manufactured comparison must not lose the real one.

        When the caller NAMES an edge_key, that is a deliberate reference and a
        mismatch is a genuine divergence — independent of whether the original
        row has a stable identity.
        """
        relationship = add_live_claim(attestation_instance)
        recorded = _attest(attestation_instance, "support", idempotency_key="legacy-explicit")

        stale_edge_key = (relationship.edge_key or 0) + 77
        store = attestation_instance.get_attestation_store()
        try:
            store._conn.execute(
                "UPDATE attestations SET claim_id = NULL, edge_key = ? WHERE attestation_id = ?",
                (stale_edge_key, recorded.attestation.attestation_id),
            )
            store._conn.commit()
        finally:
            store.close()

        with pytest.raises(ConfigError, match="diverges from the original.*edge_key"):
            _attest(
                attestation_instance,
                "support",
                idempotency_key="legacy-explicit",
                edge_key=stale_edge_key + 1,
            )


def test_a_stale_claim_id_refuses_instead_of_silently_retargeting_the_tuple(
    attestation_instance: CruxibleInstance,
) -> None:
    """A stale id used to re-resolve tuple-first AND invent a race warning.

    The unknown id fell through to the create branch, which re-resolved by tuple,
    hit the existing claim, and reported "pending claim appeared during create"
    -- a concurrency story for something no concurrency caused. The observation
    landed on a claim the caller had never seen.
    """
    live = add_live_claim(attestation_instance)
    with pytest.raises(ConfigError) as excinfo:
        _attest(attestation_instance, "support", claim_id="CLM-staleref00000")
    message = str(excinfo.value)
    assert "CLM-staleref00000" in message
    assert live.claim_id is not None and live.claim_id in message
    assert "appeared during create" not in message
    assert service_list_attestations(attestation_instance).total == 0


def test_replay_refuses_a_different_claim_target_on_the_same_tuple(
    attestation_instance: CruxibleInstance,
) -> None:
    """Same tuple, same stance, same evidence -- DIFFERENT claim.

    Comparing only stance and evidence made a second, genuinely distinct
    observation disappear: it returned the first record as "idempotent" while
    the claim it was actually about went unrecorded.
    """
    first_claim = add_live_claim(attestation_instance)
    original = _attest(attestation_instance, "support", idempotency_key="target-key")
    assert original.attestation.claim_id == first_claim.claim_id

    # A second, PARALLEL claim on the same 5-tuple: identical natural key,
    # different identity. Exactly what claim_id exists to tell apart.
    graph = attestation_instance.load_graph()
    sibling_id = mint_claim_id()
    graph.add_relationship(
        RelationshipInstance(
            relationship_type="protected_by",
            from_type="Service",
            from_id="svc-1",
            to_type="Control",
            to_id="ctl-1",
            properties={"severity": "low"},
            claim_id=sibling_id,
        )
    )
    attestation_instance.save_graph(graph)

    with pytest.raises(ConfigError, match="claim target"):
        _attest(
            attestation_instance,
            "support",
            claim_id=sibling_id,
            idempotency_key="target-key",
        )
    # ...and the untouched replay still replays.
    replay = _attest(
        attestation_instance,
        "support",
        claim_id=first_claim.claim_id,
        idempotency_key="target-key",
    )
    assert replay.idempotent_replay is True
    assert replay.attestation.attestation_id == original.attestation.attestation_id


def test_corrected_disposition_refuses_fabricated_follow_up_receipt(
    attestation_instance: CruxibleInstance,
) -> None:
    add_live_claim(attestation_instance)
    recorded = _attest(attestation_instance, "contradict")
    with pytest.raises(ConfigError, match="does not resolve"):
        service_resolve_attestation(
            attestation_instance,
            recorded.attestation.attestation_id,
            verdict="corrected",
            actor_context=actor("reviewer"),
            follow_up_receipt_id="RCP-fabricated",
        )


class TestAttestingAdvancesReadRevision:
    """Attesting DOES advance ``read_revision``, and that is correct.

    An earlier pass at this batch exempted the attestation and
    resolution-contract tables from ``_AUDIT_ONLY_TABLES`` on the theory that
    they are a pure audit lane, since neither an attestation nor a disposition
    can touch a claim's trust, review, or lifecycle status. That reasoning
    covered only the WRITE side and was wrong: these tables change what ordinary
    reads RETURN. Corroboration summaries are computed from ``attestations`` and
    attached to edge payloads on plain edge reads, the queues stamp
    ``read_revision`` from them, and continuation tokens validate on
    ``read_revision`` alone — so exempting them produced paginated reads that
    silently spanned two different states.

    The protocol audit's row was a DISCLOSURE gap, not a behavior bug. These
    tests pin the behavior; ``docs/state-resolution-and-maintenance.md``
    discloses it.
    """

    def test_attest_against_a_live_claim_advances_the_revision(
        self, attestation_instance: CruxibleInstance
    ) -> None:
        add_live_claim(attestation_instance)
        before = attestation_instance.get_read_revision()

        result = _attest(attestation_instance, "support")

        assert result.created_claim is False, "no graph write — the attest alone must move it"
        assert attestation_instance.get_read_revision() > before

    def test_a_disposition_advances_the_revision(
        self, attestation_instance: CruxibleInstance
    ) -> None:
        add_live_claim(attestation_instance)
        recorded = _attest(attestation_instance, "contradict")
        before = attestation_instance.get_read_revision()

        service_resolve_attestation(
            attestation_instance,
            recorded.attestation.attestation_id,
            verdict="upheld",
            actor_context=actor("reviewer"),
        )

        assert attestation_instance.get_read_revision() > before

    def test_an_attest_changes_what_a_plain_edge_read_returns(
        self, attestation_instance: CruxibleInstance
    ) -> None:
        """The reason the revision must move: corroboration rides on edge reads.

        This is the fact that falsified the exemption. Nothing about the edge
        itself changed, but the payload a reader gets back did.
        """
        add_live_claim(attestation_instance)
        payload_before = _edge_payload(attestation_instance)
        assert payload_before.get("corroboration", {}).get("contradict_count", 0) == 0

        _attest(attestation_instance, "contradict")

        payload_after = _edge_payload(attestation_instance)
        assert payload_after["corroboration"]["contradict_count"] == 1

    def test_an_attest_invalidates_an_outstanding_edge_list_continuation_token(
        self, attestation_instance: CruxibleInstance
    ) -> None:
        """Tokens bind to ``read_revision`` alone, so the bump is what protects paging.

        Without it, page 1 could be read at revision N, a contradiction
        recorded, and page 2's token still validate — returning rows whose
        corroboration reflects a different moment than page 1's, with nothing in
        the response able to detect it.
        """
        add_live_claim(attestation_instance)
        token = mint_continuation_token(
            surface="list",
            instance_key=str(attestation_instance.get_root_path()),
            config_digest=_config_digest(attestation_instance),
            read_revision=attestation_instance.get_read_revision(),
            filter_hash="test-filters",
            cursor={"offset": 1},
        )

        _attest(attestation_instance, "contradict")

        with pytest.raises(StaleContinuationError):
            validate_continuation_token(
                decode_continuation_token(token),
                surface="list",
                instance_key=str(attestation_instance.get_root_path()),
                config_digest=_config_digest(attestation_instance),
                read_revision=attestation_instance.get_read_revision(),
                filter_hash="test-filters",
            )

    def test_an_attest_that_mints_a_pending_claim_also_advances_the_revision(
        self, attestation_instance: CruxibleInstance
    ) -> None:
        before = attestation_instance.get_read_revision()
        result = _attest(attestation_instance, "support", properties={"severity": "high"})
        assert result.created_claim is True
        assert attestation_instance.get_read_revision() > before


def test_support_on_an_absent_endpoint_names_the_recovery(
    attestation_instance: CruxibleInstance,
) -> None:
    """An attestation carries no entities, so the recovery is a prior write.

    A 'support' stance mints the claim, which means a missing endpoint is a
    routine first-contact failure. The rejection names the tools that create the
    endpoint instead of leaving the caller to rediscover them.
    """
    with pytest.raises(ConfigError) as exc:
        service_attest(
            attestation_instance,
            relationship_type="protected_by",
            from_type="Service",
            from_id="svc-absent",
            to_type="Control",
            to_id="ctl-absent",
            stance="support",
            evidence_refs=[evidence()],
            observed_at=OBSERVED_AT,
            actor_context=actor("observer"),
            properties={},
        )
    message = str(exc.value)
    assert "entity Service:svc-absent not found" in message
    assert "create the entity first" in message
    assert "cruxible_add_entity" in message
