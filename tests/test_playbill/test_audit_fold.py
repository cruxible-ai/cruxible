"""Exact G9 audit ranking, coverage accounting, and purity laws."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.captures import CanonicalDurationV1
from cruxible_client.contracts.claim_attestations import (
    ClaimAttestationStatement,
    VerifiedClaimAttestationV1,
)
from cruxible_client.contracts.claim_verdicts import CaptureVerdictEvidenceV1
from cruxible_client.contracts.claims import (
    AcceptedClaim,
    ClaimLawEvidenceV1,
    claim_artifact_digest,
    claim_statement_digest,
    render_claim,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.semantic import SemanticAddress
from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.audit import (
    AuditBudgetV1,
    AuditDependentRefV1,
    AuditScopeV1,
    audit_row_order,
    build_reverse_dependency_index,
)
from cruxible_core.playbill.consumption import (
    ConsumptionContextV1,
    consumption_aggregate,
    record_consumption,
)
from cruxible_core.playbill.coverage.contracts import (
    CoverageAccessProfileV1,
    LogicalSourceIdentityV1,
)
from cruxible_core.playbill.query.backends import ClaimQueryFactsV1
from cruxible_core.playbill.query.impact import (
    DependencyImpactRequestV1,
    build_dependency_impact,
)
from cruxible_core.service.playbill_audit import (
    PlaybillAuditCursorInvalid,
    PlaybillAuditRequestV1,
    _audit_consumption_touch_counts,
    _AuditHistoryIndex,
    _history_index,
    _logical_source_keys,
    _row,
    completed_audit_runs,
    service_playbill_audit,
)
from cruxible_core.service.playbill_query import build_accepted_query_facts
from tests.test_playbill._knowledge_loop_support import seed_claims
from tests.test_playbill._modeling_parity_support import claim_fact, facts, subject
from tests.test_playbill._support import initialize_local

NOW = datetime(2026, 8, 26, 18, 0, tzinfo=UTC)
PREDICATE = "project.work_item.status"


def _actor() -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="human_user",
        actor_id="auditor",
        org_id="org-test",
        operation_id="op-audit",
        timestamp=NOW,
    )


def _request(*, permitted: bool = True) -> PlaybillAuditRequestV1:
    return PlaybillAuditRequestV1(
        evaluation_time=NOW,
        access_profile=CoverageAccessProfileV1(
            profile_id="test-audit",
            permitted_access_classes=("instance", "public") if permitted else ("public",),
        ),
    )


def test_empty_audit_is_byte_identical_idempotent_and_operational_only(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    accepted_before = instance.accepted_coordinate()
    first = service_playbill_audit(instance, request=_request(), actor_context=_actor())
    second = service_playbill_audit(instance, request=_request(), actor_context=_actor())

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert first.coverage.access_permitted
    assert first.rows == ()
    assert first.audited_through_generation == 0
    assert len(completed_audit_runs(instance)) == 1
    assert instance.accepted_coordinate() == accepted_before
    assert consumption_aggregate(instance).artifacts == ()
    assert instance.review_operational_store().events(family="consumption") == ()


def test_access_gate_precedes_counts_and_writes(tmp_path: Path) -> None:
    instance, _owner = initialize_local(tmp_path)
    result = service_playbill_audit(
        instance,
        request=_request(permitted=False),
        actor_context=_actor(),
    )

    assert not result.coverage.access_permitted
    assert result.coverage.candidate_claim_count == 0
    assert result.coverage.covered_claims == ()
    assert result.audited_through_generation is None
    assert instance.review_operational_store().events(family="audit") == ()


def test_pagination_commits_actual_coverage_and_cursor_rejects_operational_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    subjects = tuple(subject("project.work_item", f"wi-{index}") for index in range(3))
    claims = tuple(
        claim_fact(
            index + 10,
            subject_row=subjects[index],
            predicate=PREDICATE,
            value="ready",
        )
        for index in range(3)
    )
    projected = ClaimQueryFactsV1(
        coordinate=instance.accepted_coordinate(),
        subjects=tuple(sorted(subjects, key=lambda item: item.path.encode("utf-8"))),
        claims=tuple(sorted(claims, key=lambda item: item.accepted.path.encode("utf-8"))),
    )
    monkeypatch.setattr(
        "cruxible_core.service.playbill_audit.build_accepted_query_facts",
        lambda *_args, **_kwargs: projected,
    )
    first_request = PlaybillAuditRequestV1(
        evaluation_time=NOW,
        access_profile=CoverageAccessProfileV1(profile_id="test-audit-pages"),
        budget=AuditBudgetV1(max_rows=2, max_bytes=65_536),
    )
    first = service_playbill_audit(instance, request=first_request, actor_context=_actor())

    assert len(first.rows) == 2
    assert first.coverage.candidate_claim_count == 3
    assert first.coverage.returned_claim_count == 2
    assert first.coverage.omitted_claim_count == 1
    assert first.coverage.omission_reasons == ("row_budget_exceeded",)
    assert len(first.coverage.covered_claims) == 3
    assert first.next_cursor is not None
    omitted = next(
        row.accepted
        for row in claims
        if row.accepted.claim.identity not in {item.claim_identity for item in first.rows}
    )
    record_consumption(
        instance,
        context=ConsumptionContextV1(
            actor_context=_actor(),
            access_profile_id="test-audit-pages",
        ),
        operation="playbill.claim.get",
        coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        artifacts=((omitted.claim.identity, omitted.artifact_digest),),
    )
    with pytest.raises(PlaybillAuditCursorInvalid):
        service_playbill_audit(
            instance,
            request=first_request.model_copy(update={"cursor": first.next_cursor}),
            actor_context=_actor(),
        )

    # A fresh patrol at the new fold-start head delivers every row exactly once.
    restarted = service_playbill_audit(instance, request=first_request, actor_context=_actor())
    assert restarted.next_cursor is not None
    second = service_playbill_audit(
        instance,
        request=first_request.model_copy(update={"cursor": restarted.next_cursor}),
        actor_context=_actor(),
    )
    assert len(second.rows) == 1
    assert second.next_cursor is None
    delivered = (*restarted.rows, *second.rows)
    assert len({row.claim_identity for row in delivered}) == 3
    assert {row.claim_identity for row in delivered} == {
        row.accepted.claim.identity for row in claims
    }
    assert len(completed_audit_runs(instance)) == 3
    retry = service_playbill_audit(
        instance,
        request=first_request.model_copy(update={"cursor": restarted.next_cursor}),
        actor_context=_actor(),
    )
    assert retry.model_dump(mode="json") == second.model_dump(mode="json")
    assert len(completed_audit_runs(instance)) == 3

    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    instance.review_operational_store().append(
        family="block_observation",
        partition_id="audit-test-drift",
        event_id="audit-test-drift",
        payload={"tag": "audit-test-operational-drift-v1", "event_id": "audit-test-drift"},
        coordinate=coordinate,
        generation=0,
        actor_context=_actor(),
        recorded_at=NOW,
    )
    with pytest.raises(PlaybillAuditCursorInvalid):
        service_playbill_audit(
            instance,
            request=first_request.model_copy(update={"cursor": restarted.next_cursor}),
            actor_context=_actor(),
        )


def test_failed_fold_writes_no_completed_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _owner = initialize_local(tmp_path)

    def fail(_instance):  # type: ignore[no-untyped-def]
        raise RuntimeError("rank fold failed")

    monkeypatch.setattr(
        "cruxible_core.service.playbill_audit._audit_consumption_touch_counts", fail
    )
    with pytest.raises(RuntimeError, match="rank fold failed"):
        service_playbill_audit(instance, request=_request(), actor_context=_actor())
    assert completed_audit_runs(instance) == ()


def test_audit_caps_self_heating_without_changing_receipts_or_other_consumers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _owner = seed_claims(tmp_path)
    facts_at_head = build_accepted_query_facts(
        instance,
        coordinate=instance.accepted_coordinate(),
    )
    claim = facts_at_head.claims[0].accepted
    artifact = (claim.claim.identity, claim.artifact_digest)
    history = instance.accepted_history()
    first_generation = history[1]
    first_coordinate = AcceptedCoordinate(
        git_oid=first_generation.oid,
        semantic_root=first_generation.semantic_root.tagged,
        generation_root=first_generation.generation_root.tagged,
        compiler_digest=instance.descriptor.compiler.rule_digest,
    )
    head_coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())

    def context(reader: str, profile: str) -> ConsumptionContextV1:
        return ConsumptionContextV1(
            actor_context=GovernedActorContext(
                actor_type="service_account",
                actor_id=reader,
                org_id="org-test",
                operation_id=f"op-{reader}-{profile}",
                timestamp=NOW,
            ),
            access_profile_id=profile,
        )

    # The same reader at the historical coordinate is one touch.  At the head,
    # operation and profile multiplicity collapse to one separate touch.
    record_consumption(
        instance,
        context=context("reader-a", "instance"),
        operation="playbill.claim.get",
        coordinate=first_coordinate,
        artifacts=(artifact,),
    )
    head_first = record_consumption(
        instance,
        context=context("reader-a", "instance"),
        operation="playbill.claim.get",
        coordinate=head_coordinate,
        artifacts=(artifact,),
    )
    head_retry = record_consumption(
        instance,
        context=context("reader-a", "instance"),
        operation="playbill.claim.get",
        coordinate=head_coordinate,
        artifacts=(artifact,),
    )
    record_consumption(
        instance,
        context=context("reader-a", "instance"),
        operation="playbill.search.match",
        coordinate=head_coordinate,
        artifacts=(artifact,),
    )
    record_consumption(
        instance,
        context=context("reader-a", "alternate-instance-profile"),
        operation="playbill.search.match",
        coordinate=head_coordinate,
        artifacts=(artifact,),
    )
    # A different reader and a successor digest each contribute one.
    record_consumption(
        instance,
        context=context("reader-b", "instance"),
        operation="playbill.claim.get",
        coordinate=head_coordinate,
        artifacts=(artifact,),
    )
    successor_digest = typed_digest(
        Sha256Value,
        "playbill-audit-successor-fixture-v1",
        {"predecessor": claim.artifact_digest},
    ).tagged
    record_consumption(
        instance,
        context=context("reader-a", "instance"),
        operation="playbill.claim.get",
        coordinate=head_coordinate,
        artifacts=((claim.claim.identity, successor_digest),),
    )

    assert head_first == head_retry
    raw_before = instance.review_operational_store().events(family="consumption")
    aggregate_before = consumption_aggregate(instance)
    assert aggregate_before.artifacts[0].qualifying_touch_count == 6
    assert _audit_consumption_touch_counts(instance) == {claim.claim.identity.qualified: 4}

    store = instance.review_operational_store()
    original_events = store.events
    consumption_reads = 0

    def counted_events(*, family=None):  # type: ignore[no-untyped-def]
        nonlocal consumption_reads
        if family == "consumption":
            consumption_reads += 1
        return original_events(family=family)

    monkeypatch.setattr(instance, "review_operational_store", lambda: store)
    monkeypatch.setattr(store, "events", counted_events)
    accepted_before = instance.accepted_coordinate()
    first = service_playbill_audit(instance, request=_request(), actor_context=_actor())
    second = service_playbill_audit(instance, request=_request(), actor_context=_actor())

    consumption_refs = [
        ref
        for row in first.rows
        for ref in row.evidence_refs
        if ref.kind == "consumption_aggregate"
    ]
    assert consumption_refs, "audit rows must carry the consumption evidence ref"
    for ref in consumption_refs:
        assert ref.facts["fold"] == "audit_reader_capped_v1"

    row = next(item for item in first.rows if item.claim_identity == claim.claim.identity)
    assert row.factors.qualifying_consumption_touch_count == 4
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert consumption_reads == 2  # exactly once per complete audit fold
    assert instance.accepted_coordinate() == accepted_before
    assert store.events(family="consumption") == raw_before
    assert consumption_aggregate(instance) == aggregate_before
    assert store.events(family="curation") == ()
    assert len(completed_audit_runs(instance)) == 1


def test_byte_budget_records_exact_omission_without_skipping_the_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _owner = initialize_local(tmp_path)
    subject_row = subject("project.work_item", "wi-byte-budget")
    claim_row = claim_fact(
        9,
        subject_row=subject_row,
        predicate=PREDICATE,
        value="ready",
    )
    projected = ClaimQueryFactsV1(
        coordinate=instance.accepted_coordinate(),
        subjects=(subject_row,),
        claims=(claim_row,),
    )
    monkeypatch.setattr(
        "cruxible_core.service.playbill_audit.build_accepted_query_facts",
        lambda *_args, **_kwargs: projected,
    )

    result = service_playbill_audit(
        instance,
        request=PlaybillAuditRequestV1(
            evaluation_time=NOW,
            access_profile=CoverageAccessProfileV1(profile_id="test-audit-bytes"),
            budget=AuditBudgetV1(max_rows=10, max_bytes=1_024),
        ),
        actor_context=_actor(),
    )

    assert result.rows == ()
    assert result.coverage.candidate_claim_count == 1
    assert result.coverage.returned_claim_count == 0
    assert result.coverage.omitted_claim_count == 1
    assert result.coverage.omission_reasons == ("byte_budget_exceeded",)
    assert result.next_cursor is None
    assert len(completed_audit_runs(instance)) == 1


def test_audit_scope_and_budget_are_closed_and_canonical() -> None:
    assert (
        AuditScopeV1(
            claim_type_identities=("ClaimType:project.work_item.status",),
            subject_kinds=("project.work_item",),
        ).tag
        == "playbill-audit-scope-v1"
    )
    with pytest.raises(ValueError, match="byte-sorted"):
        AuditScopeV1(subject_kinds=("z", "a"))
    with pytest.raises(ValueError):
        AuditBudgetV1(max_rows=0)


def test_rank_order_is_score_then_every_integer_factor_then_claim_path() -> None:
    class Row:
        def __init__(self, score: int, stake: int, weakness: int, stale: int, path: str) -> None:
            self.rank_score = score
            self.factors = type(
                "Factors",
                (),
                {"stake": stake, "weakness": weakness, "staleness": stale},
            )()
            self.claim_path = path

    rows = (
        Row(20, 2, 5, 2, "claims/z.yaml"),
        Row(20, 4, 1, 5, "claims/a.yaml"),
        Row(20, 4, 1, 5, "claims/b.yaml"),
        Row(21, 1, 1, 21, "claims/c.yaml"),
    )
    ordered = sorted(rows, key=audit_row_order)  # type: ignore[arg-type]
    assert [item.claim_path for item in ordered] == [
        "claims/c.yaml",
        "claims/a.yaml",
        "claims/b.yaml",
        "claims/z.yaml",
    ]


def _capture(
    suffix: str,
    *,
    observed_at: datetime = NOW - timedelta(hours=1),
    control_domain: str = "shared-owner",
    provenance_grade: str = "self-asserted",
) -> CaptureVerdictEvidenceV1:
    return CaptureVerdictEvidenceV1(
        capture_digest=typed_digest(
            Sha256Value,
            "playbill-audit-test-capture-v1",
            {"suffix": suffix},
        ).tagged,
        admission="direct",
        basis_kind="replay_verified",
        producer=ArtifactIdentity(kind="Provider", name=f"provider-{suffix}"),
        control_domain=control_domain,
        epistemic_grade="observed",
        provenance_grade=provenance_grade,  # type: ignore[arg-type]
        observed_at=observed_at,
        current_replay_available=True,
    )


def _history_for(row, *, first: int = 2, verification: int | None = None):  # type: ignore[no-untyped-def]
    key = (row.accepted.path, row.accepted.statement_digest)
    return _AuditHistoryIndex(
        claim_lineages={row.accepted.path: (row.accepted.artifact_digest,)},
        first_statement_generation={key: first},
        lineage_creation_actor={key: "owner"},
        attestation_first_generation=(
            {}
            if verification is None
            else {
                (
                    row.accepted.path,
                    row.accepted.statement_digest,
                    row.attestations[0].attestation_digest,
                ): verification
            }
        ),
    )


def test_factor_fold_exposes_raw_counts_flags_and_exact_integer_score(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject_row = subject("project.work_item", "wi-42")
    base = claim_fact(1, subject_row=subject_row, predicate=PREDICATE, value="ready")
    captures = (_capture("a"), _capture("b"))
    row = base.model_copy(update={"captures": captures})
    monkeypatch.setattr(
        "cruxible_core.service.playbill_audit._logical_source_keys",
        lambda _instance, items: {item.capture_digest: "db.work_items" for item in items},
    )
    dependents = (
        AuditDependentRefV1(
            kind="Claim",
            identity=ArtifactIdentity(kind="Claim", name="CLM-dependent"),
            path="claims/CLM-dependent.yaml",
        ),
        AuditDependentRefV1(
            kind="Procedure",
            identity=ArtifactIdentity(kind="Procedure", name="triage"),
            path="procedures/triage.yaml",
        ),
    )

    result = _row(
        SimpleNamespace(),  # type: ignore[arg-type]
        row=row,
        subject_identity=subject_row.shell.identity,
        generation=7,
        evaluation_time=NOW,
        providers={},
        dependents=dependents,
        qualifying_consumption_touch_count=3,
        history=_history_for(row),
    )

    assert result.factors.unique_dependent_count == 2
    assert result.factors.qualifying_consumption_touch_count == 3
    assert result.factors.stake == 6
    assert result.factors.single_source
    assert result.factors.proposer_observed_only
    assert result.factors.zero_corroboration
    assert not result.factors.near_freshness_horizon
    assert result.factors.weakness == 4
    assert result.factors.never_verified
    assert result.factors.staleness == 6
    assert result.rank_score == 144
    assert "recommend" not in str(result.model_dump(mode="json")).lower()


def test_logical_source_factor_uses_accepted_identity_and_does_not_invent_one_for_cas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    external = _capture("external")
    ledger = _capture("ledger")
    cas = _capture("cas")
    sources = {
        external.capture_digest: LogicalSourceIdentityV1(
            plane="external", identity="db.work_items"
        ),
        ledger.capture_digest: LogicalSourceIdentityV1(
            plane="ledger", identity="documents/runbook.md"
        ),
        cas.capture_digest: None,
    }
    store = SimpleNamespace(read=lambda digest, *, access: digest)  # noqa: ARG005
    instance = SimpleNamespace(body_store=lambda: store)
    monkeypatch.setattr(
        "cruxible_core.service.playbill_audit.parse_capture_envelope",
        lambda raw: SimpleNamespace(source=raw),
    )
    monkeypatch.setattr(
        "cruxible_core.service.playbill_audit.accepted_logical_source",
        lambda source: sources[source],
    )

    assert _logical_source_keys(instance, (external, ledger, cas)) == {
        external.capture_digest: "external:db.work_items",
        ledger.capture_digest: "ledger:documents/runbook.md",
        cas.capture_digest: None,
    }


def test_near_horizon_is_one_quarter_of_the_exact_v2_expiration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject_row = subject("project.work_item", "wi-42")
    base = claim_fact(2, subject_row=subject_row, predicate=PREDICATE, value="ready")
    capture = _capture("near", observed_at=NOW - timedelta(microseconds=350))
    row = base.model_copy(
        update={
            "captures": (capture,),
            "rule": base.rule.model_copy(
                update={"max_evidence_age": CanonicalDurationV1(microseconds=400)}
            ),
        }
    )
    monkeypatch.setattr(
        "cruxible_core.service.playbill_audit._logical_source_keys",
        lambda _instance, items: {item.capture_digest: "db.work_items" for item in items},
    )

    result = _row(
        SimpleNamespace(),  # type: ignore[arg-type]
        row=row,
        subject_identity=subject_row.shell.identity,
        generation=2,
        evaluation_time=NOW,
        providers={},
        dependents=(),
        qualifying_consumption_touch_count=0,
        history=_history_for(row),
    )

    assert result.factors.near_freshness_horizon


def _support_attestation(row, capture: CaptureVerdictEvidenceV1):  # type: ignore[no-untyped-def]
    return VerifiedClaimAttestationV1(
        attestation_digest=typed_digest(
            Sha256Value, "playbill-audit-test-attestation-v1", {"value": "reviewer"}
        ).tagged,
        statement=ClaimAttestationStatement(
            instance_id="inst-audit",
            referent_coordinate=AcceptedCoordinate(
                git_oid="1" * 64,
                semantic_root="sha256:" + "2" * 64,
                generation_root="sha256:" + "3" * 64,
                compiler_digest="sha256:" + "4" * 64,
            ),
            subject=row.accepted.claim.statement.subject,
            subject_content_digest=row.accepted.claim.backing.referent_context.subject_content_digest,
            claim_statement_digest=row.accepted.statement_digest,
            stance="support",
            provider_or_principal=ArtifactIdentity(kind="Principal", name="reviewer"),
            signing_key_id="reviewer-key",
            capture_digests=(capture.capture_digest,),
            observed_at=NOW - timedelta(minutes=5),
        ),
        attestation_grade="verified_principal",
        control_domain="reviewer",
        coverage="exact_subject",
        current=True,
    )


def test_independent_verification_advances_recency_from_the_statement_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject_row = subject("project.work_item", "wi-42")
    base = claim_fact(3, subject_row=subject_row, predicate=PREDICATE, value="ready")
    capture = _capture("support", control_domain="source")
    attestation = _support_attestation(base, capture)
    row = base.model_copy(update={"captures": (capture,), "attestations": (attestation,)})
    monkeypatch.setattr(
        "cruxible_core.service.playbill_audit._logical_source_keys",
        lambda _instance, items: {item.capture_digest: "db.work_items" for item in items},
    )

    result = _row(
        SimpleNamespace(),  # type: ignore[arg-type]
        row=row,
        subject_identity=subject_row.shell.identity,
        generation=7,
        evaluation_time=NOW,
        providers={},
        dependents=(),
        qualifying_consumption_touch_count=0,
        history=_history_for(row, first=2, verification=5),
    )

    assert not result.factors.never_verified
    assert result.factors.last_independent_verification_generation == 5
    assert result.factors.staleness == 3
    assert not result.factors.proposer_observed_only


def _law(row, *, attestations=()):  # type: ignore[no-untyped-def]
    return ClaimLawEvidenceV1(
        law_digest="sha256:" + "1" * 64,
        adjudication_rule_digest="sha256:" + "2" * 64,
        statement_digest=row.accepted.statement_digest,
        artifact_digest=row.accepted.artifact_digest,
        initial_verdict="supported",
        evidence_basis=("direct",),
        verified_attestations=attestations,
        verified_attestation_digests=tuple(item.attestation_digest for item in attestations),
    )


def _generation(sequence: int, oid: str, row, *, actor: str, attestations=()):  # type: ignore[no-untyped-def]
    member = SimpleNamespace(
        path=row.accepted.path,
        result={"claim_evidence": _law(row, attestations=attestations).model_dump(mode="json")},
    )
    return SimpleNamespace(
        sequence=sequence,
        oid=oid,
        record=SimpleNamespace(
            actor_binding=SimpleNamespace(actor_id=actor),
            law_evidence=(member,),
        ),
    )


def test_history_index_carries_backing_only_verification_and_resets_on_statement_change() -> None:
    subject_row = subject("project.work_item", "wi-42")
    first = claim_fact(4, subject_row=subject_row, predicate=PREDICATE, value="ready")
    capture = _capture("history")
    attestation = _support_attestation(first, capture)
    backing_revision = first.model_copy(update={"attestations": (attestation,)})
    changed = claim_fact(4, subject_row=subject_row, predicate=PREDICATE, value="blocked")
    histories = (
        _generation(1, "one", first, actor="owner"),
        _generation(2, "two", backing_revision, actor="reviewer", attestations=(attestation,)),
        _generation(3, "three", changed, actor="owner-two"),
    )
    trees = {
        "one": {first.accepted.path: render_claim(first.accepted.claim)},
        "two": {backing_revision.accepted.path: render_claim(backing_revision.accepted.claim)},
        "three": {changed.accepted.path: render_claim(changed.accepted.claim)},
    }
    fake = SimpleNamespace(
        accepted_history=lambda: histories,
        tree_at=lambda oid: trees[oid],
    )

    carried = _history_index(
        fake,  # type: ignore[arg-type]
        current_claims={backing_revision.accepted.path: backing_revision},
        target_generation=2,
    )
    reset = _history_index(
        fake,  # type: ignore[arg-type]
        current_claims={changed.accepted.path: changed},
        target_generation=3,
    )

    carried_key = (backing_revision.accepted.path, backing_revision.accepted.statement_digest)
    reset_key = (changed.accepted.path, changed.accepted.statement_digest)
    assert carried.first_statement_generation[carried_key] == 1
    assert carried.attestation_first_generation[(*carried_key, attestation.attestation_digest)] == 2
    assert carried.lineage_creation_actor[carried_key] == "owner"
    assert reset.first_statement_generation[reset_key] == 3
    assert reset.lineage_creation_actor[reset_key] == "owner-two"
    assert not any(key[:2] == reset_key for key in reset.attestation_first_generation)


def test_reverse_index_matches_dependency_impact_and_builds_tree_index_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cruxible_core.playbill.audit as audit_module

    subject_row = subject("project.work_item", "wi-42")
    source = claim_fact(5, subject_row=subject_row, predicate=PREDICATE, value="ready")
    dependent = claim_fact(6, subject_row=subject_row, predicate=PREDICATE, value="blocked")
    backing = dependent.accepted.claim.backing.model_copy(
        update={
            "input_claim_digests": (source.accepted.artifact_digest,),
            "reducer_digest": "sha256:" + "9" * 64,
        }
    )
    dependent_claim = dependent.accepted.claim.model_copy(update={"backing": backing})
    dependent = dependent.model_copy(
        update={
            "accepted": AcceptedClaim(
                path=dependent.accepted.path,
                claim=dependent_claim,
                statement_digest=claim_statement_digest(dependent_claim.statement).tagged,
                artifact_digest=claim_artifact_digest(dependent_claim).tagged,
            )
        }
    )
    fact_set = facts("audit-impact", (subject_row,), (source, dependent))
    tree = {
        source.accepted.path: render_claim(source.accepted.claim),
        dependent.accepted.path: render_claim(dependent.accepted.claim),
    }
    calls = 0
    original = audit_module.dependency_artifacts

    def counted(value):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return original(value)

    monkeypatch.setattr(audit_module, "dependency_artifacts", counted)
    lineages = {source.accepted.path: (source.accepted.artifact_digest,)}
    index = build_reverse_dependency_index(
        tree=tree,
        facts=fact_set,
        claim_lineages=lineages,
    )
    impact = build_dependency_impact(
        DependencyImpactRequestV1(
            at=AcceptedCoordinate.from_internal(fact_set.coordinate),
            address=SemanticAddress.claim_statement(source.accepted.path),
            evaluation_time=NOW,
        ),
        facts=fact_set,
        source_lineages=lineages,
    )

    assert calls == 1
    assert {item.identity.qualified for item in index[source.accepted.path]} == {
        item.identity for item in impact.dependents
    }
