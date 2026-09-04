"""Read-path memo laws: what a read is allowed to re-read, and what it must re-prove."""

from __future__ import annotations

import subprocess
import threading
from collections import Counter, OrderedDict
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
    claim_path,
    new_claim_id,
)
from cruxible_client.contracts.declared_blocks import (
    ProjectionBlockStampV1,
    ProjectionClaimBackingV1,
    ProjectionMarkerSummaryV1,
)
from cruxible_client.contracts.errors import PlaybillGitError, ProjectionIntegrityError
from cruxible_client.contracts.policies import (
    ClaimEvidenceAdmissionPolicyV1,
    ClaimEvidenceAdmissionRuleV1,
)
from cruxible_core.playbill.consumption import consumption_artifacts_for_paths
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.git import GitLedger
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.search import PlaybillSearchRequestV1
from cruxible_core.runtime import playbill_api as runtime_api
from cruxible_core.service import playbill_evidence, playbill_next
from cruxible_core.service.playbill_claims import (
    _claim_from_view,
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
from cruxible_core.service.playbill_query import build_accepted_query_facts
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


class _PreemptingMemo(OrderedDict[str, dict[str, bytes]]):
    """A tree memo that hands control to an activation mid-read, exactly once.

    Read routes are serialized on the event loop, but ``activate_proposal`` is a
    sync handler and therefore runs in the anyio worker threadpool, where its
    ``refresh()`` clears this dict. This stands in for that interleaving
    deterministically: the reader is released the moment the clear has landed,
    so the second half of the read runs against an emptied memo.
    """

    def __init__(self, *, reading: threading.Event, cleared: threading.Event) -> None:
        super().__init__()
        self._reading = reading
        self._cleared = cleared
        self._armed = True

    def _preempt(self) -> None:
        if not self._armed:
            return
        self._armed = False
        self._reading.set()
        assert self._cleared.wait(30)

    def get(self, key: str, default: object = None) -> object:  # type: ignore[override]
        value = super().get(key, default)  # type: ignore[arg-type]
        if value is not None:
            self._preempt()
        return value

    def __getitem__(self, key: str) -> dict[str, bytes]:
        value = super().__getitem__(key)
        self._preempt()
        return value

    def clear(self) -> None:
        super().clear()
        self._cleared.set()


def test_a_cached_read_survives_an_activation_clearing_the_memo_under_it(
    tmp_path: Path,
) -> None:
    instance, _owner = seed_claims(tmp_path)
    oid = instance.accepted_coordinate().git_oid
    expected = instance.tree_at(oid)

    reading = threading.Event()
    cleared = threading.Event()
    memo = _PreemptingMemo(reading=reading, cleared=cleared)
    memo[oid] = dict(expected)
    instance._tree_memo = memo

    def activate_concurrently() -> None:
        assert reading.wait(30)
        instance.refresh()

    worker = threading.Thread(target=activate_concurrently)
    worker.start()
    try:
        # The read finds the entry, the activation empties the memo under it,
        # and the read must still answer with the accepted tree rather than
        # raising out of the promotion it can no longer perform.
        assert instance.tree_at(oid) == expected
    finally:
        cleared.set()
        worker.join(30)
    assert not worker.is_alive()


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


def _commit_carrying_a_symlink(ledger_path: Path) -> str:
    """Commit one tree whose single member is a symlink, not a plain file."""

    def git(arguments: list[str], stdin: bytes = b"") -> bytes:
        return subprocess.run(
            ["git", f"--git-dir={ledger_path}", *arguments],
            input=stdin,
            capture_output=True,
            check=True,
        ).stdout

    blob = git(["hash-object", "-w", "--stdin"], b"../elsewhere").decode().strip()
    tree = git(["mktree"], f"120000 blob {blob}\tlink.json\n".encode()).decode().strip()
    return git(["commit-tree", tree, "-m", "poisoned"]).decode().strip()


def test_listing_accepted_paths_refuses_what_reading_them_refuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _owner = seed_claims(tmp_path)
    poisoned = _commit_carrying_a_symlink(instance._ledger.path)
    # The listing is only reachable behind the acceptance proof, so stand in for
    # a generation the ledger accepted and then ask what each reader answers.
    monkeypatch.setattr(type(instance), "coordinate_for_oid", lambda self, oid: None)

    with pytest.raises(PlaybillGitError, match="unsupported 120000"):
        instance._ledger.read_tree(poisoned)

    # Cold: nothing memoized, the listing goes to Git and must refuse there.
    instance._tree_memo.clear()
    with pytest.raises(PlaybillGitError, match="unsupported 120000"):
        instance.paths_at(poisoned)

    # Warm: the only way to fill the memo is a read, which refuses first, so no
    # memo state can turn the refusal into an answer.
    with pytest.raises(PlaybillGitError, match="unsupported 120000"):
        instance.tree_at(poisoned)
    assert poisoned not in instance._tree_memo
    with pytest.raises(PlaybillGitError, match="unsupported 120000"):
        instance.paths_at(poisoned)


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


def _declared_projection_observation(
    instance: Any,
) -> PlaybillNextWorkspaceObservationV1:
    """Observe one declared block, so `next` reaches the resolution fold.

    Without a marker summary `_projection_items` returns before the fold ever
    runs, and only `_claim_items` evaluates a verdict: a request with no
    workspace observation cannot tell a shared verdict map from a per-fold one.
    """

    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    row = build_accepted_query_facts(instance, coordinate=instance.accepted_coordinate()).claims[0]
    stamp = ProjectionBlockStampV1(
        source_id="fixture.work-items",
        block_id="status",
        declared_generation=0,
        declared_coordinate=coordinate,
        backing=(
            ProjectionClaimBackingV1(
                identity=row.accepted.claim.identity,
                statement_digest=row.accepted.statement_digest,
            ),
        ),
        body_digest="sha256:" + "b" * 64,
    )
    return PlaybillNextWorkspaceObservationV1(
        source_observations=(
            PlaybillNextSourceObservationV4(
                source_id="fixture.work-items",
                observed_source_digest="sha256:" + "0" * 64,
                byte_length=1000,
                marker_summaries=(
                    ProjectionMarkerSummaryV1(
                        stamp=stamp,
                        observed_body_digest="sha256:" + "b" * 64,
                        start_byte=0,
                        end_byte=100,
                    ),
                ),
                occurrences=(),
                commitment_scan_proofs=(),
                citation_window_observations=(),
                scan_notes=(),
                marker_notes=(),
            ),
        )
    )


def test_next_evaluates_each_claim_verdict_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _owner = seed_claims(tmp_path)
    observation = _declared_projection_observation(instance)
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
    result = service_playbill_next(
        instance,
        request=PlaybillNextRequestV1(
            at=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
            evaluation_time=EVALUATION_TIME,
            access_profile=ACCESS,
            workspace_observation=observation,
        ),
    )

    # The observation actually reached the projection fold, so both folds ran.
    assert "workspace_sources" in result.observed_domains
    assert evaluated
    assert set(evaluated) <= {claim.identity.qualified for claim in live}
    assert max(evaluated.values()) == 1
    assert sum(evaluated.values()) == len(evaluated)


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


def test_one_claim_read_materializes_no_generation_and_still_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, _owner = seed_claims(tmp_path)
    claim = _claim_from_view(service_list_playbill_claims(instance).claims[0])
    identity = claim.identity.name
    coordinate = instance.accepted_coordinate()
    path = claim_path(identity)

    class _Manager:
        def get(self, _instance_id: str) -> Any:
            return instance

    monkeypatch.setattr(runtime_api, "check_permission", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(runtime_api, "get_playbill_manager", lambda: _Manager())
    instance._tree_memo.clear()

    counted = _count_read_trees(monkeypatch)
    view = runtime_api.playbill_get_claim(
        instance.descriptor.instance_id,
        identity,
        evaluation_time=EVALUATION_TIME,
    )

    # One served read reads the paths it answers for and nothing else.
    assert counted == Counter()
    assert view.envelope["path"] == path
    # The narrow read still resolved the ClaimType and the capture contracts.
    assert view.admission_accounts

    # The receipt is still written, and names the artifact the read served.
    served = consumption_artifacts_for_paths(instance.tree_at(coordinate.git_oid), (path,))
    assert served
    receipted = [
        (payload["response_artifact_identity"], payload["response_artifact_digest"])
        for _event, payload in instance.review_operational_store().events(family="consumption")
        if payload.get("tag") == "playbill-consumption-receipt-v1"
    ]
    assert receipted == [
        (served_identity.model_dump(mode="json"), digest) for served_identity, digest in served
    ]


def _scanned_source_observation(
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
    other_source_notes: tuple[str, ...] | None = None,
) -> tuple[Any, ...]:
    sources = [_scanned_source_observation(source_id="fixture.work-items", scan_notes=scan_notes)]
    if other_source_notes is not None:
        sources.append(
            _scanned_source_observation(
                source_id="fixture.other-notes", scan_notes=other_source_notes
            )
        )
    result = service_playbill_next(
        instance,
        request=PlaybillNextRequestV1(
            at=AcceptedCoordinate.from_internal(instance.accepted_coordinate()),
            evaluation_time=EVALUATION_TIME,
            access_profile=ACCESS,
            workspace_observation=PlaybillNextWorkspaceObservationV1(
                source_observations=tuple(
                    sorted(sources, key=lambda item: item.source_id.encode("utf-8"))
                )
            ),
        ),
    )
    return tuple(item for item in result.items if item.reason == "citation_source_unobserved")


def test_a_defective_coverage_scan_reports_one_row_naming_its_cause(
    tmp_path: Path,
) -> None:
    instance, _owner = seed_claims(tmp_path)

    healthy = _unobserved_rows(instance, scan_notes=())
    assert len(healthy) > 1
    assert all("unobserved_cause" not in row.detail for row in healthy)

    # Every note that says the source's own scan is incomplete collapses its
    # citations onto one row, and the row names the cause, not a co-symptom.
    for notes in (
        ("coverage_partial",),
        ("coverage_stale",),
        ("coverage_denied",),
        ("coverage_unavailable",),
        ("coverage_span_missing",),
        ("coverage_result_version_unsupported",),
        ("coverage_card_limit_exceeded",),
        ("coverage_proof_limit_exceeded",),
        ("coverage_window_limit_exceeded",),
        # The program instance's own shape: partial *and* window-capped. The
        # cap is reported truthfully alongside, never as the cause.
        ("coverage_partial", "coverage_window_limit_exceeded"),
    ):
        collapsed = _unobserved_rows(instance, scan_notes=notes)
        assert len(collapsed) == 1
        (row,) = collapsed
        assert row.detail["source_id"] == "fixture.work-items"
        assert row.detail["unobserved_cause"] == "source_scan_incomplete"
        assert row.detail["source_scan_notes"] == list(notes)
        assert row.detail["collapsed_citation_count"] == len(healthy)
        assert row.repair.operation == "playbill.authoring.bind"

    # A note about one dropped item is not a whole-source defect: those
    # citations each keep their own row, because the scan can still speak to
    # them one at a time.
    assert len(_unobserved_rows(instance, scan_notes=("coverage_proof_invalid",))) == len(healthy)

    # A second, healthy source in the same observation is untouched.
    mixed = _unobserved_rows(
        instance,
        scan_notes=("coverage_partial",),
        other_source_notes=(),
    )
    assert sum(1 for row in mixed if row.detail["source_id"] == "fixture.work-items") == 1


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
