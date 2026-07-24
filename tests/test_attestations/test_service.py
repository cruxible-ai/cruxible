"""D2-D5 and D8 service semantics."""
# mypy: disable-error-code=no-untyped-def

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone

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
from cruxible_core.service import (
    service_attest,
    service_attestation_queue,
    service_corroboration_summaries,
    service_list,
    service_list_attestations,
    service_resolve_attestation,
)
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
    properties: dict[str, object] | None = None,
    idempotency_key: str | None = None,
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
        observed_at=OBSERVED_AT,
        actor_context=actor(observer),
        edge_key=edge_key,
        properties=properties,
        idempotency_key=idempotency_key,
    )


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
    assert listed.items[0].edge_key_mismatch is True
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
    assert result.warnings == ["pending claim appeared during create; attached to existing claim"]


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
        follow_up_receipt_id="RCP-follow-up",
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
