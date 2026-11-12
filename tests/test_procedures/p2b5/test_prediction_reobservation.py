"""A recorded re-observation settles a prediction (maintainer ruling 2026-11-12).

A Claim stated before its prediction was accepted is not, by its own accepted
time, an observation inside the prediction's window. A later run that observes
the unchanged value records a re-observation of that live version; settle
reads it as an observation of the version at the re-observing Capture's
observed time, and cites the Capture and the journal record. Nothing about the
Claim changes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.claims import claim_artifact_digest
from cruxible_client.contracts.predictions import (
    PredictionEqualityRule,
    PredictionObservationSelector,
    PredictRequest,
    SettleRequest,
)
from cruxible_client.contracts.procedures.windows import FixedWindow
from cruxible_client.contracts.resolution_contracts import ResolutionContract
from cruxible_client.contracts.semantic import ContentSpan
from cruxible_client.contracts.temporal import format_datetime, parse_datetime
from cruxible_core.evidence.claim_reobservations import (
    ClaimReobservationStore,
    ReobservationReader,
)
from cruxible_core.governance.actor_context import GovernedActorContext
from cruxible_core.proposals.proposals import AuthenticatedActor
from cruxible_core.service.authoring.documents import service_inspect_playbill_proposal
from cruxible_core.service.procedures.predictions import (
    PredictionRefused,
    service_predict_playbill,
    service_settle_playbill_prediction,
)
from cruxible_core.service.procedures.resolution_contracts import resolve_claim_version
from tests.core_support._claim_authoring_support import (
    DirectClaimAuthoringV1,
    service_propose_playbill_claim,
    service_reobserve_playbill_claim,
)
from tests.core_support._knowledge_loop_support import accept_proposal
from tests.test_integration.test_next_closed_loop import (
    DirectForeignSourceSelection,
    _activate_direct_claim,
    _current_claim,
    _freshness_world,
)

#: The freshness world states its Claim at 20:00 and migrates its ClaimType after.
_START = datetime(2026, 8, 16, 20, 0, tzinfo=UTC)
PREDICTED_AT = _START + timedelta(hours=1)
WINDOW = timedelta(hours=2)


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


def _reobserve(instance, at: datetime, *, suffix: bytes = b"\n") -> None:  # type: ignore[no-untyped-def]
    """A later run observes the unchanged value: a recorded re-observation."""

    service_reobserve_playbill_claim(
        instance,
        authoring=_restating(instance, body=b"status: ready" + suffix, start=8, end=13),
        actor_id="owner",
        timestamp=_stamp(at),
    )


def _predict(instance, owner, *, starts_at: datetime = PREDICTED_AT):  # type: ignore[no-untyped-def]
    """Predict the stated Claim holds; its own lineage's observation settles it."""

    claim = _current_claim(instance)
    contract = ResolutionContract(
        identity=ArtifactIdentity(kind="ResolutionContract", name="status-holds"),
        hypothesis=resolve_claim_version(instance, claim.identity.name),
        observation=PredictionObservationSelector(
            subject=claim.statement.subject,
            predicate=claim.statement.predicate,
            qualifier=claim.statement.qualifier,
            role=claim.statement.role,
        ),
        rule=PredictionEqualityRule(),
        window=FixedWindow(starts_at=starts_at, duration_seconds=int(WINDOW.total_seconds())),
    )
    proposed = service_predict_playbill(
        instance,
        request=PredictRequest(contract=contract),
        actor=AuthenticatedActor(actor_id="owner"),
        evaluation_time=PREDICTED_AT,
    )
    accept_proposal(
        instance,
        owner,
        service_inspect_playbill_proposal(instance, proposal_id=proposed.proposal_id),
    )
    return claim


def _settle(instance, claim, *, at: datetime):  # type: ignore[no-untyped-def]
    return service_settle_playbill_prediction(
        instance,
        prediction_id="status-holds",
        request=SettleRequest(observation=claim.identity.name),
        actor_context=GovernedActorContext(
            actor_type="human_user",
            actor_id="owner",
            org_id="inst_playbill_test",
            operation_id="settle-owner",
            timestamp=at,
        ),
        recorded_at=at,
    )


def test_a_recorded_reobservation_settles_with_the_captures_time(tmp_path: Path) -> None:
    instance, owner = _freshness_world(tmp_path)
    claim = _predict(instance, owner)
    # Stated before the prediction: by its accepted time alone it cannot settle.
    with pytest.raises(PredictionRefused) as refused:
        _settle(instance, claim, at=PREDICTED_AT + timedelta(minutes=10))
    assert refused.value.code == "prediction_observation_predates_contract"
    assert "needs a re-observation recorded after the prediction was accepted" in str(refused.value)

    reobserved_at = PREDICTED_AT + timedelta(minutes=30)
    _reobserve(instance, reobserved_at)
    (record,) = ClaimReobservationStore(instance).history(claim.identity.name)
    assert _current_claim(instance) == claim  # nothing about the Claim changed

    result = _settle(instance, claim, at=reobserved_at + timedelta(minutes=5))
    resolution = result.resolution
    assert parse_datetime(resolution["observed_at"]) == reobserved_at
    assert resolution["settlement_outcome"] is True
    kinds = {ref["kind"]: ref["digest"] for ref in resolution["evidence_refs"]}
    assert kinds["capture"] == record.capture_digest
    assert set(kinds) == {"capture", "claim_statement", "journal_record"}
    source = resolution["value"]["authorization_source"]
    assert source["kind"] == "reobservation_admission"
    assert source["reobserving_capture_digest"] == record.capture_digest
    assert source["reobservation_record_digest"] == kinds["journal_record"]


def test_a_reobservation_before_the_prediction_was_accepted_does_not_settle(
    tmp_path: Path,
) -> None:
    instance, owner = _freshness_world(tmp_path)
    reobserved_at = PREDICTED_AT - timedelta(minutes=5)
    _reobserve(instance, reobserved_at)
    claim = _predict(instance, owner, starts_at=PREDICTED_AT - timedelta(minutes=30))
    with pytest.raises(PredictionRefused) as refused:
        _settle(instance, claim, at=PREDICTED_AT + timedelta(minutes=10))
    assert refused.value.code == "prediction_observation_predates_contract"
    # Judged by the latest observation of the version: the re-observation.
    assert refused.value.details == {
        "observed_at": format_datetime(reobserved_at),
        "contract_accepted_at": format_datetime(PREDICTED_AT),
    }


def test_an_observation_before_the_window_opens_does_not_settle(tmp_path: Path) -> None:
    instance, owner = _freshness_world(tmp_path)
    opens = PREDICTED_AT + timedelta(hours=1)
    claim = _predict(instance, owner, starts_at=opens)
    reobserved_at = PREDICTED_AT + timedelta(minutes=30)
    _reobserve(instance, reobserved_at)
    with pytest.raises(PredictionRefused) as refused:
        _settle(instance, claim, at=reobserved_at + timedelta(minutes=5))
    assert refused.value.code == "prediction_window_not_open"
    assert refused.value.details == {
        "observed_at": format_datetime(reobserved_at),
        "window_opens_at": format_datetime(opens),
    }


def test_a_reobservation_of_a_superseded_version_is_not_used(tmp_path: Path) -> None:
    instance, owner = _freshness_world(tmp_path)
    first = _predict(instance, owner)
    _reobserve(instance, PREDICTED_AT + timedelta(minutes=30))
    # The Claim is revised after the window closed, on other observed content.
    revised_at = PREDICTED_AT + WINDOW + timedelta(minutes=30)
    proposed = service_propose_playbill_claim(
        instance,
        authoring=_restating(instance, body=b"status: ready\n", start=0, end=6),
        actor_id="owner",
        proposal_name="other-content",
        timestamp=_stamp(revised_at),
    )
    _activate_direct_claim(instance, owner, proposed)
    second = _current_claim(instance)
    assert claim_artifact_digest(second) != claim_artifact_digest(first)
    # The latest re-observation was recorded while the first version was live:
    # it is no observation of the revision, which itself landed too late.
    with pytest.raises(PredictionRefused) as refused:
        _settle(instance, second, at=revised_at + timedelta(minutes=5))
    assert refused.value.code == "prediction_deadline_passed"


def test_a_reobservation_after_the_window_closed_does_not_settle(tmp_path: Path) -> None:
    instance, owner = _freshness_world(tmp_path)
    claim = _predict(instance, owner)
    late = PREDICTED_AT + WINDOW + timedelta(minutes=1)
    _reobserve(instance, late)
    with pytest.raises(PredictionRefused) as refused:
        _settle(instance, claim, at=late + timedelta(minutes=5))
    assert refused.value.code == "prediction_deadline_passed"
    assert refused.value.details == {
        "observed_at": format_datetime(late),
        "window_closed_at": format_datetime(PREDICTED_AT + WINDOW),
    }


def test_a_later_reobservation_past_the_close_does_not_hide_one_inside_the_window(
    tmp_path: Path,
) -> None:
    instance, owner = _freshness_world(tmp_path)
    claim = _predict(instance, owner)
    inside = PREDICTED_AT + timedelta(minutes=30)
    _reobserve(instance, inside)
    _reobserve(instance, PREDICTED_AT + WINDOW + timedelta(minutes=1), suffix=b"\n\n")
    assert len(ClaimReobservationStore(instance).history(claim.identity.name)) == 2
    result = _settle(instance, claim, at=PREDICTED_AT + WINDOW + timedelta(minutes=10))
    assert parse_datetime(result.resolution["observed_at"]) == inside
    assert result.resolution["settlement_outcome"] is True


def test_settle_reads_one_reobservation_record_not_the_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_core.evidence.claim_reobservations import _VERSION_RECORD
    from cruxible_core.exhaust.backends import LocalJournalBackend
    from cruxible_core.exhaust.records import CLAIM_REOBSERVATION_JOURNAL_FAMILY

    instance, owner = _freshness_world(tmp_path)
    claim = _predict(instance, owner)
    # Three inside the window, then many observed after it closed.
    for minutes in (10, 20, 30):
        _reobserve(instance, PREDICTED_AT + timedelta(minutes=minutes), suffix=b"\n" * minutes)
    late = 12
    for minutes in range(1, late + 1):
        _reobserve(
            instance, PREDICTED_AT + WINDOW + timedelta(minutes=minutes), suffix=b" " * minutes
        )
    assert len(ClaimReobservationStore(instance).history(claim.identity.name)) == 3 + late

    reader = ReobservationReader.of(instance)
    probes_before = reader.probes
    read: list[int] = []
    select = LocalJournalBackend.select_records

    def counting(self, stream, **filters):  # type: ignore[no-untyped-def]
        found = select(self, stream, **filters)
        if stream.journal_family == CLAIM_REOBSERVATION_JOURNAL_FAMILY:
            read.append(len(found))
        return found

    monkeypatch.setattr(LocalJournalBackend, "select_records", counting)
    result = _settle(instance, claim, at=PREDICTED_AT + WINDOW + timedelta(minutes=30))
    assert parse_datetime(result.resolution["observed_at"]) == PREDICTED_AT + timedelta(minutes=30)
    # One indexed seek and one journal record, however many were observed later.
    assert reader.probes - probes_before == 1
    assert sum(read) == 1
    conn = reader._connection()
    assert conn is not None
    (plan,) = (
        row[-1] for row in conn.execute(f"EXPLAIN QUERY PLAN {_VERSION_RECORD}", ("c", "v", 0, 0))
    )
    assert "USING INDEX reobservations_by_version" in plan
    assert "observed_us<?" in plan
