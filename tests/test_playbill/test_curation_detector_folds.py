"""Threshold and identity regressions for the mechanical curation folds."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast, get_args

import pytest

from cruxible_client.contracts.artifacts import ArtifactIdentity, ArtifactPin
from cruxible_client.contracts.canonical import Sha256Value, typed_digest
from cruxible_client.contracts.captures import (
    CaptureEnvelopeV1,
    CaptureRunCoordinateV1,
    capture_contract_digest,
    capture_contract_path,
    render_capture_contract,
    render_capture_envelope,
)
from cruxible_client.contracts.claim_types import (
    ClaimEvidenceFreshnessV1,
    ClaimFreshnessDurationV1,
    claim_type_digest,
    claim_type_path,
    render_claim_type,
)
from cruxible_client.contracts.claim_verdicts import CaptureVerdictEvidenceV1
from cruxible_client.contracts.claims import (
    AcceptedClaim,
    claim_artifact_digest,
    claim_statement_digest,
    render_claim,
)
from cruxible_client.contracts.projection import AcceptedCoordinate
from cruxible_client.contracts.source_references import (
    EvidenceCommitmentV1,
    ExternalSourceReferenceV1,
)
from cruxible_client.contracts.subjects import render_subject
from cruxible_core.playbill.consumption import ConsumptionAggregateV1
from cruxible_core.playbill.curation import (
    CURATION_PATTERN_KINDS,
    CurationCoverageOmissionReason,
    CurationDetectorCoverageV1,
    CurationEvidenceKind,
    CurationPatternKind,
    _validate_pattern_detail,
)
from cruxible_core.playbill.curation_detectors import (
    _active_writing_principal_count,
    _curation_history_index,
    _CurationHistoryIndex,
    _dead_vocabulary,
    _duplicate_statements,
    _freshness_calibration,
    _literal_subject_references,
    _provenance_concentration,
    _qualifier_crystallization,
    _recurring_conflicts,
)
from tests.test_playbill._modeling_parity_support import claim_fact, claim_type, subject
from tests.test_playbill._pc_c_support import capture_contract, digest, provider

NOW = datetime(2026, 8, 16, 12, tzinfo=UTC)
PREDICATE = "project.work_item.status"


def test_curation_detector_vocabularies_are_closed_and_enumerated() -> None:
    assert set(get_args(CurationPatternKind)) == set(CURATION_PATTERN_KINDS)
    assert set(get_args(CurationEvidenceKind)) == {
        "accepted_artifact",
        "accepted_member",
        "authoring_attempt",
        "block_observation",
        "capture_transition",
        "consumption_aggregate",
        "control_component",
        "proposal_attempt",
        "slot",
    }
    assert set(get_args(CurationCoverageOmissionReason)) == {
        "admission_record_missing",
        "admission_subject_unresolved",
        "admission_tree_unavailable",
        "block_document_association_unavailable",
        "block_observation_invalid",
        "capture_contract_identity_unresolved",
        "consumption_epoch_uninitialized",
        "drift_series_unavailable",
    }


def test_unknown_curation_pattern_kind_has_no_detail_fallthrough() -> None:
    with pytest.raises(ValueError, match="unknown curation pattern kind"):
        _validate_pattern_detail(cast(CurationPatternKind, "future-pattern"), {})


def _with_qualifier(row, qualifier: str):  # type: ignore[no-untyped-def]
    statement = row.accepted.claim.statement.model_copy(update={"qualifier": qualifier})
    claim = row.accepted.claim.model_copy(update={"statement": statement})
    return row.model_copy(
        update={
            "accepted": AcceptedClaim(
                path=row.accepted.path,
                claim=claim,
                statement_digest=claim_statement_digest(statement).tagged,
                artifact_digest=claim_artifact_digest(claim).tagged,
            )
        }
    )


def _capture(index: int, control_domain: str) -> CaptureVerdictEvidenceV1:
    return CaptureVerdictEvidenceV1(
        capture_digest=typed_digest(
            Sha256Value,
            "playbill-test-capture-v1",
            {"index": index},
        ).tagged,
        admission="direct",
        basis_kind="replay_verified",
        producer=ArtifactIdentity(kind="Provider", name=f"provider-{index}"),
        control_domain=control_domain,
        epistemic_grade="observed",
        provenance_grade="provider-signed",
        observed_at=NOW,
        current_replay_available=True,
    )


def test_one_cardinality_conflict_is_type_level_and_same_value_duplicates_are_not_conflict() -> (
    None
):
    subject_row = subject("project.work_item", "wi-42")
    contract = claim_type(PREDICATE, subject_kinds=("project.work_item",))
    tree = {claim_type_path(PREDICATE): render_claim_type(contract)}
    ready = claim_fact(1, subject_row=subject_row, predicate=PREDICATE, value="ready")
    blocked = claim_fact(2, subject_row=subject_row, predicate=PREDICATE, value="blocked")
    same = claim_fact(3, subject_row=subject_row, predicate=PREDICATE, value="ready")

    detected, coverage = _recurring_conflicts(tree=tree, rows=(ready, blocked), generation=4)
    not_detected, _ = _recurring_conflicts(tree=tree, rows=(ready, same), generation=4)

    assert coverage.evaluated_fact_count == 1
    assert len(detected) == 1
    assert detected[0].subject == contract.identity
    assert detected[0].detail == {
        "cardinality": "one",
        "slot_partition": "subject+predicate+qualifier",
    }
    assert not_detected == ()


def test_recurring_conflict_fold_uses_the_published_slot_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = claim_type(PREDICATE, subject_kinds=("project.work_item",))
    tree = {claim_type_path(PREDICATE): render_claim_type(contract)}
    first_subject = subject("project.work_item", "wi-42")
    first_slot = (
        claim_fact(1, subject_row=first_subject, predicate=PREDICATE, value="ready"),
        claim_fact(2, subject_row=first_subject, predicate=PREDICATE, value="blocked"),
    )
    second_subject = subject("project.work_item", "wi-43")
    second_slot = (
        claim_fact(3, subject_row=second_subject, predicate=PREDICATE, value="ready"),
        claim_fact(4, subject_row=second_subject, predicate=PREDICATE, value="blocked"),
    )
    monkeypatch.setattr(
        "cruxible_core.playbill.curation_detectors.RECURRING_CONFLICT_MINIMUM_UNRESOLVED_SLOTS",
        2,
    )

    below, _ = _recurring_conflicts(tree=tree, rows=first_slot, generation=4)
    at_threshold, _ = _recurring_conflicts(
        tree=tree,
        rows=(*first_slot, *second_slot),
        generation=4,
    )

    assert below == ()
    assert len(at_threshold) == 1
    assert len(tuple(ref for ref in at_threshold[0].evidence_refs if ref.kind == "slot")) == 2


def test_qualifier_crystallization_counts_distinct_subject_addresses_only() -> None:
    rows = tuple(
        _with_qualifier(
            claim_fact(
                index,
                subject_row=subject("project.work_item", f"wi-{index}"),
                predicate=PREDICATE,
                value="ready",
            ),
            "as-of-review",
        )
        for index in (1, 2, 3)
    )
    duplicate_subject = _with_qualifier(
        claim_fact(
            4,
            subject_row=subject("project.work_item", "wi-1"),
            predicate=PREDICATE,
            value="blocked",
        ),
        "as-of-review",
    )

    detected, _coverage = _qualifier_crystallization(rows=(*rows, duplicate_subject), generation=9)

    assert len(detected) == 1
    assert detected[0].detail == {"qualifier": "as-of-review"}
    assert len({ref.facts["subject"]["artifact_path"] for ref in detected[0].evidence_refs}) == 3  # type: ignore[index]


def test_duplicate_statement_fold_counts_only_simultaneously_live_lineages() -> None:
    subject_row = subject("project.work_item", "wi-42")
    first = claim_fact(1, subject_row=subject_row, predicate=PREDICATE, value="ready")
    second = claim_fact(2, subject_row=subject_row, predicate=PREDICATE, value="ready")
    tree = {
        first.accepted.path: render_claim(first.accepted.claim),
        second.accepted.path: render_claim(second.accepted.claim),
    }
    detected, coverage = _duplicate_statements(tree=tree, generation=2)

    assert coverage.evaluated_fact_count == 2
    assert len(detected) == 1
    assert len(detected[0].evidence_refs) == 2
    assert {ref.generation for ref in detected[0].evidence_refs} == {2}
    assert detected[0].detail == {"statement_digest": first.accepted.statement_digest}


def test_duplicate_statement_fold_exempts_retired_predecessor_and_live_successor() -> None:
    subject_row = subject("project.work_item", "wi-42")
    predecessor = claim_fact(1, subject_row=subject_row, predicate=PREDICATE, value="ready")
    successor = claim_fact(2, subject_row=subject_row, predicate=PREDICATE, value="ready")
    retired = predecessor.accepted.claim.model_copy(
        update={
            "lifecycle": predecessor.accepted.claim.lifecycle.model_copy(
                update={"state": "retired"}
            )
        }
    )
    tree = {
        predecessor.accepted.path: render_claim(retired),
        successor.accepted.path: render_claim(successor.accepted.claim),
    }

    detected, coverage = _duplicate_statements(tree=tree, generation=2)

    assert coverage.evaluated_fact_count == 1
    assert detected == ()


def test_literal_subject_reference_matches_exact_live_ids_and_lists_every_kind() -> None:
    claim_subject = subject("project.work_item", "wi-42")
    package = subject("package", "demo-package")
    component = subject("component", "demo-package")
    row = claim_fact(
        1,
        subject_row=claim_subject,
        predicate="sec.vuln.affects_package",
        value="demo-package",
    )
    tree = {
        row.accepted.path: render_claim(row.accepted.claim),
        package.path: render_subject(package.shell),
        component.path: render_subject(component.shell),
    }

    detected, coverage = _literal_subject_references(tree=tree, generation=7)

    assert coverage.evaluated_fact_count == 1
    assert len(detected) == 1
    assert detected[0].subject == row.accepted.claim.identity
    assert detected[0].detail == {
        "literal_value": "demo-package",
        "matching_subject_kinds": ["component", "package"],
        "message": "literal looks like a subject reference; consider a subject-valued object",
    }
    assert {ref.identity for ref in detected[0].evidence_refs} == {
        row.accepted.claim.identity.qualified,
        package.shell.identity.qualified,
        component.shell.identity.qualified,
    }


def test_literal_subject_reference_ignores_plain_literals_and_retired_subjects() -> None:
    claim_subject = subject("project.work_item", "wi-42")
    retired = subject("package", "retired-package")
    retired_shell = retired.shell.model_copy(
        update={"lifecycle": retired.shell.lifecycle.model_copy(update={"state": "retired"})}
    )
    retired_row = claim_fact(
        1,
        subject_row=claim_subject,
        predicate="sec.vuln.affects_package",
        value="retired-package",
    )
    plain_row = claim_fact(
        2,
        subject_row=claim_subject,
        predicate="project.work_item.status",
        value="ordinary prose",
    )
    tree = {
        retired_row.accepted.path: render_claim(retired_row.accepted.claim),
        plain_row.accepted.path: render_claim(plain_row.accepted.claim),
        retired.path: render_subject(retired_shell),
    }

    detected, coverage = _literal_subject_references(tree=tree, generation=7)

    assert coverage.evaluated_fact_count == 2
    assert detected == ()


def test_literal_subject_reference_calibration_knob_disables_the_fold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim_subject = subject("project.work_item", "wi-42")
    package = subject("package", "demo-package")
    row = claim_fact(
        1,
        subject_row=claim_subject,
        predicate="sec.vuln.affects_package",
        value="demo-package",
    )
    monkeypatch.setattr(
        "cruxible_core.playbill.curation_detectors.LITERAL_SUBJECT_REFERENCE_DETECTOR_ENABLED",
        False,
    )

    detected, coverage = _literal_subject_references(
        tree={
            row.accepted.path: render_claim(row.accepted.claim),
            package.path: render_subject(package.shell),
        },
        generation=7,
    )

    assert detected == ()
    assert coverage.evaluated_fact_count == 0


def test_provenance_concentration_uses_effective_supporting_control_components() -> None:
    rows = tuple(
        claim_fact(
            index,
            subject_row=subject("project.work_item", f"wi-{index}"),
            predicate=PREDICATE,
            value="ready",
        ).model_copy(
            update={
                "captures": (_capture(index, "shared-owner"),),
                "resolved_authority_basis": (),
            }
        )
        for index in (1, 2)
    )

    detected, coverage = _provenance_concentration(
        rows=rows,
        providers=(),
        evaluation_time=NOW,
        generation=3,
        active_writing_principal_count=2,
    )

    assert coverage.evaluated_fact_count == 2
    assert len(detected) == 1
    component = next(ref for ref in detected[0].evidence_refs if ref.kind == "control_component")
    assert component.facts["control_domains"] == ["shared-owner"]
    claim_refs = tuple(ref for ref in detected[0].evidence_refs if ref.kind == "accepted_artifact")
    assert {tuple(ref.facts["effective_supporting_evidence_digests"]) for ref in claim_refs} == {
        (row.captures[0].capture_digest,) for row in rows
    }


def test_provenance_concentration_excludes_a_stale_capture_that_bridges_current_components() -> (
    None
):
    contract = capture_contract()
    upstream = provider(contract, name="upstream-b", control_domain="independent-b")
    first = claim_fact(
        1,
        subject_row=subject("project.work_item", "wi-1"),
        predicate=PREDICATE,
        value="ready",
    ).model_copy(
        update={
            "captures": (
                _capture(1, "independent-a"),
                _capture(3, "independent-a").model_copy(
                    update={
                        "current_replay_available": False,
                        "upstream_provenance": (upstream.identity,),
                    }
                ),
            ),
            "resolved_authority_basis": (),
        }
    )
    second = claim_fact(
        2,
        subject_row=subject("project.work_item", "wi-2"),
        predicate=PREDICATE,
        value="ready",
    ).model_copy(
        update={
            "captures": (_capture(2, "independent-b"),),
            "resolved_authority_basis": (),
        }
    )

    detected, coverage = _provenance_concentration(
        rows=(first, second),
        providers=(upstream,),
        evaluation_time=NOW,
        generation=3,
        active_writing_principal_count=2,
    )

    assert coverage.evaluated_fact_count == 2
    assert detected == ()


def test_provenance_concentration_is_silent_for_a_single_active_writer() -> None:
    rows = tuple(
        claim_fact(
            index,
            subject_row=subject("project.work_item", f"wi-{index}"),
            predicate=PREDICATE,
            value="ready",
        ).model_copy(
            update={
                "captures": (_capture(index, "shared-owner"),),
                "resolved_authority_basis": (),
            }
        )
        for index in (1, 2)
    )

    detected, coverage = _provenance_concentration(
        rows=rows,
        providers=(),
        evaluation_time=NOW,
        generation=3,
        active_writing_principal_count=1,
    )

    assert coverage.evaluated_fact_count == 2
    assert detected == ()


def test_active_writer_join_excludes_daemon_recovery_and_revoked_principals() -> None:
    principals = (
        SimpleNamespace(status="active", kind="daemon"),
        SimpleNamespace(status="active", kind="ordinary"),
        SimpleNamespace(status="active", kind="recovery"),
        SimpleNamespace(status="revoked", kind="ordinary"),
    )
    fake = SimpleNamespace(
        accepted_history=lambda: (
            SimpleNamespace(
                oid="accepted",
                principals=SimpleNamespace(principals=principals),
            ),
        )
    )

    assert _active_writing_principal_count(fake, git_oid="accepted") == 1  # type: ignore[arg-type]


def test_freshness_calibration_uses_changed_commitment_intervals_without_recommendation() -> None:
    subject_row = subject("project.work_item", "wi-42")
    base_type = claim_type(PREDICATE, subject_kinds=("project.work_item",))
    current_type = base_type.model_copy(
        update={
            "artifact_format": "playbill-claim-type-v3",
            "evidence_freshness": ClaimEvidenceFreshnessV1(
                stale_after=ClaimFreshnessDurationV1(microseconds=100)
            ),
        }
    )
    type_digest = claim_type_digest(current_type).tagged
    contract = capture_contract(name="test.orders-v1")
    contract_digest = capture_contract_digest(contract).tagged
    bodies: dict[str, bytes] = {}
    capture_digests: list[str] = []
    for index in range(4):
        source = ExternalSourceReferenceV1(
            source_identity="commerce.production.orders",
            producer_binding_digest=digest("binding", "orders"),
            coordinate_type="postgres-lsn-v1",
            coordinate={"lsn": index},
            selector_type="relation-primary-key-v1",
            selector={"id": "wi-42"},
            replayability="exact",
        )
        envelope = CaptureEnvelopeV1(
            capture_contract_digest=contract_digest,
            source=source,
            commitment=EvidenceCommitmentV1(
                digest_kind="canonical_value",
                digest=digest("commitment", str(index)),
                materialization="external",
            ),
            run_coordinate=CaptureRunCoordinateV1(
                run_kind="provider",
                run_id=f"run-{index}",
                bound_generation=digest("generation", str(index)),
                executable_identity=ArtifactIdentity(kind="Provider", name="orders"),
                executable_digest=digest("provider", "orders"),
            ),
            run_receipt_digest=digest("receipt", str(index)),
            producer=ArtifactIdentity(kind="Provider", name="orders"),
            producer_binding_digest=source.producer_binding_digest,
            observed_at=NOW + timedelta(microseconds=index * 1000),
        )
        raw = render_capture_envelope(envelope)
        capture_digest_value = "sha256:" + hashlib.sha256(raw).hexdigest()
        bodies[capture_digest_value] = raw
        capture_digests.append(capture_digest_value)
    row = claim_fact(1, subject_row=subject_row, predicate=PREDICATE, value="ready")
    statement = row.accepted.claim.statement.model_copy(update={"claim_type_digest": type_digest})
    backing = row.accepted.claim.backing.model_copy(
        update={"capture_digests": tuple(sorted(capture_digests))}
    )
    claim = row.accepted.claim.model_copy(
        update={
            "statement": statement,
            "backing": backing,
            "pins": (
                ArtifactPin(
                    role="claim-type",
                    target=current_type.identity,
                    artifact_digest=type_digest,
                ),
            ),
        }
    )
    tree = {
        claim_type_path(PREDICATE): render_claim_type(current_type),
        capture_contract_path(contract.identity.name): render_capture_contract(contract),
        row.accepted.path: render_claim(claim),
    }
    history = (SimpleNamespace(sequence=1, oid="one"),)
    fake = SimpleNamespace(
        accepted_history=lambda: history,
        tree_at=lambda _oid: tree,
        body_store=lambda: SimpleNamespace(read=lambda value, access: bodies[value]),
    )

    detected, coverage = _freshness_calibration(instance=fake, tree=tree)  # type: ignore[arg-type]

    assert coverage.status == "complete"
    assert len(detected) == 1
    assert detected[0].detail == {
        "capture_contract_identity": contract.identity.qualified,
        "external_source_identity": "commerce.production.orders",
        "selector_type": "relation-primary-key-v1",
    }
    sample = detected[0].evidence_refs[-1].facts
    assert sample["changed_interval_count"] == 3
    assert sample["ratio_numerator"] == 1
    assert sample["ratio_denominator"] == 10
    assert "recommendation" not in str(detected[0].model_dump(mode="json")).lower()


def test_dead_vocabulary_starts_at_the_later_of_acceptance_and_receipt_epoch(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    contract = claim_type(PREDICATE, subject_kinds=("project.work_item",))
    tree = {claim_type_path(PREDICATE): render_claim_type(contract)}
    monkeypatch.setattr(
        "cruxible_core.playbill.curation_detectors.consumption_aggregate",
        lambda _instance: ConsumptionAggregateV1(
            initialized=True,
            consumption_epoch_generation=3,
            artifacts=(),
        ),
    )
    history = _CurationHistoryIndex(
        claims=(),
        capture_contract_identities={},
        first_accepted_generations={contract.identity.qualified: 1},
        last_generation=13,
    )
    fake = SimpleNamespace()

    too_early, _ = _dead_vocabulary(
        instance=fake,  # type: ignore[arg-type]
        tree=tree,
        generation=12,
        operational_head_digest="sha256:" + "9" * 64,
        history=history,
    )
    due, _ = _dead_vocabulary(
        instance=fake,  # type: ignore[arg-type]
        tree=tree,
        generation=13,
        operational_head_digest="sha256:" + "9" * 64,
        history=history,
    )

    assert too_early == ()
    assert len(due) == 1
    assert due[0].subject == contract.identity
    assert due[0].detail == {"artifact_family": "ClaimType"}


def test_shared_history_index_prevents_per_detector_and_per_claim_rescans() -> None:
    subject_row = subject("project.work_item", "wi-42")
    claim = claim_fact(1, subject_row=subject_row, predicate=PREDICATE, value="ready")
    tree = {claim.accepted.path: render_claim(claim.accepted.claim)}
    calls = {"history": 0, "tree": 0}

    def history():  # type: ignore[no-untyped-def]
        calls["history"] += 1
        return (
            SimpleNamespace(sequence=1, oid="one"),
            SimpleNamespace(sequence=2, oid="two"),
        )

    def tree_at(_oid):  # type: ignore[no-untyped-def]
        calls["tree"] += 1
        return tree

    fake = SimpleNamespace(
        accepted_history=history,
        tree_at=tree_at,
        body_store=lambda: None,
    )

    indexed = _curation_history_index(fake)  # type: ignore[arg-type]
    _freshness_calibration(
        instance=fake,  # type: ignore[arg-type]
        tree={},
        generation=2,
        history=indexed,
    )

    assert calls == {"history": 1, "tree": 2}


def test_run_curation_detectors_builds_shared_history_once_for_all_history_consumers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cruxible_core.playbill.curation_detectors as detector_module

    sentinel = object()
    calls = 0

    def history(_instance):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return sentinel

    empty_facts = SimpleNamespace(claims=(), subjects=(), providers=())
    fake = SimpleNamespace(
        resolve_accepted_coordinate=lambda **_kwargs: SimpleNamespace(git_oid="tree"),
        tree_at=lambda _oid: {},
    )
    monkeypatch.setattr(detector_module, "_curation_history_index", history)
    monkeypatch.setattr(detector_module, "_active_writing_principal_count", lambda *_a, **_k: 2)
    monkeypatch.setattr(
        detector_module,
        "build_accepted_query_facts",
        lambda *_a, **_k: empty_facts,
    )

    def no_history(*_args, **kwargs):  # type: ignore[no-untyped-def]
        if "history" in kwargs:
            assert kwargs["history"] is sentinel
        kind = kwargs.pop("_kind")
        return (), CurationDetectorCoverageV1(
            pattern_kind=kind,
            status="complete",
            evaluated_fact_count=0,
        )

    names = (
        ("_recurring_conflicts", CURATION_PATTERN_KINDS[0]),
        ("_admission_failures", CURATION_PATTERN_KINDS[1]),
        ("_freshness_calibration", CURATION_PATTERN_KINDS[2]),
        ("_provenance_concentration", CURATION_PATTERN_KINDS[3]),
        ("_duplicate_statements", CURATION_PATTERN_KINDS[4]),
        ("_qualifier_crystallization", CURATION_PATTERN_KINDS[5]),
        ("_block_churn", CURATION_PATTERN_KINDS[6]),
        ("_dead_vocabulary", CURATION_PATTERN_KINDS[7]),
        ("_literal_subject_references", CURATION_PATTERN_KINDS[8]),
    )
    for name, kind in names:
        monkeypatch.setattr(
            detector_module,
            name,
            lambda *_a, _kind=kind, **kwargs: no_history(*_a, _kind=_kind, **kwargs),
        )

    detector_module.run_curation_detectors(
        fake,  # type: ignore[arg-type]
        coordinate=AcceptedCoordinate(
            git_oid="a" * 40,
            semantic_root="sha256:" + "1" * 64,
            generation_root="sha256:" + "2" * 64,
            compiler_digest="sha256:" + "3" * 64,
        ),
        generation=1,
        evaluation_time=NOW,
        operational_head_digest="sha256:" + "4" * 64,
    )

    assert calls == 1
