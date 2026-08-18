"""Pure occurrence-backlog accounting, coverage honesty, and fenced lapse law."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cruxible_core.playbill.acquisition_policies import (
    IndependentCoherenceV1,
    InputAcquisitionRuleV1,
    SourceAcquisitionPolicyV1,
    acquisition_policy_digest,
)
from cruxible_core.playbill.artifacts import ArtifactAuthority, ArtifactIdentity
from cruxible_core.playbill.canonical import canonical_bytes
from cruxible_core.playbill.capture_journal import (
    CaptureJournalError,
    CaptureLandingEventV1,
    InMemoryCaptureLandingJournal,
    capture_landing_idempotency_key,
)
from cruxible_core.playbill.captures import CanonicalDurationV1, build_cas_capture
from cruxible_core.playbill.cas import BodyAccessContext, ContentAddressedBodyStore
from cruxible_core.playbill.exhaust import (
    JournalHeadVectorV1,
    LocalJournalBackend,
    build_journal_export,
    build_journal_head_manifest,
    import_journal_export,
    parse_journal_payload,
)
from cruxible_core.playbill.line_scheduler import (
    LineOccurrenceIndex,
    LineScheduler,
    ResolvedCadenceTriggerV1,
    ResolvedCaptureLandingTriggerV1,
    ResolvedWindowCloseTriggerV1,
)
from cruxible_core.playbill.lines import (
    LineDeploymentV1,
    LineLeaseV1,
    LineRuntimeRefusal,
    acquire_line_lease,
    line_partition_pairs,
)
from cruxible_core.playbill.occurrence_backlog import (
    COVERAGE_FORKED,
    COVERAGE_HEAD_INCOMPLETE,
    COVERAGE_MISSING,
    COVERAGE_TRUNCATED,
    COVERAGE_UNAUTHORIZED,
    COVERAGE_UNVERIFIED,
    LAPSE_OUT_OF_AGE,
    LAPSE_UNREPLAYABLE,
    BacklogOccurrenceV1,
    LineLapseRecorder,
    LineOccurrenceBacklogV1,
    acquisition_lapse_horizon,
    build_backlog_occurrences,
    build_occurrence_lapse,
    cadence_backlog_candidates,
    cadence_window_end,
    classify_occurrence_backlog,
    occurrence_lapse_effective_at,
    occurrence_lapse_identity_digest,
    read_landing_backlog_candidates,
    read_line_control_state,
    read_line_lapse_records,
    read_window_close_backlog_candidates,
    require_original_window_acquirable,
    unavailable_occurrence_backlog,
    verify_line_journal_coverage,
)
from cruxible_core.playbill.occurrences import (
    CadenceOccurrenceV1,
    ManualOccurrenceV1,
    line_occurrence_digest,
)
from tests.test_playbill._pc_c_support import (
    body_store,
    capture_contract,
    provider,
    provider_run,
)
from tests.test_playbill._pc_c_support import (
    digest as pc_c_digest,
)
from tests.test_playbill.test_line_scheduler import (
    ANCHOR,
    HOUR,
    LINE_ID,
    NOW,
    _accepted_line,
    _actor,
    _backend,
    _binding,
    _coordinate,
    _cursor,
    _digest,
    _dispatch,
    _HeadSigner,
    _landing_event,
    _sorted_cursors,
    _stream,
)

MINUTE = 60_000_000
DAY = 86_400_000_000


# ---------------------------------------------------------------------------
# Policy fixtures
# ---------------------------------------------------------------------------


def _rule(
    name: str,
    *,
    requirement: str = "required",
    replayability: tuple[str, ...] = ("exact",),
    max_age_microseconds: int | None = None,
    unavailable: str = "refuse",
) -> InputAcquisitionRuleV1:
    return InputAcquisitionRuleV1(
        input_name=name,
        requirement=requirement,  # type: ignore[arg-type]
        permitted_replayability=replayability,  # type: ignore[arg-type]
        max_age=(
            None
            if max_age_microseconds is None
            else CanonicalDurationV1(microseconds=max_age_microseconds)
        ),
        on_unavailable=unavailable,  # type: ignore[arg-type]
        on_stale=unavailable,  # type: ignore[arg-type]
        on_oversized=unavailable,  # type: ignore[arg-type]
        on_conflict="preserve",
        conservative_default=(False if requirement == "conservative_default" else None),
    )


def _policy(
    *rules: InputAcquisitionRuleV1, name: str = "line-acquisition"
) -> SourceAcquisitionPolicyV1:
    return SourceAcquisitionPolicyV1(
        identity=ArtifactIdentity(kind="SourceAcquisitionPolicy", name=name),
        inputs=tuple(sorted(rules, key=lambda item: item.input_name.encode("utf-8"))),
        coherence=IndependentCoherenceV1(),
        authority=ArtifactAuthority(propose_roles=("owner",), approve_roles=("owner",)),
    )


def _replayable_policy(*, max_age_microseconds: int | None = None) -> SourceAcquisitionPolicyV1:
    return _policy(_rule("orders", max_age_microseconds=max_age_microseconds))


def _unreplayable_policy() -> SourceAcquisitionPolicyV1:
    return _policy(_rule("orders", replayability=("attested_only",)))


# ---------------------------------------------------------------------------
# Occurrence fixtures
# ---------------------------------------------------------------------------


def _cadence_trigger(*, max_backfill: int = 48) -> ResolvedCadenceTriggerV1:
    return ResolvedCadenceTriggerV1(
        cadence_policy_digest=_digest("hourly"),
        anchor=ANCHOR,
        interval=HOUR,
        max_backfill_occurrences=max_backfill,
        dispatch=_dispatch(),
    )


def _cadence_occurrence(tick_index: int, *, epoch: int = 1) -> CadenceOccurrenceV1:
    return CadenceOccurrenceV1(
        line_id=LINE_ID,
        occurrence_epoch=epoch,
        cadence_policy_digest=_digest("hourly"),
        tick_index=tick_index,
    )


def _backlog_occurrence(
    tick_index: int,
    *,
    attempt_count: int = 0,
    latest_status: str | None = None,
) -> BacklogOccurrenceV1:
    occurrence = _cadence_occurrence(tick_index)
    return BacklogOccurrenceV1(
        occurrence=occurrence,
        occurrence_digest=line_occurrence_digest(occurrence),
        window_end=cadence_window_end(_cadence_trigger(), tick_index),
        attempt_count=attempt_count,
        latest_status=latest_status,  # type: ignore[arg-type]
    )


def _head_vector(journal: LocalJournalBackend, deployment: LineDeploymentV1) -> JournalHeadVectorV1:
    return journal.read_head_vector(line_partition_pairs(deployment))


def _classify(
    occurrences: tuple[BacklogOccurrenceV1, ...],
    *,
    policy: SourceAcquisitionPolicyV1,
    evaluation_time: datetime,
    head_vector: JournalHeadVectorV1 | None = None,
    line_spec_digest: str | None = None,
) -> LineOccurrenceBacklogV1:
    return classify_occurrence_backlog(
        line_id=LINE_ID,
        occurrence_epoch=1,
        line_spec_digest=line_spec_digest or _digest("line-spec"),
        policy=policy,
        occurrences=occurrences,
        head_vector=head_vector or JournalHeadVectorV1(partitions=()),
        evaluation_time=evaluation_time,
    )


def _wire(report: LineOccurrenceBacklogV1) -> bytes:
    return canonical_bytes(report.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Deployment harness
# ---------------------------------------------------------------------------


class _Deployed:
    def __init__(
        self,
        *,
        journal: LocalJournalBackend,
        bodies: ContentAddressedBodyStore,
        index: LineOccurrenceIndex,
        deployment: LineDeploymentV1,
        lease: LineLeaseV1,
        scheduler: LineScheduler,
        accepted_line,
        accepted_procedure,
    ) -> None:
        self.journal = journal
        self.bodies = bodies
        self.index = index
        self.deployment = deployment
        self.lease = lease
        self.scheduler = scheduler
        self.accepted_line = accepted_line
        self.accepted_procedure = accepted_procedure

    @property
    def head_vector(self) -> JournalHeadVectorV1:
        return _head_vector(self.journal, self.deployment)

    def recorder(self) -> LineLapseRecorder:
        return LineLapseRecorder(
            journal=self.journal,
            bodies=self.bodies,
            deployment=self.deployment,
            lease=self.lease,
            accepted_coordinate=_coordinate(),
        )

    def record_lapse(self, lapse, *, recorded_at: datetime, recorder=None):
        return (recorder or self.recorder()).record(
            lapse,
            line_spec_digest=self.accepted_line.artifact_digest,
            procedure_artifact_digest=self.accepted_procedure.artifact_digest,
            definition_digest=self.accepted_procedure.procedure.definition_digest,
            actor_context=_actor(),
            recorded_at=recorded_at,
        )

    def control_state(self, *, can_read_body: bool = True, declared=None):
        return read_line_control_state(
            journal=self.journal,
            bodies=self.bodies,
            index=self.index,
            deployment=self.deployment,
            declared_head_vector=declared if declared is not None else self.head_vector,
            access=BodyAccessContext(principal_id="reader", can_read_body=can_read_body),
        )


def _deploy(
    tmp_path: Path,
    *,
    backend_name: str = "journal-a",
    index_name: str = "backlog-index.sqlite3",
    fencing_token: str = "runner-a",
) -> _Deployed:
    from cruxible_core.playbill.lines import LineRunnerIdentityV1, bind_line_deployment

    accepted_line, accepted_procedure = _accepted_line()
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
    from cruxible_core.playbill.line_scheduler import ResolvedManualTriggerV1

    scheduler = LineScheduler(
        journal=journal,
        bodies=bodies,
        index=index,
        deployment=deployment,
        lease=lease,
        trigger=ResolvedManualTriggerV1(dispatch=_dispatch()),
        accepted_coordinate=_coordinate(),
    )
    return _Deployed(
        journal=journal,
        bodies=bodies,
        index=index,
        deployment=deployment,
        lease=lease,
        scheduler=scheduler,
        accepted_line=accepted_line,
        accepted_procedure=accepted_procedure,
    )


# ---------------------------------------------------------------------------
# The pinned policy's lapse law
# ---------------------------------------------------------------------------


def test_lapse_horizon_reads_only_replayability_and_age() -> None:
    horizon = acquisition_lapse_horizon(_replayable_policy(max_age_microseconds=30 * MINUTE))
    assert horizon.unreplayable_inputs == ()
    assert horizon.age_bound_microseconds == 30 * MINUTE
    assert horizon.age_bound_inputs == ("orders",)

    unreplayable = acquisition_lapse_horizon(_unreplayable_policy())
    assert unreplayable.unreplayable_inputs == ("orders",)
    assert unreplayable.age_bound_microseconds is None

    # The tightest required age bound governs the whole occurrence window.
    mixed = acquisition_lapse_horizon(
        _policy(
            _rule("orders", max_age_microseconds=DAY),
            _rule("prices", max_age_microseconds=30 * MINUTE),
        )
    )
    assert mixed.age_bound_microseconds == 30 * MINUTE
    assert mixed.age_bound_inputs == ("orders", "prices")


def test_only_required_inputs_bound_truthful_fulfilment() -> None:
    # An optional or defaulted input declares a truthful completion mode that
    # needs no fresh exact read, so it can never lapse the original window.
    for requirement, unavailable in (
        ("optional", "omit_optional"),
        ("conservative_default", "declared_conservative_default"),
    ):
        horizon = acquisition_lapse_horizon(
            _policy(
                _rule("orders", max_age_microseconds=DAY),
                _rule(
                    "enrichment",
                    requirement=requirement,
                    replayability=("attested_only",),
                    max_age_microseconds=MINUTE,
                    unavailable=unavailable,
                ),
            )
        )
        assert horizon.unreplayable_inputs == ()
        assert horizon.age_bound_inputs == ("orders",)
        assert horizon.age_bound_microseconds == DAY


def test_live_unavailability_is_never_guessed_by_a_read_only_scan() -> None:
    # Two policies differing only in the optional input's failure behaviours
    # classify identically: on_unavailable is applied to real acquisition
    # results, never to a status scan.
    lenient = _policy(
        _rule("orders", max_age_microseconds=DAY),
        _rule("enrichment", requirement="optional", unavailable="omit_optional"),
    )
    strict = _policy(
        _rule("orders", max_age_microseconds=DAY),
        _rule("enrichment", requirement="optional", unavailable="refuse"),
    )
    assert acquisition_lapse_horizon(lenient).unreplayable_inputs == ()
    assert acquisition_lapse_horizon(strict).unreplayable_inputs == ()

    occurrences = (_backlog_occurrence(1),)
    evaluation = ANCHOR + timedelta(hours=6)
    lenient_report = _classify(occurrences, policy=lenient, evaluation_time=evaluation)
    strict_report = _classify(occurrences, policy=strict, evaluation_time=evaluation)
    assert lenient_report.late_eligible_count == 1
    assert _wire(
        lenient_report.model_copy(
            update={"acquisition_policy_digest": strict_report.acquisition_policy_digest}
        )
    ) == _wire(strict_report)


# ---------------------------------------------------------------------------
# Pure classification
# ---------------------------------------------------------------------------


def test_in_age_replay_is_late_and_keeps_the_original_window_time() -> None:
    policy = _replayable_policy(max_age_microseconds=6 * 3_600_000_000)
    occurrence = _backlog_occurrence(1)
    evaluation = ANCHOR + timedelta(hours=5)
    report = _classify((occurrence,), policy=policy, evaluation_time=evaluation)

    assert report.late_eligible_count == 1
    assert report.lapsed_count == 0
    entry = report.late_eligible[0]
    assert entry.state == "late_eligible"
    assert entry.reason_codes == ()
    # The window keeps its own time; the scan never relabels it as "now".
    assert entry.window_end == ANCHOR + timedelta(hours=2)
    assert entry.window_end != report.evaluation_time
    assert report.evaluation_time == evaluation
    # The instant it will stop being truthful is already known and pure.
    assert entry.lapse_effective_at == ANCHOR + timedelta(hours=8)


def test_out_of_age_and_unreplayable_windows_lapse_disjointly() -> None:
    stale_policy = _replayable_policy(max_age_microseconds=2 * 3_600_000_000)
    occurrence = _backlog_occurrence(1)
    report = _classify(
        (occurrence,),
        policy=stale_policy,
        evaluation_time=ANCHOR + timedelta(hours=10),
    )
    assert report.late_eligible_count == 0
    assert report.lapsed_count == 1
    assert report.lapsed[0].reason_codes == (LAPSE_OUT_OF_AGE,)
    assert report.lapsed[0].lapse_effective_at == ANCHOR + timedelta(hours=4)

    unreplayable = _classify(
        (occurrence,),
        policy=_unreplayable_policy(),
        evaluation_time=ANCHOR + timedelta(hours=2),
    )
    assert unreplayable.lapsed_count == 1
    assert unreplayable.lapsed[0].reason_codes == (LAPSE_UNREPLAYABLE,)
    # An unreplayable input lapses the instant the window closes: never late.
    assert unreplayable.lapsed[0].lapse_effective_at == occurrence.window_end

    both = _classify(
        (occurrence,),
        policy=_policy(
            _rule("orders", replayability=("attested_only",), max_age_microseconds=MINUTE)
        ),
        evaluation_time=ANCHOR + timedelta(hours=10),
    )
    assert both.lapsed[0].reason_codes == (LAPSE_OUT_OF_AGE, LAPSE_UNREPLAYABLE)

    # The two states are disjoint and exhaustive over passed, undisposed windows.
    for report_under_test in (report, unreplayable, both):
        digests = {entry.occurrence_digest for entry in report_under_test.late_eligible}
        assert not digests & {entry.occurrence_digest for entry in report_under_test.lapsed}


def test_unbounded_policy_never_lapses_and_windows_must_have_passed() -> None:
    policy = _replayable_policy()
    assert occurrence_lapse_effective_at(NOW, acquisition_lapse_horizon(policy)) is None

    report = _classify(
        (_backlog_occurrence(1),),
        policy=policy,
        evaluation_time=ANCHOR + timedelta(days=400),
    )
    assert report.lapsed_count == 0
    assert report.late_eligible[0].lapse_effective_at is None

    # A window that has not closed yet is not backlog at all.
    not_yet = _classify(
        (_backlog_occurrence(5),),
        policy=policy,
        evaluation_time=ANCHOR + timedelta(hours=5, minutes=30),
    )
    assert not_yet.late_eligible_count == 0
    assert not_yet.lapsed_count == 0


def test_terminal_dispositions_leave_the_backlog_and_retries_stay_in_it() -> None:
    policy = _replayable_policy()
    evaluation = ANCHOR + timedelta(hours=10)
    succeeded = _backlog_occurrence(1, attempt_count=1, latest_status="succeeded")
    refused = _backlog_occurrence(2, attempt_count=1, latest_status="refused")
    failed = _backlog_occurrence(3, attempt_count=2, latest_status="failed")
    in_flight = _backlog_occurrence(4, attempt_count=1)

    report = _classify(
        (succeeded, refused, failed, in_flight),
        policy=policy,
        evaluation_time=evaluation,
    )
    assert report.late_eligible_count == 2
    assert {entry.occurrence_digest for entry in report.late_eligible} == {
        failed.occurrence_digest,
        in_flight.occurrence_digest,
    }


def test_backlog_refuses_a_foreign_line_or_epoch_and_manual_occurrences() -> None:
    foreign = _cadence_occurrence(1, epoch=2)
    entry = BacklogOccurrenceV1(
        occurrence=foreign,
        occurrence_digest=line_occurrence_digest(foreign),
        window_end=NOW,
    )
    with pytest.raises(LineRuntimeRefusal, match="backlog_occurrence_foreign"):
        _classify((entry,), policy=_replayable_policy(), evaluation_time=NOW)

    manual = ManualOccurrenceV1(line_id=LINE_ID, occurrence_epoch=1, request_id="request-1")
    with pytest.raises(LineRuntimeRefusal, match="backlog_manual_has_no_window"):
        build_backlog_occurrences(((manual, NOW),))


def test_report_binds_the_active_spec_identity_head_vector_and_counts() -> None:
    policy = _replayable_policy(max_age_microseconds=3 * 3_600_000_000)
    head_vector = JournalHeadVectorV1(partitions=())
    report = _classify(
        (_backlog_occurrence(1), _backlog_occurrence(2), _backlog_occurrence(9)),
        policy=policy,
        evaluation_time=ANCHOR + timedelta(hours=10),
        head_vector=head_vector,
        line_spec_digest=_digest("active-line-spec"),
    )
    assert report.line_id == LINE_ID
    assert report.occurrence_epoch == 1
    assert report.line_spec_digest == _digest("active-line-spec")
    assert report.acquisition_policy_digest == acquisition_policy_digest(policy).tagged
    assert report.head_vector_digest == head_vector.vector_digest
    assert report.coverage == "available"
    assert report.lapsed_count == 2
    assert report.late_eligible_count == 1
    # Entries are byte-order sorted by occurrence digest, not insertion order.
    assert [entry.occurrence_digest for entry in report.lapsed] == sorted(
        entry.occurrence_digest for entry in report.lapsed
    )


# ---------------------------------------------------------------------------
# Two backends, one classification
# ---------------------------------------------------------------------------


def test_passed_windows_classify_identically_across_two_backends(tmp_path: Path) -> None:
    primary = _deploy(tmp_path, backend_name="journal-a", index_name="index-a.sqlite3")
    trigger = _cadence_trigger()
    occurrences = [_cadence_occurrence(tick) for tick in range(1, 4)]
    for occurrence in occurrences:
        primary.scheduler.materialize(
            occurrence,
            accepted_line=primary.accepted_line,
            accepted_procedure=primary.accepted_procedure,
            actor_context=_actor(),
            materialized_at=NOW,
        )
    started = primary.scheduler.start_attempt(
        line_occurrence_digest(occurrences[0]),
        actor_context=_actor(),
        started_at=NOW,
    )
    primary.scheduler.finalize_attempt(
        started.attempt.occurrence_digest,
        started.attempt.attempt,
        status="succeeded",
        actor_context=_actor(),
        finalized_at=NOW + timedelta(seconds=5),
    )

    # A second, differently packed local store carrying the same logical prefix.
    mirror_root = tmp_path / "journal-b"
    mirror_root.mkdir(mode=0o700)
    mirror = LocalJournalBackend(mirror_root)
    stream = _stream()
    control = primary.deployment.journal_binding.control_partition_id
    head = primary.journal.read_head(stream, control)
    signer = _HeadSigner(Ed25519PrivateKey.generate())
    manifest = build_journal_head_manifest(
        JournalHeadVectorV1(partitions=(head,)),
        asserted_at=NOW,
        signer=signer,
    )
    bundle = build_journal_export(
        primary.journal,
        ranges=(
            primary.journal.range_from_sequences(
                stream, control, first_sequence=1, last_sequence=head.sequence
            ),
        ),
        head_manifest=manifest,
    )
    import_journal_export(
        mirror,
        bundle,
        expected_head_public_key=signer.private_key.public_key().public_bytes_raw().hex(),
    )
    declared = primary.head_vector
    assert mirror.read_head_vector(line_partition_pairs(primary.deployment)) == declared
    mirror_index = LineOccurrenceIndex(tmp_path / "index-b.sqlite3")
    mirror_state = read_line_control_state(
        journal=mirror,
        bodies=primary.bodies,
        index=mirror_index,
        deployment=primary.deployment,
        declared_head_vector=declared,
        access=BodyAccessContext(principal_id="reader", can_read_body=True),
    )
    primary_state = primary.control_state(declared=declared)
    assert primary_state.coverage == "available"
    assert mirror_state.coverage == "available"
    assert mirror_state.attempts == primary_state.attempts
    assert mirror_state.materialized == primary_state.materialized

    policy = _replayable_policy(max_age_microseconds=3 * 3_600_000_000)
    evaluation = ANCHOR + timedelta(hours=10)
    candidates = cadence_backlog_candidates(
        line_id=LINE_ID,
        occurrence_epoch=1,
        trigger=trigger,
        evaluation_time=evaluation,
        last_tick_index=3,
    )
    reports = tuple(
        classify_occurrence_backlog(
            line_id=LINE_ID,
            occurrence_epoch=1,
            line_spec_digest=primary.accepted_line.artifact_digest,
            policy=policy,
            occurrences=build_backlog_occurrences(
                tuple(
                    (occurrence, cadence_window_end(trigger, occurrence.tick_index))
                    for occurrence in occurrences
                )
                + candidates,
                attempts=state.attempt_state,
            ),
            head_vector=declared,
            evaluation_time=evaluation,
        )
        for state in (primary_state, mirror_state)
    )
    assert _wire(reports[0]) == _wire(reports[1])
    assert reports[0].coverage == "available"
    # Ticks 1..9 have closed windows; the one succeeded tick left the backlog
    # on both backends, and tick 10's window has not closed at all.
    assert reports[0].late_eligible_count == 3
    assert reports[0].lapsed_count == 5
    charged = {entry.occurrence_digest for entry in reports[0].late_eligible} | {
        entry.occurrence_digest for entry in reports[0].lapsed
    }
    assert line_occurrence_digest(occurrences[0]) not in charged
    assert line_occurrence_digest(_cadence_occurrence(10)) not in charged
    mirror_index.close()
    primary.index.close()


# ---------------------------------------------------------------------------
# Read paths never write
# ---------------------------------------------------------------------------


def _control_bytes(deployed: _Deployed) -> bytes:
    binding = deployed.deployment.journal_binding
    path = deployed.journal._record_log_path_for_testing(
        binding.logical_stream, binding.control_partition_id
    )
    return path.read_bytes()


def test_status_and_backlog_never_mutate_a_journal(tmp_path: Path) -> None:
    deployed = _deploy(tmp_path)
    occurrence = _cadence_occurrence(1)
    deployed.scheduler.materialize(
        occurrence,
        accepted_line=deployed.accepted_line,
        accepted_procedure=deployed.accepted_procedure,
        actor_context=_actor(),
        materialized_at=NOW,
    )
    before_bytes = _control_bytes(deployed)
    before_head = deployed.head_vector

    for _ in range(3):
        state = deployed.control_state()
        assert state.coverage == "available"
        report = _classify(
            build_backlog_occurrences(
                ((occurrence, cadence_window_end(_cadence_trigger(), 1)),),
                attempts=state.attempt_state,
            ),
            policy=_unreplayable_policy(),
            evaluation_time=ANCHOR + timedelta(hours=10),
            head_vector=deployed.head_vector,
        )
        assert report.lapsed_count == 1
        assert read_line_lapse_records(deployed.journal, deployed.deployment) == ()

    assert _control_bytes(deployed) == before_bytes
    assert deployed.head_vector == before_head
    deployed.index.close()


# ---------------------------------------------------------------------------
# Coverage law
# ---------------------------------------------------------------------------


def test_missing_truncated_forked_and_unauthorized_journals_are_unavailable(
    tmp_path: Path,
) -> None:
    deployed = _deploy(tmp_path)
    occurrence = _cadence_occurrence(1)
    deployed.scheduler.materialize(
        occurrence,
        accepted_line=deployed.accepted_line,
        accepted_procedure=deployed.accepted_procedure,
        actor_context=_actor(),
        materialized_at=NOW,
    )
    declared = deployed.head_vector

    # An unauthorized reader learns nothing, and learns it honestly.
    unauthorized = deployed.control_state(can_read_body=False)
    assert unauthorized.coverage == "unavailable"
    assert unauthorized.reason_codes == (COVERAGE_UNAUTHORIZED,)
    assert unauthorized.attempts == ()

    # A journal presenting no records at all against a populated declared head.
    empty_root = tmp_path / "journal-empty"
    empty_root.mkdir(mode=0o700)
    empty = LocalJournalBackend(empty_root)
    assert verify_line_journal_coverage(empty, deployed.deployment, declared) == (COVERAGE_MISSING,)

    # A head vector that does not even name this Line's partitions.
    assert verify_line_journal_coverage(
        deployed.journal,
        deployed.deployment,
        JournalHeadVectorV1(partitions=()),
    ) == (COVERAGE_HEAD_INCOMPLETE,)

    # A declared head ahead of the local prefix is truncation, not zero.
    stream = _stream()
    control = deployed.deployment.journal_binding.control_partition_id
    head = deployed.journal.read_head(stream, control)
    ahead = head.model_copy(update={"sequence": head.sequence + 5})
    truncated = JournalHeadVectorV1(
        partitions=tuple(
            ahead if item.partition_id == control else item for item in declared.partitions
        )
    )
    assert verify_line_journal_coverage(deployed.journal, deployed.deployment, truncated) == (
        COVERAGE_TRUNCATED,
    )

    # A same-height head with a different record digest is a fork.
    forked_head = head.model_copy(update={"record_digest": _digest("other-head")})
    forked = JournalHeadVectorV1(
        partitions=tuple(
            forked_head if item.partition_id == control else item for item in declared.partitions
        )
    )
    assert verify_line_journal_coverage(deployed.journal, deployed.deployment, forked) == (
        COVERAGE_FORKED,
    )

    unavailable = unavailable_occurrence_backlog(
        line_id=LINE_ID,
        occurrence_epoch=1,
        line_spec_digest=deployed.accepted_line.artifact_digest,
        policy=_replayable_policy(),
        head_vector=declared,
        evaluation_time=NOW,
        reason_codes=(COVERAGE_TRUNCATED,),
    )
    assert unavailable.coverage == "unavailable"
    assert unavailable.late_eligible is None
    assert unavailable.lapsed is None
    assert unavailable.late_eligible_count is None
    assert unavailable.lapsed_count is None
    deployed.index.close()


def test_a_corrupted_control_prefix_reports_unavailable_coverage(tmp_path: Path) -> None:
    deployed = _deploy(tmp_path)
    deployed.scheduler.materialize(
        _cadence_occurrence(1),
        accepted_line=deployed.accepted_line,
        accepted_procedure=deployed.accepted_procedure,
        actor_context=_actor(),
        materialized_at=NOW,
    )
    declared = deployed.head_vector
    binding = deployed.deployment.journal_binding
    path = deployed.journal._record_log_path_for_testing(
        binding.logical_stream, binding.control_partition_id
    )
    content = bytearray(path.read_bytes())
    content[-5] ^= 1
    path.write_bytes(content)

    assert verify_line_journal_coverage(deployed.journal, deployed.deployment, declared) == (
        COVERAGE_UNVERIFIED,
    )
    state = deployed.control_state(declared=declared)
    assert state.coverage == "unavailable"
    assert state.reason_codes == (COVERAGE_UNVERIFIED,)
    assert state.materialized == ()
    deployed.index.close()


# ---------------------------------------------------------------------------
# Landing and window-close read boundaries
# ---------------------------------------------------------------------------


def _landed(tmp_path: Path, journal: InMemoryCaptureLandingJournal, *, run_id: str, landed_at):
    contract = capture_contract()
    provider_artifact = provider(contract)
    result = build_cas_capture(
        store=body_store(tmp_path / run_id),
        contract=contract,
        source_body=b'{"order_id":"ord-1"}',
        run_coordinate=provider_run(provider_artifact, run_id=run_id),
        run_receipt_digest=pc_c_digest("backlog-receipt", run_id),
        producer=provider_artifact.identity,
        producer_binding_digest=pc_c_digest("backlog-binding", "orders"),
        observed_at=landed_at,
    )
    event = journal.append(
        instance_id="inst-backlog",
        envelope=result.envelope,
        landed_at=landed_at,
        idempotency_key=capture_landing_idempotency_key(
            instance_id="inst-backlog",
            envelope=result.envelope,
        ),
    )
    return result.envelope, event


def test_landing_windows_come_from_real_journal_reads(tmp_path: Path) -> None:
    landing_journal = InMemoryCaptureLandingJournal()
    landed_at = ANCHOR + timedelta(hours=1)
    envelope, event = _landed(tmp_path, landing_journal, run_id="run-a", landed_at=landed_at)
    trigger = ResolvedCaptureLandingTriggerV1(
        anchor_capture_contract_digest=envelope.capture_contract_digest,
        landing_filter_digest=_digest("landing-filter"),
        dispatch=_dispatch(),
    )
    coverage = read_landing_backlog_candidates(
        landing_journal,
        line_id=LINE_ID,
        occurrence_epoch=1,
        trigger=trigger,
    )
    assert coverage.coverage == "available"
    assert len(coverage.candidates) == 1
    occurrence, window_end = coverage.candidates[0]
    # The window closes when the anchor landed, not when a runner noticed it.
    assert window_end == event.landed_at
    assert occurrence.trigger_kind == "capture_landing"

    report = classify_occurrence_backlog(
        line_id=LINE_ID,
        occurrence_epoch=1,
        line_spec_digest=_digest("line-spec"),
        policy=_replayable_policy(max_age_microseconds=30 * MINUTE),
        occurrences=build_backlog_occurrences(coverage.candidates),
        head_vector=JournalHeadVectorV1(partitions=()),
        evaluation_time=landed_at + timedelta(hours=3),
    )
    assert report.lapsed_count == 1
    assert report.lapsed[0].reason_codes == (LAPSE_OUT_OF_AGE,)


def test_window_close_windows_and_unresolvable_cursors(tmp_path: Path) -> None:
    landing_journal = InMemoryCaptureLandingJournal()
    landed_at = ANCHOR + timedelta(hours=2)
    _landed(tmp_path, landing_journal, run_id="run-a", landed_at=landed_at)
    trigger = ResolvedWindowCloseTriggerV1(
        window_policy_digest=_digest("window-policy"),
        min_new_events=1,
        dispatch=_dispatch(),
    )
    coverage = read_window_close_backlog_candidates(
        landing_journal,
        line_id=LINE_ID,
        occurrence_epoch=1,
        trigger=trigger,
    )
    assert coverage.coverage == "available"
    assert len(coverage.candidates) == 1
    assert coverage.candidates[0][1] == landed_at

    class _UnreadableLanding:
        def events_after(self, cursor: str | None = None) -> tuple[CaptureLandingEventV1, ...]:
            raise CaptureJournalError("Capture cursor does not resolve in this journal")

        def vector_cursor(self):
            raise CaptureJournalError("Capture cursor does not resolve in this journal")

    unreadable = _UnreadableLanding()
    landing_coverage = read_landing_backlog_candidates(
        unreadable,
        line_id=LINE_ID,
        occurrence_epoch=1,
        trigger=ResolvedCaptureLandingTriggerV1(
            anchor_capture_contract_digest=_digest("anchor-capture"),
            landing_filter_digest=_digest("landing-filter"),
            dispatch=_dispatch(),
        ),
    )
    assert landing_coverage.coverage == "unavailable"
    assert landing_coverage.reason_codes == (COVERAGE_UNVERIFIED,)
    assert landing_coverage.candidates == ()

    window_coverage = read_window_close_backlog_candidates(
        unreadable,
        line_id=LINE_ID,
        occurrence_epoch=1,
        trigger=trigger,
    )
    assert window_coverage.coverage == "unavailable"
    assert window_coverage.reason_codes == (COVERAGE_UNVERIFIED,)


def test_a_regressed_window_cursor_is_unavailable_not_empty() -> None:
    class _RegressedLanding:
        def events_after(self, cursor: str | None = None) -> tuple[CaptureLandingEventV1, ...]:
            return (
                _landing_event(
                    partition="alpha",
                    sequence=0,
                    contract_digest=_digest("anchor-capture"),
                    producer_digest=_digest("producer-one"),
                ),
            )

        def vector_cursor(self):
            return ()

    coverage = read_window_close_backlog_candidates(
        _RegressedLanding(),
        line_id=LINE_ID,
        occurrence_epoch=1,
        trigger=ResolvedWindowCloseTriggerV1(
            window_policy_digest=_digest("window-policy"),
            min_new_events=1,
            dispatch=_dispatch(),
        ),
        from_cursors=_sorted_cursors(_cursor("alpha", 3, "alpha-3")),
    )
    assert coverage.coverage == "unavailable"
    assert coverage.reason_codes == (COVERAGE_UNVERIFIED,)


# ---------------------------------------------------------------------------
# The fenced lapse receipt
# ---------------------------------------------------------------------------


def _lapsed_occurrence() -> BacklogOccurrenceV1:
    return _backlog_occurrence(1)


def test_lapse_append_is_fenced_and_idempotent(tmp_path: Path) -> None:
    deployed = _deploy(tmp_path)
    policy = _unreplayable_policy()
    occurrence = _lapsed_occurrence()
    evaluation = ANCHOR + timedelta(hours=10)
    lapse = build_occurrence_lapse(occurrence, policy=policy, evaluation_time=evaluation)

    first = deployed.record_lapse(lapse, recorded_at=NOW)
    assert first.record.event_kind == "occurrence_lapsed"
    assert first.record.occurrence_id == occurrence.occurrence_digest
    assert first.record.attempt is None

    # A retry, even by a later runner at a later wall time, is one record.
    retry = deployed.record_lapse(lapse, recorded_at=NOW + timedelta(hours=4))
    assert retry == first
    assert len(read_line_lapse_records(deployed.journal, deployed.deployment)) == 1

    # A fenced writer cannot memorialize anything.
    stream = _stream()
    for _, partition_id in line_partition_pairs(deployed.deployment):
        deployed.journal.fence_writer(
            stream, partition_id, expected_fencing_token=deployed.lease.fencing_token
        )
    with pytest.raises(LineRuntimeRefusal, match="lease_fenced"):
        deployed.record_lapse(lapse, recorded_at=NOW + timedelta(hours=5))
    assert len(read_line_lapse_records(deployed.journal, deployed.deployment)) == 1
    deployed.index.close()


def test_one_lapse_identity_cannot_carry_two_payloads(tmp_path: Path) -> None:
    deployed = _deploy(tmp_path)
    occurrence = _lapsed_occurrence()
    policy = _unreplayable_policy()
    lapse = build_occurrence_lapse(
        occurrence,
        policy=policy,
        evaluation_time=ANCHOR + timedelta(hours=10),
    )
    deployed.record_lapse(lapse, recorded_at=NOW)

    forged = lapse.model_copy(update={"lapse_effective_at": ANCHOR + timedelta(hours=99)})
    assert occurrence_lapse_identity_digest(forged) == occurrence_lapse_identity_digest(lapse)
    with pytest.raises(LineRuntimeRefusal, match="lapse_payload_conflict"):
        deployed.record_lapse(forged, recorded_at=NOW + timedelta(hours=1))
    deployed.index.close()


def test_lapse_separates_effective_from_recorded_time_and_hides_the_runner(
    tmp_path: Path,
) -> None:
    deployed = _deploy(tmp_path)
    occurrence = _lapsed_occurrence()
    policy = _replayable_policy(max_age_microseconds=2 * 3_600_000_000)

    # Two runners noticing at wildly different times derive one effective time.
    early = build_occurrence_lapse(
        occurrence, policy=policy, evaluation_time=ANCHOR + timedelta(hours=5)
    )
    late = build_occurrence_lapse(
        occurrence, policy=policy, evaluation_time=ANCHOR + timedelta(days=30)
    )
    assert early == late
    assert early.lapse_effective_at == ANCHOR + timedelta(hours=4)

    recorded_at = NOW + timedelta(days=9)
    stored = deployed.record_lapse(early, recorded_at=recorded_at)
    assert stored.record.recorded_at == recorded_at
    payload = parse_journal_payload(
        deployed.bodies.read(
            stored.record.payload_digest,
            access=BodyAccessContext(principal_id="reader", can_read_body=True),
        )
    )
    assert isinstance(payload, dict)
    normative = payload["lapse"]
    assert isinstance(normative, dict)
    # Runner and backend identity live in the audit envelope only.
    assert "runner_id" not in normative
    assert "recorded_at" not in normative
    assert normative["lapse_effective_at"] != normative.get("recorded_at")
    audit = payload["audit"]
    assert isinstance(audit, dict)
    assert audit["runner_id"] == deployed.deployment.runner.runner_id
    assert audit["recorded_at"] != normative["lapse_effective_at"]
    # Identity is the occurrence coordinate plus the pinned policy digest alone.
    assert payload["lapse_identity_digest"] == occurrence_lapse_identity_digest(early)
    deployed.index.close()


def test_build_occurrence_lapse_refuses_an_unlapsed_window() -> None:
    with pytest.raises(LineRuntimeRefusal, match="lapse_not_effective"):
        build_occurrence_lapse(
            _lapsed_occurrence(),
            policy=_replayable_policy(),
            evaluation_time=ANCHOR + timedelta(days=400),
        )


def test_a_lapse_cannot_be_recorded_against_another_line_or_epoch(tmp_path: Path) -> None:
    deployed = _deploy(tmp_path)
    lapse = build_occurrence_lapse(
        _lapsed_occurrence(),
        policy=_unreplayable_policy(),
        evaluation_time=ANCHOR + timedelta(hours=10),
    )
    foreign = lapse.model_copy(update={"occurrence_epoch": 7})
    with pytest.raises(LineRuntimeRefusal, match="lapse_occurrence_foreign"):
        deployed.record_lapse(foreign, recorded_at=NOW)
    deployed.index.close()


# ---------------------------------------------------------------------------
# Lapse legality is receipt-independent
# ---------------------------------------------------------------------------


def test_post_lapse_original_window_refuses_with_no_writer_ever_online() -> None:
    occurrence = _lapsed_occurrence()
    policy = _unreplayable_policy()
    # No journal, no lease, no receipt: the refusal is derived, not remembered.
    with pytest.raises(LineRuntimeRefusal, match="occurrence_lapsed"):
        require_original_window_acquirable(
            occurrence,
            policy=policy,
            evaluation_time=ANCHOR + timedelta(hours=10),
        )

    in_age = _replayable_policy(max_age_microseconds=8 * 3_600_000_000)
    require_original_window_acquirable(
        occurrence,
        policy=in_age,
        evaluation_time=ANCHOR + timedelta(hours=5),
    )
    with pytest.raises(LineRuntimeRefusal, match="occurrence_lapsed"):
        require_original_window_acquirable(
            occurrence,
            policy=in_age,
            evaluation_time=ANCHOR + timedelta(hours=11),
        )


def test_classification_is_byte_identical_with_and_without_the_receipt(tmp_path: Path) -> None:
    deployed = _deploy(tmp_path)
    occurrence = _lapsed_occurrence()
    policy = _unreplayable_policy()
    evaluation = ANCHOR + timedelta(hours=10)
    deployed.scheduler.materialize(
        occurrence.occurrence,
        accepted_line=deployed.accepted_line,
        accepted_procedure=deployed.accepted_procedure,
        actor_context=_actor(),
        materialized_at=NOW,
    )

    def _report() -> LineOccurrenceBacklogV1:
        state = deployed.control_state()
        assert state.coverage == "available"
        return classify_occurrence_backlog(
            line_id=LINE_ID,
            occurrence_epoch=1,
            line_spec_digest=deployed.accepted_line.artifact_digest,
            policy=policy,
            occurrences=build_backlog_occurrences(
                ((occurrence.occurrence, occurrence.window_end),),
                attempts=state.attempt_state,
            ),
            # The head vector legitimately advances when the receipt lands, so
            # compare the classification itself, not the head it was taken at.
            head_vector=JournalHeadVectorV1(partitions=()),
            evaluation_time=evaluation,
        )

    without_receipt = _report()
    assert without_receipt.lapsed_count == 1
    assert read_line_lapse_records(deployed.journal, deployed.deployment) == ()

    deployed.record_lapse(
        build_occurrence_lapse(occurrence, policy=policy, evaluation_time=evaluation),
        recorded_at=NOW + timedelta(hours=1),
    )
    assert len(read_line_lapse_records(deployed.journal, deployed.deployment)) == 1

    with_receipt = _report()
    assert _wire(with_receipt) == _wire(without_receipt)
    deployed.index.close()
