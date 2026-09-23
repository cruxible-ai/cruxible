"""Remembered slot answers survive accepts only when every read they made still holds."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from cruxible_client.contracts.captures import DirectForeignSourceSelectionV1
from cruxible_client.contracts.semantic import ContentSpan
from cruxible_core.service.authoring.documents import PlaybillAcceptedCoordinate
from cruxible_core.service.discovery import search as playbill_search
from cruxible_core.service.evidence.evidence import ClaimVerdictReadContext
from tests.core_support._candidate_support import submit_query_definition_candidate
from tests.core_support._knowledge_loop_support import (
    TIMESTAMP,
    accept_proposal,
    activate,
    authoring,
    seed_claims,
    service_propose_playbill_claim,
    work_item_query,
)
from tests.test_integration.test_playbill_search import EVALUATION_TIME


def _derive(instance, *, when=EVALUATION_TIME, fresh: bool):
    playbill_search.reset_claim_resolution_memo(slots=fresh)
    coordinate = instance.accepted_coordinate()
    context = ClaimVerdictReadContext(instance, coordinate)
    verdicts: dict = {}
    statuses = playbill_search.claim_resolution_statuses(
        instance,
        claims=context.claims(),
        at=PlaybillAcceptedCoordinate.from_internal(coordinate),
        evaluation_time=when,
        verdicts_by_identity=verdicts,
        read_context=context,
    )
    return statuses, {
        identity: verdict.model_dump(mode="json") for identity, verdict in verdicts.items()
    }


def _add_claim(instance, owner, subject_id: str, value: str, *, name: str) -> None:
    body = instance.body_store().store(f"status: {value}".encode())
    proposed = service_propose_playbill_claim(
        instance,
        authoring=authoring(subject_id, value, with_claim_type=False).model_copy(
            update={
                "source_selection": DirectForeignSourceSelectionV1(
                    logical_source_identity="fixture.work-items",
                    span=ContentSpan(
                        content_digest=body.digest,
                        start_byte=0,
                        end_byte=len(f"status: {value}".encode()),
                    ),
                )
            }
        ),
        actor_id="owner",
        proposal_name=name,
        timestamp=TIMESTAMP,
    )
    activate(instance, owner, proposed)


def test_slot_answers_are_reused_exactly_when_their_reads_hold(tmp_path: Path, monkeypatch):
    instance, owner = seed_claims(tmp_path)
    derived: list[str] = []
    resolve = playbill_search.resolve_playbill_claim_group

    def counted(instance, *, subject, **kwargs):
        derived.append(subject.artifact_path)
        return resolve(instance, subject=subject, **kwargs)

    monkeypatch.setattr(playbill_search, "resolve_playbill_claim_group", counted)

    def check(expected_derived: set[str], **kwargs) -> dict:
        # Warm first, from slots remembered at the previous coordinate; the fresh
        # derivation then forgets everything and becomes the next step's memory.
        derived.clear()
        warm = _derive(instance, fresh=False, **kwargs)
        assert set(derived) == expected_derived
        fresh = _derive(instance, fresh=True, **kwargs)
        assert warm == fresh
        return fresh[0]

    # Populate the remembered slots.
    _derive(instance, fresh=True)

    # An unrelated accept changes nothing any slot read.
    accept_proposal(
        instance,
        owner,
        submit_query_definition_candidate(
            instance,
            query=work_item_query("unrelated.query"),
            actor_id="owner",
            proposal_name="unrelated",
            timestamp=TIMESTAMP,
        ),
    )
    check(set())

    # A Claim about a new Subject opens one new slot; the others are reused.
    _add_claim(instance, owner, "wi-44", "ready", name="new-slot")
    check({"subjects/project.work_item/wi-44.json"})

    # A contender joining an existing slot changes that slot's membership only.
    _add_claim(instance, owner, "wi-42", "done", name="contender")
    check({"subjects/project.work_item/wi-42.json"})

    # Retained evidence disappearing changes replay availability for its slot.
    digest = instance.body_store().digest_bytes(b"status: blocked").tagged
    instance.body_store()._path(digest).unlink()
    check({"subjects/project.work_item/wi-43.json"})

    # Later instants are served only inside each slot's invariance interval.
    later = EVALUATION_TIME + timedelta(days=3650)
    fresh = _derive(instance, fresh=True, when=later)
    assert _derive(instance, fresh=False, when=later) == fresh


def test_a_verdict_that_iterates_every_provider_is_refused_rather_than_remembered():
    from cruxible_client.contracts.errors import ProposalIntegrityError
    from cruxible_core.service.evidence.evidence import VerdictReads, _RecordingProviders

    providers = _RecordingProviders({}, VerdictReads())
    with pytest.raises(ProposalIntegrityError):
        list(providers)
