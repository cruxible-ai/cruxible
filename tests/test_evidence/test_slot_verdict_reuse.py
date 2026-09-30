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


def test_evidence_vanishing_while_a_slot_is_remembered_is_not_masked(tmp_path, monkeypatch):
    instance, _owner = seed_claims(tmp_path)
    context = ClaimVerdictReadContext(instance, instance.accepted_coordinate())
    capture = next(
        claim.backing.capture_digests[0]
        for claim in context.claims()
        if claim.statement.subject.artifact_path.endswith("wi-43.json")
    )
    snapshot = ClaimVerdictReadContext.snapshot

    def evidence_vanishes_before_recording(self, reads):
        path = instance.body_store()._path(capture)
        if path.exists():
            path.unlink()
        return snapshot(self, reads)

    # Derive with the evidence present; it disappears before the slot is stored.
    monkeypatch.setattr(ClaimVerdictReadContext, "snapshot", evidence_vanishes_before_recording)
    _derive(instance, fresh=True)
    monkeypatch.setattr(ClaimVerdictReadContext, "snapshot", snapshot)

    assert _derive(instance, fresh=False) == _derive(instance, fresh=True)


def test_availability_is_not_remembered_across_a_change_during_its_derivation(
    tmp_path, monkeypatch
):
    from cruxible_core.service.evidence import evidence as playbill_evidence

    instance, _owner = seed_claims(tmp_path)
    capture = (
        ClaimVerdictReadContext(instance, instance.accepted_coordinate())
        .claims()[0]
        .backing.capture_digests[0]
    )
    playbill_evidence._AVAILABILITY_MEMO.clear()
    identity = playbill_evidence._cas_file_identity
    vanished = []

    def evidence_vanishes_before_observation(store, digest):
        if not vanished:
            store._path(capture).unlink()
            vanished.append(digest)
        return identity(store, digest)

    monkeypatch.setattr(
        playbill_evidence, "_cas_file_identity", evidence_vanishes_before_observation
    )
    available = playbill_evidence._current_replay_available
    assert available(instance, capture, readers={}) is False
    monkeypatch.setattr(playbill_evidence, "_cas_file_identity", identity)
    assert available(instance, capture, readers={}) is False
    assert playbill_evidence._replay_available(instance, capture, readers={}) is False


def test_a_shared_capture_observed_inconsistently_is_not_remembered(tmp_path, monkeypatch):
    from cruxible_core.service.evidence import evidence as playbill_evidence
    from tests.test_authoring.test_authoring_existing_capture import shared_capture_world

    instance, *_rest = shared_capture_world(tmp_path)
    claims = ClaimVerdictReadContext(instance, instance.accepted_coordinate()).claims()
    digests = {claim.backing.capture_digests[0] for claim in claims}
    assert len(claims) == 2 and len(digests) == 1  # one slot, one shared Capture
    (capture,) = digests
    available = playbill_evidence._current_replay_available

    def vanishes_after_first_use(instance_, digest, **kwargs):
        answer = available(instance_, digest, **kwargs)
        path = instance.body_store()._path(capture)
        if digest == capture and path.exists():
            path.unlink()
        return answer

    # The first Claim sees the evidence; it is gone before the second is evaluated.
    playbill_evidence._AVAILABILITY_MEMO.clear()
    monkeypatch.setattr(playbill_evidence, "_current_replay_available", vanishes_after_first_use)
    _derive(instance, fresh=True)
    monkeypatch.setattr(playbill_evidence, "_current_replay_available", available)

    assert _derive(instance, fresh=False) == _derive(instance, fresh=True)


def _rewrite_in_place(path: Path, *, keep_mtime: bool) -> None:
    import os

    before = path.stat()
    os.chmod(path, 0o600)
    content = path.read_bytes()
    path.write_bytes(bytes([content[0] ^ 1]) + content[1:])
    if keep_mtime:
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))


def _outcome(call):  # type: ignore[no-untyped-def]
    try:
        return call()
    except Exception as exc:  # noqa: BLE001 - the refusal itself is the outcome compared
        return (type(exc).__name__, str(exc))


def _remembered_derivation(instance):  # type: ignore[no-untyped-def]
    coordinate = instance.accepted_coordinate()
    context = ClaimVerdictReadContext(instance, coordinate)
    return playbill_search.claim_resolution_statuses(
        instance,
        claims=context.claims(),
        at=PlaybillAcceptedCoordinate.from_internal(coordinate),
        evaluation_time=EVALUATION_TIME,
        read_context=context,
    )


@pytest.mark.parametrize("keep_mtime", [False, True], ids=["rewrite", "rewrite-keep-mtime"])
def test_a_remembered_derivation_is_not_served_over_a_body_rewritten_in_place(
    tmp_path: Path, keep_mtime: bool
) -> None:
    """The shard fingerprint sees arrivals and removals; the body fingerprints see the rest."""

    instance, _owner = seed_claims(tmp_path)
    context = ClaimVerdictReadContext(instance, instance.accepted_coordinate())
    capture = context.claims()[0].backing.capture_digests[0]
    fingerprint = playbill_search.verdict_input_fingerprint(instance)
    _derive(instance, fresh=True)

    _rewrite_in_place(instance.body_store()._path(capture), keep_mtime=keep_mtime)
    # The keyed shard fingerprint does not move; the entry must still not be served.
    assert playbill_search.verdict_input_fingerprint(instance) == fingerprint
    # Served straight from the remembered derivation: nothing is reset.
    warm = _outcome(lambda: _remembered_derivation(instance))
    cold = _outcome(lambda: _derive(instance, fresh=True))
    assert warm == cold
    assert warm[0] == "PlaybillCasError"


@pytest.mark.parametrize("keep_mtime", [False, True], ids=["rewrite", "rewrite-keep-mtime"])
def test_get_does_not_serve_a_stale_verdict_after_a_ruling_is_rewritten_in_place(
    tmp_path: Path, keep_mtime: bool
) -> None:
    from cruxible_client.contracts.get_reads import PlaybillGetRequestV1
    from cruxible_client.contracts.write import PlaybillWriteRequestV1
    from cruxible_core.service.authoring.write_verbs import service_playbill_write
    from cruxible_core.service.discovery.get import service_playbill_get
    from cruxible_core.storage.cas import BodyAccessContext
    from tests.core_support._write_support import KIND, caller, seed_write_surface

    instance, _owner = seed_write_surface(tmp_path)
    outcome = service_playbill_write(
        instance,
        request=PlaybillWriteRequestV1.model_validate(
            {
                "because": "The ruling as written.",
                "changes": [
                    {"op": "set", "subject": f"{KIND}/wi-1", "field": "ruling", "value": "Ruled.\n"}
                ],
            }
        ),
        caller=caller(),
    )
    assert outcome.status == "accepted", outcome
    claim_id = outcome.changes[0].claim
    request = PlaybillGetRequestV1(ref=str(claim_id))
    access = BodyAccessContext(principal_id="owner")

    def read() -> object:
        return service_playbill_get(instance, request=request, access=access).model_dump(
            mode="json", exclude={"evaluation_time"}
        )

    assert read()["card"]["verdict"] == "supported"  # type: ignore[index]
    with instance.bind_accepted_projection(instance.accepted_coordinate()) as projection:
        digest = projection.typed.source(f"Claim:{claim_id}").statement.object.content_digest
    _rewrite_in_place(instance.body_store()._path(digest), keep_mtime=keep_mtime)

    warm = _outcome(read)
    playbill_search.reset_claim_resolution_memo()
    assert warm == _outcome(read)
    assert warm[0] == "PlaybillCasError"


def _capture_and_source(instance, capture: str) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    from cruxible_client.contracts.captures import CasSourceReferenceV1, parse_capture_envelope
    from cruxible_core.storage.cas import BodyAccessContext

    envelope = parse_capture_envelope(
        instance.body_store().read(
            capture, access=BodyAccessContext(principal_id="test", can_read_body=True)
        )
    )
    # The source bytes replay availability consults after the Capture itself.
    if isinstance(envelope.source, CasSourceReferenceV1):
        return capture, envelope.source.content_digest
    assert envelope.commitment.materialization == "cas"
    return capture, envelope.commitment.digest


@pytest.mark.parametrize("which", ["capture", "source"])
def test_a_body_rewritten_after_its_read_but_before_the_memo_insert_is_not_remembered(
    tmp_path: Path, monkeypatch, which: str
) -> None:
    """Identities are the ones each read used, never ones collected afterwards.

    Between the derivation's reads and the memo insert, the availability memo is
    evicted (as concurrent activity would) and a body the verdicts read is
    rewritten in place with its size and mtime kept. The entry must not record
    the rewritten file's identity against verdicts derived from the old bytes.
    """

    from cruxible_core.service.evidence import evidence

    instance, _owner = seed_claims(tmp_path)
    context = ClaimVerdictReadContext(instance, instance.accepted_coordinate())
    capture = context.claims()[0].backing.capture_digests[0]
    target = dict(zip(("capture", "source"), _capture_and_source(instance, capture)))[which]
    snapshot = ClaimVerdictReadContext.snapshot
    rewritten: list[bool] = []

    def evict_and_rewrite_before_insert(self, reads, **kwargs):  # type: ignore[no-untyped-def]
        # The last read before the insert; the window opens as it returns.
        values = snapshot(self, reads, **kwargs)
        if not rewritten:
            evidence._AVAILABILITY_MEMO.clear()
            _rewrite_in_place(instance.body_store()._path(target), keep_mtime=True)
            rewritten.append(True)
        return values

    playbill_search.reset_claim_resolution_memo()
    evidence._AVAILABILITY_MEMO.clear()
    monkeypatch.setattr(ClaimVerdictReadContext, "snapshot", evict_and_rewrite_before_insert)
    _remembered_derivation(instance)  # derives from the good bytes; the body moves
    monkeypatch.setattr(ClaimVerdictReadContext, "snapshot", snapshot)
    assert rewritten

    warm = _outcome(lambda: _remembered_derivation(instance))
    cold = _outcome(lambda: _derive(instance, fresh=True))
    assert warm == cold
    assert warm[0] == "PlaybillCasError"


@pytest.mark.parametrize("which", ["capture", "source"])
def test_an_availability_evicted_before_the_insert_keeps_every_dependency(
    tmp_path: Path, monkeypatch, which: str
) -> None:
    """Evicting the availability memo mid-request loses no dependency.

    The memo is evicted between the derivation's reads and the insert, then,
    once the derivation is remembered, a consulted body (the Capture, or the
    source bytes its availability also read) is rewritten in place with its
    mtime kept: the next read must re-derive rather than serve the old answer.
    """

    from cruxible_core.service.evidence import evidence

    instance, _owner = seed_claims(tmp_path)
    context = ClaimVerdictReadContext(instance, instance.accepted_coordinate())
    capture = context.claims()[0].backing.capture_digests[0]
    target = dict(zip(("capture", "source"), _capture_and_source(instance, capture)))[which]
    snapshot = ClaimVerdictReadContext.snapshot

    def evict_before_insert(self, reads, **kwargs):  # type: ignore[no-untyped-def]
        values = snapshot(self, reads, **kwargs)
        evidence._AVAILABILITY_MEMO.clear()
        return values

    playbill_search.reset_claim_resolution_memo()
    evidence._AVAILABILITY_MEMO.clear()
    monkeypatch.setattr(ClaimVerdictReadContext, "snapshot", evict_before_insert)
    _remembered_derivation(instance)
    monkeypatch.setattr(ClaimVerdictReadContext, "snapshot", snapshot)
    _rewrite_in_place(instance.body_store()._path(target), keep_mtime=True)

    warm = _outcome(lambda: _remembered_derivation(instance))
    cold = _outcome(lambda: _derive(instance, fresh=True))
    assert warm == cold
    assert warm[0] == "PlaybillCasError"
