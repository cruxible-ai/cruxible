"""Line deployment, occurrence derivation, attempt, lease, and handoff laws."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.artifacts import (
    ArtifactAuthority,
    ArtifactIdentity,
    ArtifactLifecycle,
    ArtifactPin,
)
from cruxible_core.playbill.canonical import (
    ArtifactDigest,
    GenerationRoot,
    SemanticRoot,
    Sha256Value,
    typed_digest,
)
from cruxible_core.playbill.capture_journal import (
    CaptureCursorV1,
    CaptureLandingEventV1,
    capture_landing_event_id,
)
from cruxible_core.playbill.captures import CanonicalDurationV1, CaptureRunCoordinateV1
from cruxible_core.playbill.cas import ContentAddressedBodyStore
from cruxible_core.playbill.exhaust import (
    PROCEDURE_EXHAUST_JOURNAL_FAMILY,
    JournalStreamIdentityV1,
    LocalJournalBackend,
)
from cruxible_core.playbill.line_scheduler import (
    IndexedLineOccurrenceV1,
    LineDispatchRulesV1,
    LineOccurrenceIndex,
    LineScheduler,
    ResolvedCadenceTriggerV1,
    ResolvedCaptureLandingTriggerV1,
    ResolvedManualTriggerV1,
    ResolvedTriggerV1,
    ResolvedWindowCloseTriggerV1,
    bind_resolved_trigger,
    cadence_occurrence_time,
    derive_cadence_occurrences,
    derive_landing_occurrences,
    derive_manual_occurrences,
    derive_window_close_occurrences,
)
from cruxible_core.playbill.lines import (
    LineDeploymentV1,
    LineJournalBindingV1,
    LineLeaseV1,
    LineRunnerIdentityV1,
    LineRuntimeRefusal,
    acquire_line_lease,
    bind_line_deployment,
    line_deployment_digest,
    rebind_line_deployment_backend,
    revise_line_deployment,
    take_over_line_lease,
    verify_line_lease,
)
from cruxible_core.playbill.occurrences import (
    CadenceOccurrenceV1,
    LineOccurrenceV1,
    ManualOccurrenceV1,
    WindowCloseOccurrenceV1,
    capture_landing_occurrence,
    line_occurrence_digest,
    occurrence_digest,
)
from cruxible_core.playbill.procedures.artifacts import (
    AcceptedProcedureV1,
    ProcedureArtifactV1,
    procedure_artifact_digest,
    procedure_path,
)
from cruxible_core.playbill.procedures.closure import LineSlotBindingV1
from cruxible_core.playbill.procedures.graph import compute_procedure_definition_digest_v3
from cruxible_core.playbill.procedures.line_specs import (
    AcceptedLineSpecV1,
    CadenceTriggerPolicyV1,
    CaptureLandingTriggerPolicyV1,
    LineSpecV1,
    ManualTriggerPolicyV1,
    TriggerPolicyV1,
    WindowCloseTriggerPolicyV1,
    line_spec_digest,
    line_spec_path,
)
from cruxible_core.playbill.procedures.models import (
    ProcedureBudgetV3,
    ProcedureDefinitionV3,
    ProcedureHardCapsV3,
    ProcedurePinSlotRefV1,
    ProcedurePinSlotV1,
    ProjectNodeV3,
    StateTapNodeV3,
)
from cruxible_core.playbill.projection import AcceptedCoordinate

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
ANCHOR = datetime(2026, 8, 17, 0, 0, tzinfo=timezone.utc)
HOUR = CanonicalDurationV1(microseconds=3_600_000_000)
LINE_ID = "triage-hourly"


def _digest(label: str) -> str:
    return typed_digest(ArtifactDigest, "playbill-line-scheduler-test-v1", {"label": label}).tagged


def _raw(label: str) -> str:
    return typed_digest(Sha256Value, "playbill-line-scheduler-raw-v1", {"label": label}).value


def _pin(role: str, kind: str, name: str, *, digest: str | None = None) -> ArtifactPin:
    return ArtifactPin(
        role=role,
        target=ArtifactIdentity(kind=kind, name=name),
        artifact_digest=digest or _digest(name),
    )


def _coordinate() -> AcceptedCoordinate:
    return AcceptedCoordinate(
        git_oid="a" * 40,
        semantic_root=typed_digest(
            SemanticRoot, "playbill-line-scheduler-semantic-v1", {"value": "accepted"}
        ).tagged,
        generation_root=typed_digest(
            GenerationRoot, "playbill-line-scheduler-generation-v1", {"value": "accepted"}
        ).tagged,
        compiler_digest=_digest("compiler"),
    )


def _actor() -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="service_account",
        actor_id="line-runner",
        org_id="instance-a",
        operation_id="operation-a",
        timestamp=NOW,
    )


def _dispatch(
    *,
    overlap: str = "serial",
    max_attempts: int = 3,
    backoff_seconds: int = 60,
) -> LineDispatchRulesV1:
    return LineDispatchRulesV1(
        overlap=overlap,  # type: ignore[arg-type]
        max_attempts=max_attempts,
        backoff=CanonicalDurationV1(microseconds=backoff_seconds * 1_000_000),
        backoff_multiplier=2,
        max_backoff=CanonicalDurationV1(microseconds=3_600_000_000),
    )


def _accepted_procedure() -> tuple[AcceptedProcedureV1, ArtifactPin, Mapping[str, str]]:
    interface_digest = _digest("query-interface")
    query_pin = _pin("query", "QueryDefinition", "claims-by-status")
    contract_in = _pin("contract-in", "Contract", "empty-input")
    contract_out = _pin("contract-out", "Contract", "claim-rows")
    definition = ProcedureDefinitionV3(
        name="triage",
        contract_in=contract_in,
        contract_out=contract_out,
        nodes=(
            StateTapNodeV3(
                node_id="read",
                query=ProcedurePinSlotRefV1(slot_name="query"),
                parameters={},
                as_="rows",
            ),
            ProjectNodeV3(
                node_id="shape",
                fields={"rows": "$steps.rows"},
                contract_out=contract_out,
                as_="result",
            ),
        ),
        returns="result",
        pin_slots=(
            ProcedurePinSlotV1(
                slot_name="query",
                pin_role="query",
                artifact_kind="QueryDefinition",
                interface_digest=interface_digest,
            ),
        ),
        budget=ProcedureBudgetV3(
            wall_clock=CanonicalDurationV1(microseconds=1_000_000),
            max_provider_calls=0,
            max_capture_bytes=0,
            max_items=100,
        ),
        hard_caps=ProcedureHardCapsV3(
            max_wall_clock=CanonicalDurationV1(microseconds=2_000_000),
            max_provider_calls=0,
            max_capture_bytes=0,
            max_items=200,
            max_repeat_attempts=1,
        ),
        terminal_capability=2,
    )
    procedure = ProcedureArtifactV1(
        identity=ArtifactIdentity(kind="Procedure", name="triage"),
        definition=definition,
        definition_digest=compute_procedure_definition_digest_v3(definition).tagged,
        authority=ArtifactAuthority(
            propose_roles=("procedure-author",),
            approve_roles=("procedure-reviewer",),
        ),
        pins=tuple(
            sorted(
                (contract_in, contract_out),
                key=lambda pin: (pin.role, pin.target.qualified, pin.artifact_digest),
            )
        ),
        activation_policy="drain",
    )
    accepted = AcceptedProcedureV1(
        path=procedure_path("triage"),
        procedure=procedure,
        artifact_digest=procedure_artifact_digest(procedure).tagged,
    )
    return accepted, query_pin, {query_pin.artifact_digest: interface_digest}


def _accepted_line(
    *,
    trigger: TriggerPolicyV1 | None = None,
    epoch: int = 1,
    parameters: object | None = None,
    predecessor_digest: str | None = None,
) -> tuple[AcceptedLineSpecV1, AcceptedProcedureV1]:
    accepted_procedure, query_pin, _interfaces = _accepted_procedure()
    procedure_pin = _pin(
        "procedure", "Procedure", "triage", digest=accepted_procedure.artifact_digest
    )
    bindings = (LineSlotBindingV1(slot_name="query", artifact_pin=query_pin),)
    trigger = trigger or ManualTriggerPolicyV1()
    pins = [procedure_pin, query_pin]
    if isinstance(trigger, CadenceTriggerPolicyV1):
        pins.append(
            _pin("trigger-cadence-policy", "Policy", "hourly", digest=trigger.cadence_policy_digest)
        )
    elif isinstance(trigger, CaptureLandingTriggerPolicyV1):
        pins.extend(
            (
                _pin(
                    "trigger-capture-contract",
                    "CaptureContract",
                    "anchor-capture",
                    digest=trigger.anchor_capture_contract_digest,
                ),
                _pin(
                    "trigger-landing-filter",
                    "LandingFilter",
                    "landing-filter",
                    digest=trigger.landing_filter_digest,
                ),
            )
        )
    elif isinstance(trigger, WindowCloseTriggerPolicyV1):
        pins.append(
            _pin(
                "trigger-window-policy",
                "Policy",
                "window-policy",
                digest=trigger.window_policy_digest,
            )
        )
    line = LineSpecV1(
        identity=ArtifactIdentity(kind="Line", name=LINE_ID),
        occurrence_epoch=epoch,
        procedure=procedure_pin,
        parameters=parameters if parameters is not None else {"status": "open"},
        slot_bindings=bindings,
        trigger_policy=trigger,
        requested_terminal_rung=2,
        budgets={
            "max_capture_bytes": 0,
            "max_items": 100,
            "max_provider_calls": 0,
            "max_wall_clock_microseconds": 1_000_000,
        },
        epsilon={"$decimal": "0.1"},
        authority=ArtifactAuthority(
            propose_roles=("line-author",),
            approve_roles=("line-reviewer",),
        ),
        pins=tuple(
            sorted(pins, key=lambda pin: (pin.role, pin.target.qualified, pin.artifact_digest))
        ),
        lifecycle=ArtifactLifecycle(predecessor_digest=predecessor_digest),
    )
    accepted_line = AcceptedLineSpecV1(
        path=line_spec_path(LINE_ID),
        line=line,
        artifact_digest=line_spec_digest(line).tagged,
    )
    return accepted_line, accepted_procedure


def _stream() -> JournalStreamIdentityV1:
    return JournalStreamIdentityV1(
        instance_id="instance-a",
        journal_family=PROCEDURE_EXHAUST_JOURNAL_FAMILY,
        stream_id="lines",
    )


def _binding(backend_id: str = "local-a") -> LineJournalBindingV1:
    return LineJournalBindingV1(
        logical_stream=_stream(),
        control_partition_id="line-control",
        run_partition_id="line-runs",
        backend_id=backend_id,
    )


def _backend(tmp_path: Path, name: str) -> LocalJournalBackend:
    root = tmp_path / name
    root.mkdir(mode=0o700)
    return LocalJournalBackend(root)


@dataclass(frozen=True)
class _HeadSigner:
    private_key: Ed25519PrivateKey
    signer_id: str = "line-home"
    signing_key_id: str = "head-key-1"

    def sign_journal_head(self, message: bytes) -> str:
        return self.private_key.sign(message).hex()


@dataclass
class _Harness:
    journal: LocalJournalBackend
    bodies: ContentAddressedBodyStore
    index: LineOccurrenceIndex
    deployment: LineDeploymentV1
    lease: LineLeaseV1
    scheduler: LineScheduler
    accepted_line: AcceptedLineSpecV1
    accepted_procedure: AcceptedProcedureV1

    def materialize(
        self,
        occurrence: LineOccurrenceV1,
        *,
        at: datetime = NOW,
    ) -> IndexedLineOccurrenceV1:
        return self.scheduler.materialize(
            occurrence,
            accepted_line=self.accepted_line,
            accepted_procedure=self.accepted_procedure,
            actor_context=_actor(),
            materialized_at=at,
        )


def _harness(
    tmp_path: Path,
    *,
    trigger: TriggerPolicyV1 | None = None,
    resolved: ResolvedTriggerV1 | None = None,
    fencing_token: str = "runner-a",
    backend_name: str = "journal-a",
    index_name: str = "line-index.sqlite3",
) -> _Harness:
    accepted_line, accepted_procedure = _accepted_line(trigger=trigger)
    journal = _backend(tmp_path, backend_name)
    cas_root = tmp_path / f"cas-{backend_name}"
    cas_root.mkdir(mode=0o700)
    bodies = ContentAddressedBodyStore(cas_root)
    index = LineOccurrenceIndex(tmp_path / index_name)
    deployment = bind_line_deployment(
        accepted_line,
        deployment_id="deployment-a",
        runner=LineRunnerIdentityV1(runner_id="runner-a"),
        journal_binding=_binding(),
        activated_at=NOW,
    )
    lease = acquire_line_lease(
        journal,
        deployment,
        fencing_token=fencing_token,
        acquired_at=NOW,
    )
    scheduler = LineScheduler(
        journal=journal,
        bodies=bodies,
        index=index,
        deployment=deployment,
        lease=lease,
        trigger=resolved or ResolvedManualTriggerV1(dispatch=_dispatch()),
        accepted_coordinate=_coordinate(),
    )
    return _Harness(
        journal=journal,
        bodies=bodies,
        index=index,
        deployment=deployment,
        lease=lease,
        scheduler=scheduler,
        accepted_line=accepted_line,
        accepted_procedure=accepted_procedure,
    )


def _landing_event(
    *,
    partition: str,
    sequence: int,
    contract_digest: str,
    producer_digest: str,
    previous_event_digest: str | None = None,
) -> CaptureLandingEventV1:
    provisional = CaptureLandingEventV1(
        instance_id="instance-a",
        partition_id=_raw(partition),
        sequence=sequence,
        event_id="0" * 64,
        idempotency_key=_raw(f"{partition}:{sequence}:idempotency"),
        capture_digest=_digest(f"{partition}:{sequence}:capture"),
        capture_contract_digest=contract_digest,
        run_coordinate=CaptureRunCoordinateV1(
            run_kind="procedure",
            run_id="run-a",
            bound_generation=_digest("generation"),
            executable_identity=ArtifactIdentity(kind="Procedure", name="triage"),
            executable_digest=_digest("executable"),
        ),
        run_receipt_digest=_digest(f"{partition}:{sequence}:receipt"),
        producer_binding_digest=producer_digest,
        previous_event_digest=previous_event_digest,
        landed_at=NOW,
    )
    return provisional.model_copy(
        update={"event_id": capture_landing_event_id(provisional)},
    )


def _cursor(partition: str, sequence: int, event: str) -> CaptureCursorV1:
    return CaptureCursorV1(
        partition_id=_raw(partition),
        sequence=sequence,
        event_id=_raw(event),
    )


def _sorted_cursors(*cursors: CaptureCursorV1) -> tuple[CaptureCursorV1, ...]:
    return tuple(sorted(cursors, key=lambda cursor: cursor.partition_id))


# --------------------------------------------------------------------------
# Occurrence identity
# --------------------------------------------------------------------------


def test_occurrence_identity_goldens_cover_all_four_trigger_kinds() -> None:
    cadence = CadenceOccurrenceV1(
        line_id=LINE_ID,
        occurrence_epoch=1,
        cadence_policy_digest=_digest("hourly"),
        tick_index=12,
    )
    landing = capture_landing_occurrence(
        line_id=LINE_ID,
        occurrence_epoch=1,
        anchor=_landing_event(
            partition="alpha",
            sequence=0,
            contract_digest=_digest("anchor-capture"),
            producer_digest=_digest("producer-one"),
        ),
    )
    window = WindowCloseOccurrenceV1(
        line_id=LINE_ID,
        occurrence_epoch=1,
        window_policy_digest=_digest("window-policy"),
        from_cursors=(),
        to_cursors=_sorted_cursors(_cursor("alpha", 2, "alpha-2")),
    )
    manual = ManualOccurrenceV1(
        line_id=LINE_ID,
        occurrence_epoch=1,
        request_id="operator-request-1",
    )

    assert line_occurrence_digest(cadence) == (
        "sha256:648f120d1f0381f748a7d1373caee2a1ba8adb735aec59573b2e8f7d2b29a891"
    )
    assert line_occurrence_digest(landing) == (
        "sha256:077a67cb13d16e59334b89e88f70ef580392bf1c6e973c90a2be350f85bef001"
    )
    assert line_occurrence_digest(window) == (
        "sha256:c5f434cc9381805a057c4c662f08a05b45ecc4531e947e02488b34ae6a367c13"
    )
    assert line_occurrence_digest(manual) == (
        "sha256:a7c7655531c56fad59c615b04778b30a0ee7434628bb6e94475ec5b54fadcf2e"
    )
    # The PC-C landing preimage stays frozen: the general helper must reproduce it.
    assert line_occurrence_digest(landing) == occurrence_digest(landing)


def test_occurrence_identity_ignores_attempts_and_epoch_separates_history() -> None:
    first = CadenceOccurrenceV1(
        line_id=LINE_ID,
        occurrence_epoch=1,
        cadence_policy_digest=_digest("hourly"),
        tick_index=3,
    )
    second_epoch = first.model_copy(update={"occurrence_epoch": 2})
    assert line_occurrence_digest(first) != line_occurrence_digest(second_epoch)
    assert line_occurrence_digest(first) == line_occurrence_digest(first.model_copy())


# --------------------------------------------------------------------------
# Pure derivation
# --------------------------------------------------------------------------


def _cadence_trigger(*, max_backfill: int = 2) -> ResolvedCadenceTriggerV1:
    return ResolvedCadenceTriggerV1(
        cadence_policy_digest=_digest("hourly"),
        anchor=ANCHOR,
        interval=HOUR,
        max_backfill_occurrences=max_backfill,
        dispatch=_dispatch(),
    )


def test_cadence_derivation_is_deterministic_and_backfill_is_bounded() -> None:
    trigger = _cadence_trigger(max_backfill=2)
    evaluation = ANCHOR + timedelta(hours=10, minutes=30)

    cold_start = derive_cadence_occurrences(
        line_id=LINE_ID,
        occurrence_epoch=1,
        trigger=trigger,
        evaluation_time=evaluation,
    )
    assert tuple(item.tick_index for item in cold_start) == (8, 9, 10)

    caught_up = derive_cadence_occurrences(
        line_id=LINE_ID,
        occurrence_epoch=1,
        trigger=trigger,
        evaluation_time=evaluation,
        last_tick_index=9,
    )
    assert tuple(item.tick_index for item in caught_up) == (10,)
    assert (
        derive_cadence_occurrences(
            line_id=LINE_ID,
            occurrence_epoch=1,
            trigger=trigger,
            evaluation_time=evaluation,
            last_tick_index=10,
        )
        == ()
    )
    assert (
        derive_cadence_occurrences(
            line_id=LINE_ID,
            occurrence_epoch=1,
            trigger=trigger,
            evaluation_time=ANCHOR - timedelta(seconds=1),
        )
        == ()
    )

    # Two independent schedulers at the same head derive identical identities.
    other = derive_cadence_occurrences(
        line_id=LINE_ID,
        occurrence_epoch=1,
        trigger=_cadence_trigger(max_backfill=2),
        evaluation_time=evaluation,
    )
    assert tuple(map(line_occurrence_digest, other)) == tuple(
        map(line_occurrence_digest, cold_start)
    )
    assert cadence_occurrence_time(trigger, 10) == ANCHOR + timedelta(hours=10)


def test_landing_derivation_is_deterministic_across_partitions_and_duplicates() -> None:
    anchor_contract = _digest("anchor-capture")
    trigger = ResolvedCaptureLandingTriggerV1(
        anchor_capture_contract_digest=anchor_contract,
        landing_filter_digest=_digest("landing-filter"),
        dispatch=_dispatch(),
    )
    events = tuple(
        _landing_event(
            partition=partition,
            sequence=sequence,
            contract_digest=anchor_contract,
            producer_digest=_digest("producer-one"),
        )
        for partition, sequence in (("beta", 1), ("alpha", 0), ("beta", 0), ("gamma", 0))
    )
    other_contract = _landing_event(
        partition="alpha",
        sequence=1,
        contract_digest=_digest("other-capture"),
        producer_digest=_digest("producer-one"),
    )

    derived = derive_landing_occurrences(
        line_id=LINE_ID,
        occurrence_epoch=1,
        trigger=trigger,
        events=(*events, other_contract),
    )
    shuffled = derive_landing_occurrences(
        line_id=LINE_ID,
        occurrence_epoch=1,
        trigger=trigger,
        events=(other_contract, events[3], events[0], events[2], events[1]),
    )
    assert tuple(map(line_occurrence_digest, derived)) == tuple(
        map(line_occurrence_digest, shuffled)
    )
    assert tuple((item.partition_id, item.sequence) for item in derived) == tuple(
        sorted((item.partition_id, item.sequence) for item in derived)
    )
    assert all(item.trigger_kind == "capture_landing" for item in derived)
    assert len(derived) == 4

    # A duplicate landing of the exact same event is one occurrence, not two.
    duplicated = derive_landing_occurrences(
        line_id=LINE_ID,
        occurrence_epoch=1,
        trigger=trigger,
        events=(events[0], events[0]),
    )
    assert len(duplicated) == 1


def test_landing_derivation_honors_the_producer_allowlist() -> None:
    anchor_contract = _digest("anchor-capture")
    trigger = ResolvedCaptureLandingTriggerV1(
        anchor_capture_contract_digest=anchor_contract,
        landing_filter_digest=_digest("landing-filter"),
        producer_binding_digests=(_digest("producer-one"),),
        dispatch=_dispatch(),
    )
    admitted = _landing_event(
        partition="alpha",
        sequence=0,
        contract_digest=anchor_contract,
        producer_digest=_digest("producer-one"),
    )
    excluded = _landing_event(
        partition="alpha",
        sequence=1,
        contract_digest=anchor_contract,
        producer_digest=_digest("producer-two"),
    )
    derived = derive_landing_occurrences(
        line_id=LINE_ID,
        occurrence_epoch=1,
        trigger=trigger,
        events=(admitted, excluded),
    )
    assert tuple(item.sequence for item in derived) == (0,)


def test_window_close_identity_comes_from_cursor_vectors_not_close_time() -> None:
    trigger = ResolvedWindowCloseTriggerV1(
        window_policy_digest=_digest("window-policy"),
        min_new_events=2,
        dispatch=_dispatch(),
    )
    from_cursors = _sorted_cursors(_cursor("alpha", 0, "alpha-0"))
    to_cursors = _sorted_cursors(
        _cursor("alpha", 2, "alpha-2"),
        _cursor("beta", 0, "beta-0"),
    )

    closed = derive_window_close_occurrences(
        line_id=LINE_ID,
        occurrence_epoch=1,
        trigger=trigger,
        from_cursors=from_cursors,
        to_cursors=to_cursors,
    )
    assert len(closed) == 1
    replayed = derive_window_close_occurrences(
        line_id=LINE_ID,
        occurrence_epoch=1,
        trigger=trigger,
        from_cursors=from_cursors,
        to_cursors=to_cursors,
    )
    assert line_occurrence_digest(closed[0]) == line_occurrence_digest(replayed[0])

    too_small = derive_window_close_occurrences(
        line_id=LINE_ID,
        occurrence_epoch=1,
        trigger=trigger,
        from_cursors=from_cursors,
        to_cursors=_sorted_cursors(_cursor("alpha", 1, "alpha-1")),
    )
    assert too_small == ()

    with pytest.raises(LineRuntimeRefusal) as regressed:
        derive_window_close_occurrences(
            line_id=LINE_ID,
            occurrence_epoch=1,
            trigger=trigger,
            from_cursors=to_cursors,
            to_cursors=from_cursors,
        )
    assert regressed.value.code == "playbill.line.window_cursor_regressed"


def test_manual_derivation_dedupes_and_orders_request_handles() -> None:
    derived = derive_manual_occurrences(
        line_id=LINE_ID,
        occurrence_epoch=1,
        request_ids=("beta", "alpha", "beta"),
    )
    assert tuple(item.request_id for item in derived) == ("alpha", "beta")


# --------------------------------------------------------------------------
# Scheduler: occurrences, attempts, and restart
# --------------------------------------------------------------------------


def _manual(request_id: str, *, epoch: int = 1) -> ManualOccurrenceV1:
    return ManualOccurrenceV1(line_id=LINE_ID, occurrence_epoch=epoch, request_id=request_id)


def test_materialization_is_idempotent_and_attempts_do_not_change_identity(tmp_path) -> None:
    harness = _harness(tmp_path)
    occurrence = _manual("request-one")

    first = harness.materialize(occurrence)
    repeated = harness.materialize(occurrence)
    assert first == repeated
    assert first.occurrence_digest == line_occurrence_digest(occurrence)

    head = harness.journal.read_head(_stream(), "line-control")
    assert head.sequence == 1

    started = harness.scheduler.start_attempt(
        first.occurrence_digest,
        actor_context=_actor(),
        started_at=NOW,
    )
    assert started.attempt.attempt == 1 and started.in_flight
    finalized = harness.scheduler.finalize_attempt(
        first.occurrence_digest,
        1,
        status="failed",
        actor_context=_actor(),
        finalized_at=NOW + timedelta(seconds=5),
        failure="provider timeout",
    )
    assert finalized.status == "failed"

    retried = harness.scheduler.start_attempt(
        first.occurrence_digest,
        actor_context=_actor(),
        started_at=NOW + timedelta(seconds=65),
    )
    assert retried.attempt.attempt == 2
    assert retried.attempt.occurrence_digest == first.occurrence_digest
    assert harness.index.get_occurrence(first.occurrence_digest) == first


def test_bounded_cadence_backfill_materializes_once_and_then_resumes(tmp_path) -> None:
    harness = _harness(
        tmp_path,
        trigger=CadenceTriggerPolicyV1(cadence_policy_digest=_digest("hourly")),
        resolved=_cadence_trigger(max_backfill=2),
    )
    trigger = _cadence_trigger(max_backfill=2)
    evaluation = ANCHOR + timedelta(hours=10, minutes=30)

    derived = derive_cadence_occurrences(
        line_id=LINE_ID,
        occurrence_epoch=1,
        trigger=trigger,
        evaluation_time=evaluation,
        last_tick_index=harness.index.last_cadence_tick(
            line_id=LINE_ID,
            occurrence_epoch=1,
            cadence_policy_digest=trigger.cadence_policy_digest,
        ),
    )
    materialized = harness.scheduler.materialize_all(
        derived,
        accepted_line=harness.accepted_line,
        accepted_procedure=harness.accepted_procedure,
        actor_context=_actor(),
        materialized_at=evaluation,
    )
    assert tuple(item.sequence for item in materialized) == (1, 2, 3)
    assert (
        harness.index.last_cadence_tick(
            line_id=LINE_ID,
            occurrence_epoch=1,
            cadence_policy_digest=trigger.cadence_policy_digest,
        )
        == 10
    )

    head = harness.journal.read_head(_stream(), "line-control")
    replayed = harness.scheduler.materialize_all(
        derived,
        accepted_line=harness.accepted_line,
        accepted_procedure=harness.accepted_procedure,
        actor_context=_actor(),
        materialized_at=evaluation,
    )
    assert replayed == materialized
    assert harness.journal.read_head(_stream(), "line-control") == head

    resumed = derive_cadence_occurrences(
        line_id=LINE_ID,
        occurrence_epoch=1,
        trigger=trigger,
        evaluation_time=evaluation + timedelta(hours=1),
        last_tick_index=harness.index.last_cadence_tick(
            line_id=LINE_ID,
            occurrence_epoch=1,
            cadence_policy_digest=trigger.cadence_policy_digest,
        ),
    )
    assert tuple(item.tick_index for item in resumed) == (11,)


def test_read_paths_never_append_to_the_control_journal(tmp_path) -> None:
    harness = _harness(tmp_path)
    harness.materialize(_manual("request-one"))
    before = harness.journal.read_head(_stream(), "line-control")

    harness.scheduler.refresh()
    harness.index.occurrences(line_id=LINE_ID)
    harness.index.attempts(line_occurrence_digest(_manual("request-one")))
    harness.index.in_flight_occurrence_digests(line_id=LINE_ID, occurrence_epoch=1)

    assert harness.journal.read_head(_stream(), "line-control") == before


def test_restart_reproduces_identical_occurrence_and_attempt_identities(tmp_path) -> None:
    harness = _harness(tmp_path)
    first = harness.materialize(_manual("request-one"))
    second = harness.materialize(_manual("request-two"))
    harness.scheduler.start_attempt(
        first.occurrence_digest,
        actor_context=_actor(),
        started_at=NOW,
    )
    harness.scheduler.finalize_attempt(
        first.occurrence_digest,
        1,
        status="succeeded",
        actor_context=_actor(),
        finalized_at=NOW + timedelta(seconds=1),
    )
    expected_occurrences = harness.index.occurrences(line_id=LINE_ID)
    expected_attempts = harness.index.attempts(first.occurrence_digest)

    harness.index.close()
    (tmp_path / "line-index.sqlite3").unlink()
    rebuilt_index = LineOccurrenceIndex(tmp_path / "line-index.sqlite3")
    restarted = LineScheduler(
        journal=harness.journal,
        bodies=harness.bodies,
        index=rebuilt_index,
        deployment=harness.deployment,
        lease=harness.lease,
        trigger=ResolvedManualTriggerV1(dispatch=_dispatch()),
        accepted_coordinate=_coordinate(),
    )
    restarted.refresh()

    assert rebuilt_index.occurrences(line_id=LINE_ID) == expected_occurrences
    assert rebuilt_index.attempts(first.occurrence_digest) == expected_attempts
    assert rebuilt_index.get_occurrence(second.occurrence_digest) == second
    # A restarted scheduler re-derives the same identity and appends nothing new.
    head = harness.journal.read_head(_stream(), "line-control")
    assert (
        restarted.materialize(
            _manual("request-one"),
            accepted_line=harness.accepted_line,
            accepted_procedure=harness.accepted_procedure,
            actor_context=_actor(),
            materialized_at=NOW + timedelta(hours=1),
        )
        == first
    )
    assert harness.journal.read_head(_stream(), "line-control") == head


def test_trigger_epoch_history_keeps_both_epochs_distinct(tmp_path) -> None:
    cadence = CadenceTriggerPolicyV1(cadence_policy_digest=_digest("hourly"))
    harness = _harness(
        tmp_path,
        trigger=cadence,
        resolved=_cadence_trigger(),
    )
    tick = CadenceOccurrenceV1(
        line_id=LINE_ID,
        occurrence_epoch=1,
        cadence_policy_digest=_digest("hourly"),
        tick_index=1,
    )
    first_epoch = harness.materialize(tick)

    successor_trigger = CadenceTriggerPolicyV1(cadence_policy_digest=_digest("half-hourly"))
    successor, successor_procedure = _accepted_line(
        trigger=successor_trigger,
        epoch=2,
        predecessor_digest=harness.accepted_line.artifact_digest,
    )
    deployment = revise_line_deployment(
        harness.deployment,
        accepted=successor,
        activated_at=NOW + timedelta(hours=1),
    )
    lease = acquire_line_lease(
        harness.journal,
        deployment,
        fencing_token="runner-a",
        acquired_at=NOW + timedelta(hours=1),
    )
    scheduler = LineScheduler(
        journal=harness.journal,
        bodies=harness.bodies,
        index=harness.index,
        deployment=deployment,
        lease=lease,
        trigger=ResolvedCadenceTriggerV1(
            cadence_policy_digest=_digest("half-hourly"),
            anchor=ANCHOR,
            interval=HOUR,
            max_backfill_occurrences=2,
            dispatch=_dispatch(),
        ),
        accepted_coordinate=_coordinate(),
    )
    second_epoch = scheduler.materialize(
        CadenceOccurrenceV1(
            line_id=LINE_ID,
            occurrence_epoch=2,
            cadence_policy_digest=_digest("half-hourly"),
            tick_index=1,
        ),
        accepted_line=successor,
        accepted_procedure=successor_procedure,
        actor_context=_actor(),
        materialized_at=NOW + timedelta(hours=1),
    )

    assert first_epoch.occurrence_digest != second_epoch.occurrence_digest
    assert len(harness.index.occurrences(line_id=LINE_ID)) == 2
    assert harness.index.occurrences(line_id=LINE_ID, occurrence_epoch=1) == (first_epoch,)
    assert harness.index.occurrences(line_id=LINE_ID, occurrence_epoch=2) == (second_epoch,)
    assert (
        harness.index.last_cadence_tick(
            line_id=LINE_ID,
            occurrence_epoch=1,
            cadence_policy_digest=_digest("hourly"),
        )
        == 1
    )


def test_occurrence_stays_pinned_to_the_linespec_that_derived_it(tmp_path) -> None:
    harness = _harness(tmp_path)
    occurrence = _manual("request-one")
    original = harness.materialize(occurrence)

    successor, successor_procedure = _accepted_line(
        parameters={"status": "closed"},
        predecessor_digest=harness.accepted_line.artifact_digest,
    )
    assert successor.artifact_digest != harness.accepted_line.artifact_digest
    deployment = revise_line_deployment(
        harness.deployment,
        accepted=successor,
        activated_at=NOW + timedelta(minutes=1),
    )
    lease = acquire_line_lease(
        harness.journal,
        deployment,
        fencing_token="runner-a",
        acquired_at=NOW + timedelta(minutes=1),
    )
    scheduler = LineScheduler(
        journal=harness.journal,
        bodies=harness.bodies,
        index=harness.index,
        deployment=deployment,
        lease=lease,
        trigger=ResolvedManualTriggerV1(dispatch=_dispatch()),
        accepted_coordinate=_coordinate(),
    )

    still_pinned = scheduler.materialize(
        occurrence,
        accepted_line=successor,
        accepted_procedure=successor_procedure,
        actor_context=_actor(),
        materialized_at=NOW + timedelta(minutes=1),
    )
    assert still_pinned.line_spec_digest == original.line_spec_digest
    assert still_pinned.line_spec_digest != successor.artifact_digest

    scheduler.start_attempt(
        original.occurrence_digest,
        actor_context=_actor(),
        started_at=NOW + timedelta(minutes=2),
    )
    records = harness.journal.all_records(_stream(), "line-control")
    assert records[-1].record.event_kind == "attempt_started"
    assert records[-1].record.line_spec_digest == original.line_spec_digest


def test_serial_overlap_blocks_a_concurrent_second_occurrence(tmp_path) -> None:
    harness = _harness(tmp_path)
    first = harness.materialize(_manual("request-one"))
    second = harness.materialize(_manual("request-two"))
    harness.scheduler.start_attempt(
        first.occurrence_digest,
        actor_context=_actor(),
        started_at=NOW,
    )

    with pytest.raises(LineRuntimeRefusal) as blocked:
        harness.scheduler.start_attempt(
            second.occurrence_digest,
            actor_context=_actor(),
            started_at=NOW,
        )
    assert blocked.value.code == "playbill.line.overlap_blocked"

    with pytest.raises(LineRuntimeRefusal) as in_flight:
        harness.scheduler.start_attempt(
            first.occurrence_digest,
            actor_context=_actor(),
            started_at=NOW,
        )
    assert in_flight.value.code == "playbill.line.attempt_in_flight"

    concurrent = LineScheduler(
        journal=harness.journal,
        bodies=harness.bodies,
        index=harness.index,
        deployment=harness.deployment,
        lease=harness.lease,
        trigger=ResolvedManualTriggerV1(dispatch=_dispatch(overlap="concurrent")),
        accepted_coordinate=_coordinate(),
    )
    allowed = concurrent.start_attempt(
        second.occurrence_digest,
        actor_context=_actor(),
        started_at=NOW,
    )
    assert allowed.attempt.attempt == 1


def test_retry_budget_and_backoff_bound_attempts(tmp_path) -> None:
    harness = _harness(tmp_path)
    harness.scheduler.trigger = ResolvedManualTriggerV1(dispatch=_dispatch(max_attempts=2))
    occurrence = harness.materialize(_manual("request-one"))

    harness.scheduler.start_attempt(
        occurrence.occurrence_digest,
        actor_context=_actor(),
        started_at=NOW,
    )
    harness.scheduler.finalize_attempt(
        occurrence.occurrence_digest,
        1,
        status="failed",
        actor_context=_actor(),
        finalized_at=NOW + timedelta(seconds=1),
    )

    with pytest.raises(LineRuntimeRefusal) as pending:
        harness.scheduler.start_attempt(
            occurrence.occurrence_digest,
            actor_context=_actor(),
            started_at=NOW + timedelta(seconds=30),
        )
    assert pending.value.code == "playbill.line.retry_backoff_pending"

    harness.scheduler.start_attempt(
        occurrence.occurrence_digest,
        actor_context=_actor(),
        started_at=NOW + timedelta(seconds=61),
    )
    harness.scheduler.finalize_attempt(
        occurrence.occurrence_digest,
        2,
        status="budget_exhausted",
        actor_context=_actor(),
        finalized_at=NOW + timedelta(seconds=62),
    )
    with pytest.raises(LineRuntimeRefusal) as exhausted:
        harness.scheduler.start_attempt(
            occurrence.occurrence_digest,
            actor_context=_actor(),
            started_at=NOW + timedelta(hours=4),
        )
    assert exhausted.value.code == "playbill.line.attempt_budget_exhausted"


def test_a_succeeded_occurrence_is_terminal_for_its_epoch(tmp_path) -> None:
    harness = _harness(tmp_path)
    occurrence = harness.materialize(_manual("request-one"))
    harness.scheduler.start_attempt(
        occurrence.occurrence_digest,
        actor_context=_actor(),
        started_at=NOW,
    )
    harness.scheduler.finalize_attempt(
        occurrence.occurrence_digest,
        1,
        status="succeeded",
        actor_context=_actor(),
        finalized_at=NOW + timedelta(seconds=1),
    )
    with pytest.raises(LineRuntimeRefusal) as terminal:
        harness.scheduler.start_attempt(
            occurrence.occurrence_digest,
            actor_context=_actor(),
            started_at=NOW + timedelta(hours=1),
        )
    assert terminal.value.code == "playbill.line.attempt_not_retryable"

    with pytest.raises(LineRuntimeRefusal) as twice:
        harness.scheduler.finalize_attempt(
            occurrence.occurrence_digest,
            1,
            status="failed",
            actor_context=_actor(),
            finalized_at=NOW + timedelta(seconds=2),
        )
    assert twice.value.code == "playbill.line.attempt_already_finalized"


def test_scheduler_refuses_foreign_lines_stale_epochs_and_wrong_triggers(tmp_path) -> None:
    harness = _harness(tmp_path)

    with pytest.raises(LineRuntimeRefusal) as foreign:
        harness.materialize(
            ManualOccurrenceV1(line_id="other-line", occurrence_epoch=1, request_id="r"),
        )
    assert foreign.value.code == "playbill.line.occurrence_line_mismatch"

    with pytest.raises(LineRuntimeRefusal) as stale:
        harness.materialize(_manual("request-one", epoch=7))
    assert stale.value.code == "playbill.line.occurrence_epoch_stale"

    mismatched = LineScheduler(
        journal=harness.journal,
        bodies=harness.bodies,
        index=harness.index,
        deployment=harness.deployment,
        lease=harness.lease,
        trigger=_cadence_trigger(),
        accepted_coordinate=_coordinate(),
    )
    with pytest.raises(LineRuntimeRefusal) as wrong_trigger:
        mismatched.materialize(
            _manual("request-one"),
            accepted_line=harness.accepted_line,
            accepted_procedure=harness.accepted_procedure,
            actor_context=_actor(),
            materialized_at=NOW,
        )
    assert wrong_trigger.value.code == "playbill.line.trigger_policy_mismatch"

    with pytest.raises(LineRuntimeRefusal) as unknown:
        harness.scheduler.start_attempt(
            line_occurrence_digest(_manual("never-materialized")),
            actor_context=_actor(),
            started_at=NOW,
        )
    assert unknown.value.code == "playbill.line.occurrence_unknown"


def test_resolved_trigger_must_carry_the_exact_pinned_digests() -> None:
    accepted_line, _procedure = _accepted_line(
        trigger=CadenceTriggerPolicyV1(cadence_policy_digest=_digest("hourly")),
    )
    bind_resolved_trigger(accepted_line.line, _cadence_trigger())
    with pytest.raises(LineRuntimeRefusal) as mismatch:
        bind_resolved_trigger(
            accepted_line.line,
            ResolvedCadenceTriggerV1(
                cadence_policy_digest=_digest("half-hourly"),
                anchor=ANCHOR,
                interval=HOUR,
                max_backfill_occurrences=1,
                dispatch=_dispatch(),
            ),
        )
    assert mismatch.value.code == "playbill.line.trigger_policy_mismatch"


# --------------------------------------------------------------------------
# Lease fencing and backend handoff
# --------------------------------------------------------------------------


def test_lease_takeover_structurally_fences_the_previous_holder(tmp_path) -> None:
    harness = _harness(tmp_path)
    harness.materialize(_manual("request-one"))

    successor_lease = take_over_line_lease(
        harness.journal,
        harness.deployment,
        previous=harness.lease,
        fencing_token="runner-b",
        acquired_at=NOW + timedelta(minutes=1),
    )
    assert successor_lease.generation > harness.lease.generation

    with pytest.raises(LineRuntimeRefusal) as fenced:
        harness.materialize(_manual("request-two"))
    assert fenced.value.code == "playbill.line.lease_fenced"

    successor = LineScheduler(
        journal=harness.journal,
        bodies=harness.bodies,
        index=harness.index,
        deployment=harness.deployment,
        lease=successor_lease,
        trigger=ResolvedManualTriggerV1(dispatch=_dispatch()),
        accepted_coordinate=_coordinate(),
    )
    written = successor.materialize(
        _manual("request-two"),
        accepted_line=harness.accepted_line,
        accepted_procedure=harness.accepted_procedure,
        actor_context=_actor(),
        materialized_at=NOW + timedelta(minutes=1),
    )
    assert written.sequence == 2

    with pytest.raises(LineRuntimeRefusal) as reused:
        take_over_line_lease(
            harness.journal,
            harness.deployment,
            previous=successor_lease,
            fencing_token="runner-b",
            acquired_at=NOW,
        )
    assert reused.value.code == "playbill.line.lease_takeover_token_reused"

    with pytest.raises(LineRuntimeRefusal) as mismatch:
        take_over_line_lease(
            harness.journal,
            harness.deployment,
            previous=harness.lease,
            fencing_token="runner-c",
            acquired_at=NOW,
        )
    assert mismatch.value.code == "playbill.line.lease_takeover_token_mismatch"


def test_verified_backend_handoff_preserves_every_line_identity(tmp_path) -> None:
    harness = _harness(tmp_path)
    first = harness.materialize(_manual("request-one"))
    harness.scheduler.start_attempt(
        first.occurrence_digest,
        actor_context=_actor(),
        started_at=NOW,
    )
    harness.scheduler.finalize_attempt(
        first.occurrence_digest,
        1,
        status="failed",
        actor_context=_actor(),
        finalized_at=NOW + timedelta(seconds=1),
    )
    before_occurrences = harness.index.occurrences(line_id=LINE_ID)
    before_attempts = harness.index.attempts(first.occurrence_digest)
    source_records = harness.journal.all_records(_stream(), "line-control")

    target = _backend(tmp_path, "journal-b")
    signer = _HeadSigner(Ed25519PrivateKey.generate())
    revised, head_vector = rebind_line_deployment_backend(
        harness.deployment,
        source=harness.journal,
        target=target,
        backend_id="local-b",
        source_fencing_token=harness.lease.fencing_token,
        target_fencing_token="runner-b",
        signer=signer,
        expected_head_public_key=signer.private_key.public_key().public_bytes_raw().hex(),
        asserted_at=NOW + timedelta(minutes=1),
        activated_at=NOW + timedelta(minutes=1),
    )

    assert revised.revision == 2
    assert revised.line_spec_digest == harness.deployment.line_spec_digest
    assert revised.occurrence_epoch == harness.deployment.occurrence_epoch
    assert revised.line_id == harness.deployment.line_id
    assert revised.journal_binding.logical_identity == (
        harness.deployment.journal_binding.logical_identity
    )
    assert revised.journal_binding.backend_id == "local-b"
    assert revised.handoff_head_vector_digest == head_vector.vector_digest
    assert revised.predecessor_deployment_digest == line_deployment_digest(harness.deployment)
    assert target.all_records(_stream(), "line-control") == source_records

    # The old deployment writer is fenced; the new home continues the same chain.
    with pytest.raises(LineRuntimeRefusal) as fenced:
        verify_line_lease(harness.journal, harness.deployment, harness.lease)
    assert fenced.value.code == "playbill.line.lease_fenced"

    moved_index = LineOccurrenceIndex(tmp_path / "line-index-b.sqlite3")
    moved_bodies_root = tmp_path / "cas-journal-a"
    moved = LineScheduler(
        journal=target,
        bodies=ContentAddressedBodyStore(moved_bodies_root),
        index=moved_index,
        deployment=revised,
        lease=LineLeaseV1(
            line_id=LINE_ID,
            deployment_digest=line_deployment_digest(revised),
            runner=revised.runner,
            fencing_token="runner-b",
            generation=1,
            acquired_at=NOW + timedelta(minutes=1),
        ),
        trigger=ResolvedManualTriggerV1(dispatch=_dispatch()),
        accepted_coordinate=_coordinate(),
    )
    moved.refresh()
    assert moved_index.occurrences(line_id=LINE_ID) == before_occurrences
    assert moved_index.attempts(first.occurrence_digest) == before_attempts

    continued = moved.materialize(
        _manual("request-two"),
        accepted_line=harness.accepted_line,
        accepted_procedure=harness.accepted_procedure,
        actor_context=_actor(),
        materialized_at=NOW + timedelta(minutes=2),
    )
    assert continued.sequence == len(source_records) + 1


def test_deployment_revision_refuses_identity_or_unverified_rebinds(tmp_path) -> None:
    harness = _harness(tmp_path)

    signer = _HeadSigner(Ed25519PrivateKey.generate())
    with pytest.raises(LineRuntimeRefusal) as same_label:
        rebind_line_deployment_backend(
            harness.deployment,
            source=harness.journal,
            target=_backend(tmp_path, "journal-c"),
            backend_id=harness.deployment.journal_binding.backend_id,
            source_fencing_token=harness.lease.fencing_token,
            target_fencing_token="runner-b",
            signer=signer,
            expected_head_public_key=signer.private_key.public_key().public_bytes_raw().hex(),
            asserted_at=NOW,
            activated_at=NOW,
        )
    assert same_label.value.code == "playbill.line.deployment_rebind_reuses_backend_id"

    with pytest.raises(LineRuntimeRefusal) as unverified:
        revise_line_deployment(
            harness.deployment,
            journal_binding=_binding("local-b"),
            activated_at=NOW,
        )
    assert unverified.value.code == "playbill.line.deployment_rebind_unverified"

    with pytest.raises(LineRuntimeRefusal) as spurious:
        revise_line_deployment(
            harness.deployment,
            handoff_head_vector_digest=_digest("head-vector"),
            activated_at=NOW,
        )
    assert spurious.value.code == "playbill.line.deployment_rebind_unverified"

    other_stream = JournalStreamIdentityV1(
        instance_id="instance-a",
        journal_family=PROCEDURE_EXHAUST_JOURNAL_FAMILY,
        stream_id="other-lines",
    )
    with pytest.raises(LineRuntimeRefusal) as moved_stream:
        revise_line_deployment(
            harness.deployment,
            journal_binding=_binding("local-b").model_copy(update={"logical_stream": other_stream}),
            handoff_head_vector_digest=_digest("head-vector"),
            activated_at=NOW,
        )
    assert moved_stream.value.code == "playbill.line.deployment_stream_identity_changed"

    successor, _procedure = _accepted_line(
        parameters={"status": "closed"},
        predecessor_digest=harness.accepted_line.artifact_digest,
    )
    with pytest.raises(LineRuntimeRefusal) as rebind_with_spec:
        revise_line_deployment(
            harness.deployment,
            accepted=successor,
            journal_binding=_binding("local-b"),
            handoff_head_vector_digest=_digest("head-vector"),
            activated_at=NOW,
        )
    assert rebind_with_spec.value.code == "playbill.line.deployment_rebind_changed_spec"

    foreign_line = LineSpecV1.model_validate(
        {
            **harness.accepted_line.line.model_dump(mode="json"),
            "identity": {"kind": "Line", "name": "other-line"},
        }
    )
    foreign_accepted = AcceptedLineSpecV1(
        path=line_spec_path("other-line"),
        line=foreign_line,
        artifact_digest=line_spec_digest(foreign_line).tagged,
    )
    with pytest.raises(LineRuntimeRefusal) as foreign:
        revise_line_deployment(
            harness.deployment,
            accepted=foreign_accepted,
            activated_at=NOW,
        )
    assert foreign.value.code == "playbill.line.deployment_line_identity_changed"


def test_a_lease_from_a_superseded_revision_is_not_current(tmp_path) -> None:
    harness = _harness(tmp_path)
    successor, _procedure = _accepted_line(
        parameters={"status": "closed"},
        predecessor_digest=harness.accepted_line.artifact_digest,
    )
    revised = revise_line_deployment(
        harness.deployment,
        accepted=successor,
        activated_at=NOW,
    )
    with pytest.raises(LineRuntimeRefusal) as stale:
        verify_line_lease(harness.journal, revised, harness.lease)
    assert stale.value.code == "playbill.line.lease_not_current"


def test_a_second_runner_cannot_acquire_a_held_lease(tmp_path) -> None:
    harness = _harness(tmp_path)
    with pytest.raises(LineRuntimeRefusal) as held:
        acquire_line_lease(
            harness.journal,
            harness.deployment,
            fencing_token="runner-b",
            acquired_at=NOW,
        )
    assert held.value.code == "playbill.line.lease_held"


def test_runner_and_backend_identity_stay_out_of_governed_digests(tmp_path) -> None:
    harness = _harness(tmp_path)
    payload = harness.accepted_line.line.model_dump(mode="json")
    rendered = repr(payload)
    assert "runner" not in rendered and "local-a" not in rendered
    assert line_spec_digest(harness.accepted_line.line).tagged == (
        harness.accepted_line.artifact_digest
    )

    occurrence = _manual("request-one")
    fields = occurrence.model_dump(mode="json")
    assert "runner_id" not in fields and "backend_id" not in fields
    rebound = harness.deployment.model_copy(
        update={"journal_binding": _binding("local-b")},
    )
    assert line_occurrence_digest(occurrence) == line_occurrence_digest(occurrence)
    assert line_deployment_digest(rebound) != line_deployment_digest(harness.deployment)
