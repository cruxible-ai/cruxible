"""A cited Capture must be one a daemon run is recorded producing.

Any governed writer can store bytes in the CAS, so a Capture envelope is only
evidence when the journal names its exact digest: a ``produced_capture`` record
of the Source acquisition the run selected, or a delivered ``emit_capture``
terminal. These tests forge envelopes the way a writer could, cite them through
authoring submit, and check that the genuine Captures still cite at a cost that
does not grow with the journal.
"""

from __future__ import annotations

import itertools
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cruxible_client.contracts.acquisition_policies import (
    CanonicalDuration,
    InputAcquisitionRule,
)
from cruxible_client.contracts.authoring.models import (
    ChangeSetAuthoringPayload,
    ClaimAuthoringPayload,
    ClaimDependencyDrafts,
    ExistingCaptureCitationSource,
    authoring_member_identity,
)
from cruxible_client.contracts.captures import (
    CaptureEnvelope,
    CaptureEnvelopeV1,
    CaptureProductionRecord,
    CaptureRunCoordinateV1,
    build_cas_capture,
    capture_contract_digest,
    capture_digest,
    parse_capture_envelope,
    render_capture_envelope,
    verify_capture,
)
from cruxible_client.contracts.cas_contracts import BodyAccessContext
from cruxible_client.contracts.errors import SettlementEvidenceUnproven
from cruxible_client.contracts.providers import provider_digest
from cruxible_client.contracts.source_references import (
    CasSourceReference,
    EvidenceCommitment,
)
from cruxible_core.authoring.coordinator import AuthoringIntentCoordinator
from cruxible_core.authoring.lowering import lower_authoring
from cruxible_core.authoring.store import AuthoringIntentStore
from cruxible_core.exhaust import LocalJournalBackend, producer_receipts
from cruxible_core.exhaust.records import ProcedureJournalRecordDraftV1
from cruxible_core.procedures.egress import TerminalEgressReceiptV2
from cruxible_core.proposals.proposals import AuthenticatedActor, ProposalAdmissionRequest
from cruxible_core.runtime.instance import PlaybillInstance
from tests.core_support._p2b1_support import accepted_provider
from tests.support.capture_receipts import (
    admit_as_before_production_proof,
    admit_legacy_v1_citations,
)
from tests.test_authoring.p2b4_unit1.test_live_capture_consumers import (
    ProducedCaptures,
    acquisition_demo,  # noqa: F401 - the fixture the producer world needs
    run_capture_producers,
)
from tests.test_authoring.test_authoring_existing_capture import _activate
from tests.test_authoring.test_authoring_preflight import _working_payload

_ACTOR = AuthenticatedActor(actor_id="owner")
_READ = BodyAccessContext(principal_id="unit-test", can_read_body=True)
_minted = itertools.count(1)


def _coordinator(instance: PlaybillInstance) -> AuthoringIntentCoordinator:
    return AuthoringIntentCoordinator(
        instance=instance,
        store=AuthoringIntentStore(instance.root / instance.descriptor.storage.exhaust),
        claim_id_factory=lambda: f"CLM-{next(_minted):032x}",
    )


def _citing(capture: str, *, qualifier: str, revises: str | None = None) -> ClaimAuthoringPayload:
    return ClaimAuthoringPayload(
        statement=_working_payload(occurrence_count=1).statement.model_copy(
            update={"qualifier": qualifier}
        ),
        rationale="Cite one Capture a run is recorded producing.",
        source=ExistingCaptureCitationSource(capture_digest=capture),
        citation_role="evidence",
        dependency_drafts=ClaimDependencyDrafts(),
        revises=revises,
    )


def _preflight_codes(instance: PlaybillInstance, payload: object, *, at: str) -> list[str]:
    coordinator = _coordinator(instance)
    intent = coordinator.create(actor=_ACTOR, payload=payload, canonical_timestamp=at).intent
    result = coordinator.preflight(intent.intent_id, actor=_ACTOR)
    return [item.code for item in result.frontier.diagnostics]


def _envelope(instance: PlaybillInstance, digest: str):  # type: ignore[no-untyped-def]
    return parse_capture_envelope(instance.body_store().read(digest, access=_READ))


def _store_envelope(instance: PlaybillInstance, envelope) -> str:  # type: ignore[no-untyped-def]
    stored = instance.body_store().store(render_capture_envelope(envelope))
    assert stored.digest == capture_digest(envelope).tagged
    return stored.digest


def _over_attacker_bytes(instance: PlaybillInstance, envelope, body: bytes):  # type: ignore[no-untyped-def]
    """The same envelope committing to bytes a writer stored, not what the run made."""

    stored = instance.body_store().store(body)
    commitment = envelope.commitment.model_copy(
        update={"digest": stored.digest, "byte_length": len(body)}
    )
    update: dict[str, object] = {"commitment": commitment}
    if isinstance(envelope.source, CasSourceReference):
        update["source"] = CasSourceReference(content_digest=stored.digest)
    return envelope.model_copy(update=update)


def _genuine(world: ProducedCaptures) -> tuple[str, str]:
    """The Source acquisition's Capture and the emit_capture terminal's Capture."""

    terminal = TerminalEgressReceiptV2.model_validate(world.payload("terminal_egress")["receipt"])
    return str(world.payload("produced_capture")["capture_digest"]), terminal.children[
        0
    ].egress_digest


def _verifies_without_proof(world: ProducedCaptures, digest: str) -> bool:
    """Whether every check but the production record passes: what verified before."""

    envelope = _envelope(world.instance, digest)
    producers = {
        envelope.producer.qualified: envelope.run_coordinate.executable_digest,
    }
    verify_capture(
        digest,
        store=world.instance.body_store(),
        contract=world.contract,
        producer_artifact_digests=producers,
        producer_receipt_resolver=world.instance.capture_verification_context(),
        require_production_proof=False,
    )
    return True


@pytest.fixture
def world(tmp_path: Path, acquisition_demo: None) -> ProducedCaptures:  # noqa: F811
    return run_capture_producers(tmp_path)


def test_forged_envelopes_are_refused_through_authoring_submit(world: ProducedCaptures) -> None:
    instance = world.instance
    contract_digest = capture_contract_digest(world.contract).tagged
    source_capture, terminal_capture = _genuine(world)
    body = instance.body_store().store(b'{"attacker":"chosen"}')

    # (a) A v1 envelope whose executable is the contract's own identity: no
    # producer receipt at all, under a daemon-fetched contract.
    v1 = CaptureEnvelopeV1(
        capture_contract_digest=contract_digest,
        source=CasSourceReference(content_digest=body.digest),
        commitment=EvidenceCommitment(
            digest_kind="exact_bytes",
            digest=body.digest,
            byte_length=len(b'{"attacker":"chosen"}'),
            materialization="cas",
        ),
        run_coordinate=CaptureRunCoordinateV1(
            run_kind="provider",
            run_id="forged-run",
            bound_generation=instance.accepted_coordinate().generation_root,
            executable_identity=world.contract.identity,
            executable_digest=contract_digest,
        ),
        run_receipt_digest=body.digest,
        producer=accepted_provider().provider.identity,
        producer_binding_digest=body.digest,
        observed_at=datetime(2026, 9, 2, 12, 30, tzinfo=UTC),
    )
    # (b) The terminal's real producer receipt over a body the run never emitted.
    procedure_forgery = _over_attacker_bytes(
        instance, _envelope(instance, terminal_capture), b'{"answer":"forged"}'
    )
    # (c) A non-workspace Provider Capture: the real invocation receipt binds the
    # provider output, never the committed material, so only the record can.
    provider_forgery = _over_attacker_bytes(
        instance, _envelope(instance, source_capture), b'{"size":"forged"}'
    )
    forgeries = {
        "v1": (_store_envelope(instance, v1), "cruxible.authoring.capture_v1_citation_refused"),
        "procedure": (
            _store_envelope(instance, procedure_forgery),
            "cruxible.authoring.capture_production_unproven",
        ),
        "provider": (
            _store_envelope(instance, provider_forgery),
            "cruxible.authoring.capture_production_unproven",
        ),
    }
    for index, (name, (digest, code)) in enumerate(forgeries.items()):
        # Every other check passes: before production proof these were evidence.
        assert _verifies_without_proof(world, digest), name
        codes = _preflight_codes(
            instance,
            _citing(digest, qualifier=f"forged-{name}"),
            at=f"2026-09-02T12:1{index}:00.000000Z",
        )
        assert codes[:1] == [code], name

    # The genuine Captures of the same run still cite.
    for index, digest in enumerate((source_capture, terminal_capture)):
        coordinator = _coordinator(instance)
        intent = coordinator.create(
            actor=_ACTOR,
            payload=_citing(digest, qualifier=f"genuine-{index}"),
            canonical_timestamp=f"2026-09-02T12:2{index}:00.000000Z",
        ).intent
        submitted = coordinator.submit(intent.intent_id, actor=_ACTOR)
        assert submitted.status.proposal_id is not None
        _activate(instance, submitted)
        instance.refresh()
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert reopened.accepted_coordinate() == instance.accepted_coordinate()


class _AnyRunProduced:
    """A verification context that vouches for every Capture: lowering's check bypassed."""

    def __init__(self, instance: PlaybillInstance) -> None:
        self.instance = instance
        self.inner = instance.capture_verification_context()

    def producer_receipt(self, digest: str, *, run_id: str):  # type: ignore[no-untyped-def]
        return self.inner.producer_receipt(digest, run_id=run_id)

    def recall_verified(self, capture_digest: str) -> None:
        return None

    def remember_verified(self, memo: object) -> None:
        return None

    def produced_capture(
        self, capture_digest: str, *, run_id: str
    ) -> tuple[CaptureProductionRecord, ...]:
        envelope = _envelope(self.instance, capture_digest)
        return (
            CaptureProductionRecord(
                capture_digest=capture_digest,
                capture_contract_digest=envelope.capture_contract_digest,
                run_id=run_id,
                receipt_digest=None,
            ),
        )


def test_raw_proposal_citing_a_forgery_is_refused_by_the_claim_law(
    world: ProducedCaptures,
) -> None:
    instance = world.instance
    _source, terminal_capture = _genuine(world)
    forged = _store_envelope(
        instance,
        _over_attacker_bytes(instance, _envelope(instance, terminal_capture), b'{"raw":1}'),
    )
    coordinator = _coordinator(instance)
    intent = coordinator.create(
        actor=_ACTOR,
        payload=_citing(forged, qualifier="raw-forgery"),
        canonical_timestamp="2026-09-02T12:40:00.000000Z",
    ).intent
    lowered = lower_authoring(
        instance, intent=intent, actor_id="owner", captures=_AnyRunProduced(instance)
    )
    base = instance.accepted_coordinate()

    result = instance.proposal_service().submit(
        actor=_ACTOR,
        request=ProposalAdmissionRequest(
            target_ref="refs/proposals/owner/raw-forged-capture",
            proposed_base_oid=base.git_oid,
        ),
        candidate_tree=dict(lowered.proposed_tree),
        timestamp="2026-09-02T12:41:00.000000Z",
    )

    assert [item.code for item in result.evaluation.diagnostics] == [
        "cruxible.claim.capture_production_unproven"
    ]


def test_a_source_read_the_rule_did_not_select_is_uncitable(
    tmp_path: Path,
    acquisition_demo: None,  # noqa: F811
) -> None:
    # Only attested-only replayability is admitted, and the read is exact: the
    # optional input is omitted, so the Capture is built and stored but never
    # published as the run's output.
    rule = InputAcquisitionRule(
        input_name="source_result",
        requirement="optional",
        permitted_replayability=("attested_only",),
        max_age=CanonicalDuration(microseconds=60_000_000),
        on_unavailable="omit_optional",
        on_stale="refuse",
        on_oversized="refuse",
        on_conflict="preserve",
    )
    world = run_capture_producers(tmp_path, rule=rule, include_terminal=False)
    acquisition = world.payload("source_acquisition")
    assert acquisition["decision"]["disposition"] == "omitted"
    (considered,) = acquisition["decision"]["considered_capture_digests"]
    assert "produced_capture" not in {stored.record.event_kind for stored in world.records}
    assert world.instance.body_store().verify(considered)
    assert _verifies_without_proof(world, considered)

    coordinator = _coordinator(world.instance)
    intent = coordinator.create(
        actor=_ACTOR,
        payload=_citing(considered, qualifier="not-selected"),
        canonical_timestamp="2026-09-02T12:50:00.000000Z",
    ).intent
    result = coordinator.preflight(intent.intent_id, actor=_ACTOR)

    refusal = result.frontier.diagnostics[0]
    assert refusal.code == "cruxible.authoring.capture_production_unproven"
    assert refusal.offending_element == "source.capture_digest"
    assert [repair.kind for repair in refusal.repairs] == ["replace_capture"]


def _count_run_lookups(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    calls = {"runs": 0, "journal": 0, "payloads": 0}
    original_run = producer_receipts.JournalProducerReceiptResolver._index_run
    original_all = producer_receipts.JournalProducerReceiptResolver.index_all
    original_parse = producer_receipts.parse_journal_payload

    def run(self, run_id):  # type: ignore[no-untyped-def]
        calls["runs"] += 1
        return original_run(self, run_id)

    def full(self):  # type: ignore[no-untyped-def]
        if not self._full:
            calls["journal"] += 1
        return original_all(self)

    def parse(content):  # type: ignore[no-untyped-def]
        calls["payloads"] += 1
        return original_parse(content)

    monkeypatch.setattr(producer_receipts.JournalProducerReceiptResolver, "_index_run", run)
    monkeypatch.setattr(producer_receipts.JournalProducerReceiptResolver, "index_all", full)
    monkeypatch.setattr(producer_receipts, "parse_journal_payload", parse)
    return calls


def test_n_claims_citing_one_capture_read_its_run_once(
    world: ProducedCaptures, monkeypatch: pytest.MonkeyPatch
) -> None:
    _source, terminal_capture = _genuine(world)
    members = [_citing(terminal_capture, qualifier=f"shared-{index}") for index in range(6)]
    payload = ChangeSetAuthoringPayload(
        members=tuple(
            sorted(members, key=lambda member: authoring_member_identity(member).encode("utf-8"))
        ),
        rationale="Six statements backed by one Capture.",
    )
    coordinator = _coordinator(world.instance)
    intent = coordinator.create(
        actor=_ACTOR, payload=payload, canonical_timestamp="2026-09-02T13:00:00.000000Z"
    ).intent
    calls = _count_run_lookups(monkeypatch)

    result = coordinator.preflight(intent.intent_id, actor=_ACTOR)

    assert result.verdict == "passed", result.frontier.diagnostics
    # Lowering and every member's Claim law share one context: one run lookup.
    assert calls["runs"] == 1
    assert calls["journal"] == 0


def _pad_journal(world: ProducedCaptures, count: int, *, start: int) -> None:
    """Append ``count`` proof-kind records of other runs to the same partition."""

    journal_root = world.instance.root / world.instance.descriptor.storage.exhaust
    journal = LocalJournalBackend(journal_root / "procedure-runs")
    template = next(
        stored.record for stored in world.records if stored.record.event_kind == "produced_capture"
    )
    fields = set(ProcedureJournalRecordDraftV1.model_fields) - {"tag"}
    for index in range(start, start + count):
        draft = ProcedureJournalRecordDraftV1.model_validate(
            {
                **template.model_dump(mode="python", include=fields),
                "run_id": f"pad-run-{index}",
            }
        )
        journal.append(
            draft,
            expected_head=journal.read_head(template.stream, template.partition_id),
            fencing_token="writer",
        )


def test_a_capture_citation_reads_only_its_run_as_the_journal_grows(
    world: ProducedCaptures, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = world.instance
    source_capture, _terminal = _genuine(world)
    run_payloads = sum(
        stored.record.event_kind in producer_receipts._PROOF_EVENT_KINDS for stored in world.records
    )
    reads: list[int] = []
    for padded, start in ((50, 0), (450, 50)):
        _pad_journal(world, padded, start=start)
        calls = _count_run_lookups(monkeypatch)
        assert _verifies_with_proof(world, source_capture)
        reads.append(calls["payloads"])
        assert (calls["runs"], calls["journal"]) == (1, 0)
        monkeypatch.undo()
    # 50 prior runs or 500: the lookup reads exactly the cited run's records.
    assert reads == [run_payloads, run_payloads]
    assert instance.accepted_coordinate() is not None


def _verifies_with_proof(world: ProducedCaptures, digest: str) -> bool:
    envelope = _envelope(world.instance, digest)
    verified = verify_capture(
        digest,
        store=world.instance.body_store(),
        contract=world.contract,
        producer_artifact_digests={
            envelope.producer.qualified: envelope.run_coordinate.executable_digest
        },
        producer_receipt_resolver=world.instance.capture_verification_context(),
    )
    return isinstance(verified, CaptureEnvelope)


def test_an_index_missing_the_run_falls_back_to_the_journal(
    world: ProducedCaptures, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_capture, terminal_capture = _genuine(world)
    index = (
        world.instance.root
        / world.instance.descriptor.storage.exhaust
        / "procedure-runs"
        / "event-index.sqlite3"
    )
    # The SQL index is a rebuildable locator: drop the run's rows, as if another
    # process's append had not reached it yet.
    with sqlite3.connect(index) as connection:
        connection.execute("DELETE FROM records WHERE run_id=?", (world.run_id,))
    calls = _count_run_lookups(monkeypatch)

    assert _verifies_with_proof(world, source_capture)
    assert _verifies_with_proof(world, terminal_capture)
    assert calls["runs"] == 2  # one per context, each finding nothing
    assert calls["journal"] == 2  # each fell back to one pass over the journal


def test_an_accepted_v1_citation_stays_valid_and_no_new_one_is_admitted(
    world: ProducedCaptures, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = world.instance
    provider = accepted_provider().provider
    # A version-1 Provider Capture, as Sources produced before receipts.
    legacy = build_cas_capture(
        store=instance.body_store(),
        contract=world.contract,
        source_body=b'{"status":"released"}',
        run_coordinate=CaptureRunCoordinateV1(
            run_kind="provider",
            run_id="legacy-provider-run",
            bound_generation=instance.accepted_coordinate().generation_root,
            executable_identity=provider.identity,
            executable_digest=provider_digest(provider).tagged,
        ),
        run_receipt_digest=capture_contract_digest(world.contract).tagged,
        producer=provider.identity,
        producer_binding_digest=capture_contract_digest(world.contract).tagged,
        observed_at=datetime(2026, 9, 2, 11, tzinfo=UTC),
    ).capture_digest

    # Accepted before this law existed: a writer then held no proof to give.
    with monkeypatch.context() as before:
        admit_legacy_v1_citations(before)
        coordinator = _coordinator(instance)
        accepted = coordinator.create(
            actor=_ACTOR,
            payload=_citing(legacy, qualifier="legacy"),
            canonical_timestamp="2026-09-02T13:10:00.000000Z",
        ).intent
        _activate(instance, coordinator.submit(accepted.intent_id, actor=_ACTOR))
        pending = coordinator.create(
            actor=_ACTOR,
            payload=_citing(legacy, qualifier="legacy-pending"),
            canonical_timestamp="2026-09-02T13:11:00.000000Z",
        ).intent
        submitted_before = coordinator.submit(pending.intent_id, actor=_ACTOR)
        assert submitted_before.status.proposal_id is not None

    # Recovery replays the acceptance under its recorded law: the state opens.
    instance.refresh()
    reopened = PlaybillInstance.open(instance.root, trust_root=instance.trust_root)
    assert reopened.accepted_coordinate() == instance.accepted_coordinate()
    # A proposal admitted before is not an accepted citation: settlement holds
    # what it cites for the first time to the law in force, and names the repair.
    with pytest.raises(SettlementEvidenceUnproven) as refused:
        _activate(instance, submitted_before)
    assert refused.value.error_code == "cruxible.proposal.capture_production_unproven"
    assert "readmit" in str(refused.value) and "resubmit" in str(refused.value)
    instance.refresh()
    # A new citation of the same envelope is refused.
    assert _preflight_codes(
        instance,
        _citing(legacy, qualifier="legacy-new"),
        at="2026-09-02T13:12:00.000000Z",
    )[:1] == ["cruxible.authoring.capture_v1_citation_refused"]
    # Revising the accepted Claim keeps the citation it inherits.
    revision = _citing(legacy, qualifier="legacy", revises=accepted.semantic_identity)
    revision = revision.model_copy(
        update={
            "statement": revision.statement.model_copy(
                update={"effective_from": datetime(2026, 9, 1, tzinfo=UTC)}
            )
        }
    )
    coordinator = _coordinator(instance)
    revised = coordinator.create(
        actor=_ACTOR, payload=revision, canonical_timestamp="2026-09-02T13:13:00.000000Z"
    ).intent
    submitted = coordinator.submit(revised.intent_id, actor=_ACTOR)
    assert submitted.status.proposal_id is not None, submitted.intent.last_preflight
    _activate(instance, submitted)
    instance.refresh()
    assert coordinator.status(revised.intent_id, actor=_ACTOR).state == "accepted"


def test_a_pending_forged_citation_admitted_before_the_fix_is_refused_at_settlement(
    world: ProducedCaptures, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cruxible_client.contracts.accepted_attestations import (
        attestation_path,
        render_accepted_attestation,
    )
    from cruxible_client.contracts.claim_attestations import (
        claim_attestation_v2_envelope_digest,
    )
    from cruxible_core.proposals.settlement import ChangeActorBinding
    from tests.core_support._support import client_material
    from tests.test_claims.test_claim_attestation_service import _request
    from tests.test_ledger.test_activation import _sign

    instance = world.instance
    source_capture, terminal_capture = _genuine(world)
    # A genuine accepted Claim for the attestation to attest.
    coordinator = _coordinator(instance)
    genuine = coordinator.create(
        actor=_ACTOR,
        payload=_citing(source_capture, qualifier="attested"),
        canonical_timestamp="2026-09-02T14:00:00.000000Z",
    ).intent
    _activate(instance, coordinator.submit(genuine.intent_id, actor=_ACTOR))
    instance.refresh()
    forged = _store_envelope(
        instance,
        _over_attacker_bytes(instance, _envelope(instance, terminal_capture), b'{"pending":1}'),
    )

    with monkeypatch.context() as before:
        admit_as_before_production_proof(before)
        # A Claim citing the forgery, admitted by the vulnerable build.
        claim_intent = coordinator.create(
            actor=_ACTOR,
            payload=_citing(forged, qualifier="pending-forgery"),
            canonical_timestamp="2026-09-02T14:01:00.000000Z",
        ).intent
        pending_claim = coordinator.submit(claim_intent.intent_id, actor=_ACTOR)
        assert pending_claim.status.proposal_id is not None
        # A new-capture attestation citing the forgery, admitted the same way.
        attestation = _request(
            instance,
            world.owner,
            genuine.semantic_identity,
            tmp_path,
            basis="new_capture",
            captures=(forged,),
            attested_at=datetime(2026, 9, 2, 14, 2, tzinfo=UTC),
        ).attestation
        base = instance.accepted_coordinate()
        tree = instance.tree_at(base.git_oid)
        tree[attestation_path(claim_attestation_v2_envelope_digest(attestation))] = (
            render_accepted_attestation(attestation)
        )
        pending_attestation = instance.proposal_service().submit(
            actor=_ACTOR,
            request=ProposalAdmissionRequest(
                target_ref="refs/proposals/owner/pending-forged-attestation",
                proposed_base_oid=base.git_oid,
            ),
            candidate_tree=tree,
            timestamp="2026-09-02T14:03:00.000000Z",
        )
        assert pending_attestation.candidate is not None, pending_attestation.evaluation

    # After the upgrade neither settles.
    with pytest.raises(SettlementEvidenceUnproven):
        _activate(instance, pending_claim)
    assert pending_attestation.evaluation.evaluated_tree_oid is not None
    with pytest.raises(SettlementEvidenceUnproven):
        instance.prepare_generation(
            base=base,
            candidate_tree=instance.proposal_tree(
                pending_attestation.evaluation.evaluated_tree_oid
            ),
            candidate=pending_attestation.candidate,
            approvals=(
                _sign(
                    client_material(instance.root.parent, instance),
                    pending_attestation.candidate.candidate_digest,
                    base.semantic_root,
                ),
            ),
            actor_binding=ChangeActorBinding(actor_id="owner"),
            proposal_actor_id="owner",
            sequence=len(instance.accepted_history()),
        )
    assert instance.accepted_coordinate() == base  # nothing was accepted


def test_one_submit_verifies_a_shared_capture_once(
    world: ProducedCaptures, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cruxible_client.contracts import captures as capture_contracts
    from cruxible_core.storage.cas import ContentAddressedBodyStore

    _source, terminal_capture = _genuine(world)
    material = _envelope(world.instance, terminal_capture).commitment.digest
    members = [_citing(terminal_capture, qualifier=f"one-submit-{index}") for index in range(6)]
    payload = ChangeSetAuthoringPayload(
        members=tuple(
            sorted(members, key=lambda member: authoring_member_identity(member).encode("utf-8"))
        ),
        rationale="Six statements backed by one Capture, in one submit.",
    )
    coordinator = _coordinator(world.instance)
    intent = coordinator.create(
        actor=_ACTOR, payload=payload, canonical_timestamp="2026-09-02T15:00:00.000000Z"
    ).intent

    counts = {"contexts": 0, "runs": 0, "verifications": 0, "envelope": 0, "material": 0}
    opened = producer_receipts.LocalProducerReceiptResolver.__init__
    indexed = producer_receipts.JournalProducerReceiptResolver._index_run
    verify_once = capture_contracts._verify_capture_once
    read = ContentAddressedBodyStore.read

    def count_context(self, **kwargs):  # type: ignore[no-untyped-def]
        counts["contexts"] += 1
        opened(self, **kwargs)

    def count_run(self, run_id):  # type: ignore[no-untyped-def]
        counts["runs"] += 1
        return indexed(self, run_id)

    def count_verification(*args, **kwargs):  # type: ignore[no-untyped-def]
        counts["verifications"] += 1
        return verify_once(*args, **kwargs)

    def count_read(self, digest, *, access):  # type: ignore[no-untyped-def]
        if digest == terminal_capture:
            counts["envelope"] += 1
        elif digest == material:
            counts["material"] += 1
        return read(self, digest, access=access)

    monkeypatch.setattr(producer_receipts.LocalProducerReceiptResolver, "__init__", count_context)
    monkeypatch.setattr(producer_receipts.JournalProducerReceiptResolver, "_index_run", count_run)
    monkeypatch.setattr(capture_contracts, "_verify_capture_once", count_verification)
    monkeypatch.setattr(ContentAddressedBodyStore, "read", count_read)

    submitted = coordinator.submit(intent.intent_id, actor=_ACTOR)

    assert submitted.status.proposal_id is not None, submitted.intent.last_preflight
    # Lowering, the preflight laws and admission share the submit's one context:
    # one run lookup and one verification for six Claims. The Capture is read
    # once to lower its first citation and once to verify it; its material once
    # to verify it and once to derive its cited source window.
    assert counts == {
        "contexts": 1,
        "runs": 1,
        "verifications": 1,
        "envelope": 2,
        "material": 2,
    }


def test_a_capture_provable_only_by_its_delivered_terminal_is_found_by_contract(
    world: ProducedCaptures,
) -> None:
    from cruxible_core.service.evidence.capture_reads import (
        contract_captures,
        verify_accepted_capture,
    )

    instance = world.instance
    source_capture, terminal_capture = _genuine(world)
    # A crash after the delivered terminal and before its produced_capture
    # record: cut the journal right after the terminal and rebuild its index.
    journal_root = instance.root / instance.descriptor.storage.exhaust / "procedure-runs"
    journal = LocalJournalBackend(journal_root)
    terminal = next(
        stored.record for stored in world.records if stored.record.event_kind == "terminal_egress"
    )
    directory = journal._partition_directory(terminal.stream, terminal.partition_id, create=False)
    end = next(
        offset + size
        for offset, size, stored in LocalJournalBackend._read_frames(
            directory,
            stream=terminal.stream,
            partition_id=terminal.partition_id,
            recover_tail=False,
        )
        if stored.record.event_kind == "terminal_egress"
    )
    with (directory / "records.log").open("r+b") as log:
        log.truncate(end)
    (journal_root / "event-index.sqlite3").unlink()
    survivors = journal.all_records(terminal.stream, terminal.partition_id)
    assert [stored.record.event_kind for stored in survivors].count("produced_capture") == 1
    assert survivors[-1].record.event_kind == "terminal_egress"

    inventory = contract_captures(
        instance,
        contract_digests=(capture_contract_digest(world.contract).tagged,),
        budget=16,
    )
    assert inventory.complete
    assert {terminal_capture, source_capture} <= {item.digest for item in inventory.captures}
    verified = verify_accepted_capture(
        instance, instance.accepted_coordinate(), terminal_capture, access=_READ
    )
    assert not isinstance(verified, str)
