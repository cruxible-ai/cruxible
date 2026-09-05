"""One next request shares accepted inputs without caching mutable observations."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from cruxible_client.contracts.canonical import canonical_bytes
from cruxible_client.contracts.claims import claim_citation_references
from cruxible_client.contracts.errors import ProjectionIntegrityError
from cruxible_core.service import playbill_next, playbill_query
from cruxible_core.service.playbill_claims import _claim_from_view, service_list_playbill_claims
from cruxible_core.service.playbill_next import PlaybillNextDriftObservationV1
from tests.test_playbill.test_projection_next import (
    _claim_backing,
    _query_backing,
    _request,
)
from tests.test_playbill.test_query_execution_service import _instance_with_query


def _uncached_folds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the existing standalone helpers without their request-local inputs."""
    for name in (
        "_claim_items",
        "_citation_commitments",
        "_projection_items",
        "_claim_dependency_items",
    ):
        original = getattr(playbill_next, name)

        def uncached(*args: Any, _original: Any = original, **kwargs: Any) -> Any:
            kwargs.pop("claims", None)
            kwargs.pop("facts_reader", None)
            return _original(*args, **kwargs)

        monkeypatch.setattr(playbill_next, name, uncached)


def _rich_request(instance: Any) -> Any:
    return _request(
        instance,
        backing=(_claim_backing(instance, stale=True), _query_backing(instance, stale=True)),
        dirty=True,
    )


def test_next_shares_population_and_facts_without_changing_complete_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner = _instance_with_query(tmp_path)
    request = _rich_request(instance)
    counts: Counter[str] = Counter()
    original_list = playbill_next.service_list_playbill_claims
    original_row = playbill_query._fact_row

    def listed(*args: Any, **kwargs: Any) -> Any:
        counts["population"] += 1
        return original_list(*args, **kwargs)

    def row(*args: Any, **kwargs: Any) -> Any:
        counts["fact_row"] += 1
        return original_row(*args, **kwargs)

    monkeypatch.setattr(playbill_next, "service_list_playbill_claims", listed)
    monkeypatch.setattr(playbill_query, "_fact_row", row)
    optimized = playbill_next.service_playbill_next(instance, request=request)
    assert counts == {"population": 1, "fact_row": 2}
    assert {item.reason for item in optimized.items}.issuperset(
        {"projection_dirty", "projection_backing_stale", "projection_candidates_changed"}
    )

    counts.clear()
    _uncached_folds(monkeypatch)
    uncached = playbill_next.service_playbill_next(instance, request=request)
    assert counts["population"] == 3
    assert counts["fact_row"] == 6
    assert canonical_bytes(optimized.model_dump(mode="json")) == canonical_bytes(
        uncached.model_dump(mode="json")
    )


def test_retired_population_preserves_complete_next_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_playbill._published_world import published_world, retire_claim

    instance, owner, claim_id = published_world(tmp_path)
    backing = _claim_backing(instance)
    retire_claim(instance, owner, claim_id)
    request = _request(instance, backing=(backing,))
    optimized = playbill_next.service_playbill_next(instance, request=request)
    retired_row = next(
        item for item in optimized.items if item.reason == "projection_backing_stale"
    )
    assert retired_row.detail["retired_backings"] == [backing.identity.qualified]
    _uncached_folds(monkeypatch)
    assert playbill_next.service_playbill_next(instance, request=request) == optimized


def test_each_next_request_rereads_current_cas_availability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner = _instance_with_query(tmp_path)
    request = _rich_request(instance)
    original = playbill_query._fact_row
    observed: list[bool] = []
    source_digest = instance.body_store().digest_bytes(b"status: ready").tagged

    def row(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        if result.accepted.claim.statement.object.value == "ready":
            observed.extend(capture.current_replay_available for capture in result.captures)
        return result

    monkeypatch.setattr(playbill_query, "_fact_row", row)
    first = playbill_next.service_playbill_next(instance, request=request)
    assert observed and all(observed)
    observed.clear()
    assert instance.body_store().erase(source_digest)
    try:
        playbill_next.service_playbill_next(instance, request=request)
        assert observed and not any(observed)
    finally:
        assert instance.body_store().store(b"status: ready").digest == source_digest
    observed.clear()
    assert playbill_next.service_playbill_next(instance, request=request) == first
    assert observed and all(observed)


def test_next_reuses_no_source_or_marker_observation_across_requests(tmp_path: Path) -> None:
    instance, _owner = _instance_with_query(tmp_path)
    claim = _claim_from_view(service_list_playbill_claims(instance).claims[0])
    citation = claim_citation_references(claim)[0]
    from cruxible_client.contracts.captures import parse_capture_envelope
    from cruxible_core.playbill.cas import BodyAccessContext

    envelope = parse_capture_envelope(
        instance.body_store().read(
            citation.capture_digest,
            access=BodyAccessContext(principal_id="fixture", can_read_body=True),
        )
    )
    clean = _request(instance, backing=(_claim_backing(instance),))
    assert clean.workspace_observation is not None
    clean = clean.model_copy(
        update={
            "workspace_observation": clean.workspace_observation.model_copy(
                update={
                    "drift_observations": (
                        PlaybillNextDriftObservationV1(
                            citation_id=citation.citation_id,
                            expected_commitment_digest=envelope.commitment.digest,
                            observed_commitment_digest=envelope.commitment.digest,
                        ),
                    )
                }
            )
        }
    )
    changed = _request(instance, backing=(_claim_backing(instance),), dirty=True)
    assert changed.workspace_observation is not None
    changed = changed.model_copy(
        update={
            "workspace_observation": changed.workspace_observation.model_copy(
                update={
                    "drift_observations": (
                        PlaybillNextDriftObservationV1(
                            citation_id=citation.citation_id,
                            expected_commitment_digest=envelope.commitment.digest,
                            observed_commitment_digest="sha256:" + "f" * 64,
                        ),
                    )
                }
            )
        }
    )
    first = playbill_next.service_playbill_next(instance, request=clean)
    dirty = playbill_next.service_playbill_next(instance, request=changed)
    assert {item.reason for item in dirty.items}.issuperset(
        {"projection_dirty", "citation_drifted"}
    )
    assert "projection_dirty" not in {item.reason for item in first.items}
    assert "citation_drifted" not in {item.reason for item in first.items}
    assert dirty.result_digest != first.result_digest
    assert playbill_next.service_playbill_next(instance, request=clean) == first


def test_failed_initial_population_load_preserves_per_fold_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner = _instance_with_query(tmp_path)
    request = _rich_request(instance)
    expected = playbill_next.service_playbill_next(instance, request=request)
    original = playbill_next.service_list_playbill_claims
    calls = 0

    def fail_once(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProjectionIntegrityError("synthetic unavailable population projection")
        return original(*args, **kwargs)

    monkeypatch.setattr(playbill_next, "service_list_playbill_claims", fail_once)
    assert playbill_next.service_playbill_next(instance, request=request) == expected
    assert calls == 3


def test_persistent_population_failure_retains_the_standalone_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, _owner = _instance_with_query(tmp_path)
    request = _rich_request(instance)

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise ProjectionIntegrityError("synthetic malformed population projection")

    monkeypatch.setattr(playbill_next, "service_list_playbill_claims", unavailable)
    with pytest.raises(ProjectionIntegrityError) as optimized:
        playbill_next.service_playbill_next(instance, request=request)
    _uncached_folds(monkeypatch)
    with pytest.raises(ProjectionIntegrityError) as uncached:
        playbill_next.service_playbill_next(instance, request=request)
    assert str(optimized.value) == str(uncached.value)
