"""Read-path memo laws: what a read is allowed to re-read, and what it must re-prove."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.captures import (
    COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT,
    AcceptedCaptureContract,
    capture_contract_digest,
    capture_contract_path,
)
from cruxible_client.contracts.claims import (
    _self_source_capture_admitted_by_rule,
    new_claim_id,
)
from cruxible_client.contracts.errors import ProjectionIntegrityError
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicyV1,
    ClaimEvidenceAdmissionRuleV1,
)
from cruxible_core.playbill.authoring.insertions import (
    PublicationClaimProjectedAsItself,
    refuse_claim_projected_as_itself,
)
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.git import GitLedger
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.search import PlaybillSearchRequestV1
from cruxible_core.service import playbill_evidence, playbill_next
from cruxible_core.service.playbill_claims import (
    _claim_from_view,
    service_get_playbill_claim,
    service_list_playbill_claims,
)
from cruxible_core.service.playbill_next import (
    PlaybillNextRequestV1,
    PlaybillNextSourceObservationV4,
    PlaybillNextWorkspaceObservationV1,
    service_playbill_next,
)
from cruxible_core.service.playbill_publications import (
    bound_publication_registrations,
    reset_bound_publication_registration_memo,
)
from cruxible_core.service.playbill_search import service_search_playbill
from cruxible_core.storage import playbill_projection
from tests.test_playbill._knowledge_loop_support import seed_claims
from tests.test_playbill.test_claims import _claim as _test_claim
from tests.test_playbill.test_claims import _claim_type as _test_claim_type

EVALUATION_TIME = datetime(2026, 8, 21, 14, tzinfo=UTC)
ACCESS = CoverageAccessProfileV1(profile_id="read-latency-test")


def _orient_request(instance: Any) -> PlaybillSearchRequestV1:
    return PlaybillSearchRequestV1(
        mode="orient",
        accepted_coordinate=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
        evaluation_time=EVALUATION_TIME,
        access_profile=ACCESS,
    )


def _count_read_trees(monkeypatch: pytest.MonkeyPatch) -> Counter[str]:
    counted: Counter[str] = Counter()
    original = GitLedger.read_tree

    def counting(self: GitLedger, oid: str) -> dict[str, bytes]:
        counted[oid] += 1
        return original(self, oid)

    monkeypatch.setattr(GitLedger, "read_tree", counting)
    return counted


def test_orient_reads_each_accepted_generation_tree_at_most_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _owner = seed_claims(tmp_path)
    generations = len(instance.accepted_history())
    instance._tree_memo.clear()

    counted = _count_read_trees(monkeypatch)
    service_search_playbill(instance, request=_orient_request(instance))

    assert counted
    assert max(counted.values()) == 1
    assert len(counted) <= generations

    counted.clear()
    service_search_playbill(instance, request=_orient_request(instance))
    assert counted == Counter()


def test_a_memoized_tree_is_handed_out_as_an_independent_copy(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)
    oid = instance.accepted_coordinate().git_oid

    first = instance.tree_at(oid)
    paths = set(first)
    first.pop(next(iter(first)))
    first["claims/injected.json"] = b"{}"

    second = instance.tree_at(oid)
    assert set(second) == paths


def test_blob_and_path_reads_agree_with_the_whole_tree(tmp_path: Path) -> None:
    instance, _owner = seed_claims(tmp_path)
    oid = instance.accepted_coordinate().git_oid
    tree = instance.tree_at(oid)
    instance._tree_memo.clear()

    assert set(instance.paths_at(oid)) == set(tree)
    sample = sorted(path for path in tree if path.startswith("claims/"))[:2]
    assert instance.blobs_at(oid, sample) == {path: tree[path] for path in sample}
    assert instance.blob_at(oid, sample[0]) == tree[sample[0]]
    assert instance.blob_at(oid, "claims/absent-from-this-generation.json") is None


def test_a_serving_piece_is_verified_once_and_a_tampered_piece_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _owner = seed_claims(tmp_path)
    coordinate = instance.accepted_coordinate()
    playbill_projection.reset_projection_verification_memo()

    calls = 0
    original = playbill_projection.projection_logical_digest

    def counting(path: Path) -> Any:
        nonlocal calls
        calls += 1
        return original(path)

    monkeypatch.setattr(playbill_projection, "projection_logical_digest", counting)
    piece: Path | None = None
    for _ in range(3):
        with instance.bind_accepted_projection(coordinate) as handle:
            piece = handle.piece_paths[0]
    assert calls == 1
    assert piece is not None

    content = bytearray(piece.read_bytes())
    # Flip one byte deep inside the page data, past the SQLite header.
    content[-1] ^= 0xFF
    mode = piece.stat().st_mode
    piece.chmod(0o600)
    piece.write_bytes(bytes(content))
    piece.chmod(mode)

    with pytest.raises(ProjectionIntegrityError):
        with instance.bind_accepted_projection(coordinate):
            pass


def test_next_evaluates_each_claim_verdict_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _owner = seed_claims(tmp_path)
    evaluated: Counter[str] = Counter()
    original = playbill_evidence.service_evaluate_playbill_claim_verdict

    def counting(instance_: Any, *, claim_identity: str, **values: Any) -> Any:
        evaluated[claim_identity] += 1
        return original(instance_, claim_identity=claim_identity, **values)

    monkeypatch.setattr(playbill_evidence, "service_evaluate_playbill_claim_verdict", counting)
    monkeypatch.setattr(playbill_next, "service_evaluate_playbill_claim_verdict", counting)

    live = tuple(
        claim
        for claim in (
            _claim_from_view(view) for view in service_list_playbill_claims(instance).claims
        )
        if claim.lifecycle.state == "live"
    )
    service_playbill_next(
        instance,
        request=PlaybillNextRequestV1(
            at=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
            evaluation_time=EVALUATION_TIME,
            access_profile=ACCESS,
        ),
    )

    assert evaluated
    assert set(evaluated) <= {claim.identity.qualified for claim in live}
    assert max(evaluated.values()) == 1


def test_the_claim_read_history_index_is_built_once_per_coordinate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _owner = seed_claims(tmp_path)
    instance.claim_read_history_memo.clear()
    built = 0
    original = playbill_evidence._build_claim_read_history_index

    def counting(source: Any, *, coordinate: Any) -> Any:
        nonlocal built
        built += 1
        return original(source, coordinate=coordinate)

    monkeypatch.setattr(playbill_evidence, "_build_claim_read_history_index", counting)
    service_search_playbill(instance, request=_orient_request(instance))
    assert built == 1

    service_search_playbill(instance, request=_orient_request(instance))
    assert built == 1

    # Replay after activation must not serve a superseded index.
    instance.refresh()
    assert instance.claim_read_history_memo == {}


def test_the_publication_intent_fold_runs_once_per_durable_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cruxible_core.playbill.authoring.store import AuthoringIntentStore

    instance, _owner = seed_claims(tmp_path)
    (instance.root / instance.descriptor.storage.exhaust / "authoring-intents").mkdir(
        mode=0o700, parents=True, exist_ok=True
    )
    reset_bound_publication_registration_memo()

    folds = 0
    original = AuthoringIntentStore.events

    def counting(self: AuthoringIntentStore) -> Any:
        nonlocal folds
        folds += 1
        return original(self)

    monkeypatch.setattr(AuthoringIntentStore, "events", counting)
    first = bound_publication_registrations(instance)
    for _ in range(5):
        assert bound_publication_registrations(instance) == first
    assert folds == 1


def test_one_claim_read_does_not_materialize_extra_generations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _owner = seed_claims(tmp_path)
    identity = _claim_from_view(service_list_playbill_claims(instance).claims[0]).identity.name
    instance._tree_memo.clear()

    counted = _count_read_trees(monkeypatch)
    service_get_playbill_claim(instance, identity=identity, evaluation_time=EVALUATION_TIME)

    assert sum(counted.values()) <= 1


def _clipped_source_observation(
    *,
    source_id: str,
    scan_notes: tuple[str, ...],
) -> PlaybillNextSourceObservationV4:
    return PlaybillNextSourceObservationV4(
        source_id=source_id,
        observed_source_digest="sha256:" + "0" * 64,
        byte_length=64,
        marker_summaries=(),
        occurrences=(),
        commitment_scan_proofs=(),
        citation_window_observations=(),
        scan_notes=scan_notes,
        marker_notes=(),
    )


def _unobserved_rows(
    instance: Any,
    *,
    scan_notes: tuple[str, ...],
) -> tuple[Any, ...]:
    result = service_playbill_next(
        instance,
        request=PlaybillNextRequestV1(
            at=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
            evaluation_time=EVALUATION_TIME,
            access_profile=ACCESS,
            workspace_observation=PlaybillNextWorkspaceObservationV1(
                source_observations=(
                    _clipped_source_observation(
                        source_id="fixture.work-items",
                        scan_notes=scan_notes,
                    ),
                )
            ),
        ),
    )
    return tuple(item for item in result.items if item.reason == "citation_source_unobserved")


def test_a_clipped_coverage_scan_reports_one_row_for_its_whole_source(
    tmp_path: Path,
) -> None:
    instance, _owner = seed_claims(tmp_path)

    uncapped = _unobserved_rows(instance, scan_notes=())
    assert len(uncapped) > 1
    assert all("clipped_scan_notes" not in row.detail for row in uncapped)

    clipped = _unobserved_rows(instance, scan_notes=("coverage_proof_limit_exceeded",))
    assert len(clipped) == 1
    (row,) = clipped
    assert row.detail["source_id"] == "fixture.work-items"
    assert row.detail["clipped_scan_notes"] == ["coverage_proof_limit_exceeded"]
    assert row.detail["collapsed_citation_count"] == len(uncapped)
    assert row.repair.operation == "playbill.authoring.bind"


def _coordinator_contract() -> AcceptedCaptureContract:
    contract = COORDINATOR_SELF_SOURCE_CAPTURE_CONTRACT
    return AcceptedCaptureContract(
        path=capture_contract_path(contract.identity.name),
        contract=contract,
        artifact_digest=capture_contract_digest(contract).tagged,
    )


def test_a_prose_claim_type_admits_its_own_authored_body_and_a_domain_type_does_not() -> None:
    claim = _test_claim(
        claim_id=new_claim_id(),
        capture_digest="sha256:" + "1" * 64,
        source_digest="sha256:" + "2" * 64,
        source_length=13,
    )
    accepted = _coordinator_contract()
    domain_type = _test_claim_type()

    # A domain ClaimType names no rule for the coordinator self-source contract,
    # so the citation is skipped exactly as before and the Claim reads uncovered.
    assert not _self_source_capture_admitted_by_rule(
        claim,
        claim_type=domain_type,
        capture_contract=accepted,
        capture_digest=claim.backing.citations[0].capture_digest,
    )

    prose_type = domain_type.model_copy(
        update={
            "evidence_admission_policy": ClaimEvidenceAdmissionPolicyV1(
                rules=(
                    ClaimEvidenceAdmissionRuleV1(
                        rule_id="authored-prose",
                        claim_roles=("normative", "observation"),
                        capture_contract_digests=(accepted.artifact_digest,),
                        evidence_kinds=accepted.contract.evidence_kinds,
                        admission="origin_only",
                        subject_binding="exact_claim_subject",
                    ),
                )
            )
        }
    )
    assert _self_source_capture_admitted_by_rule(
        claim,
        claim_type=prose_type,
        capture_contract=accepted,
        capture_digest=claim.backing.citations[0].capture_digest,
    )


def test_a_claim_projected_as_itself_is_refused_by_name() -> None:
    assert (
        refuse_claim_projected_as_itself(
            claim_identity="CLM-0",
            source_id="program-cards",
            overlapping_citation_ids=(),
        )
        is None
    )
    with pytest.raises(PublicationClaimProjectedAsItself) as refusal:
        refuse_claim_projected_as_itself(
            claim_identity="CLM-0",
            source_id="program-cards",
            overlapping_citation_ids=("cit-b", "cit-a"),
        )
    message = str(refusal.value)
    assert message.startswith("playbill.authoring.publication_claim_projected_as_itself: ")
    assert "program-cards" in message
    assert "cit-a, cit-b" in message
