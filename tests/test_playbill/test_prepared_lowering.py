"""Prepared lowering is disposable; live preflight laws still run on cache hits."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from cruxible_client.contracts.claims import LiteralClaimObject
from cruxible_client.contracts.errors import PlaybillCasError, ProposalAdmissionError
from cruxible_client.contracts.proposal_models import ProposalReceiveLimits
from cruxible_client.contracts.subjects import render_subject, subject_path
from cruxible_core.playbill.authoring import preflight as preflight_module
from cruxible_core.playbill.authoring import prepared_lowering
from cruxible_core.playbill.proposals import AuthenticatedActor
from tests.test_playbill._support import initialize_local
from tests.test_playbill.test_authoring_change_set_intents import (
    _change_set,
    _claim,
    _coordinator,
    _shell,
)
from tests.test_playbill.test_authoring_preflight import (
    TIMESTAMP,
    _seed_claim_surface,
    _working_payload,
)
from tests.test_playbill.test_resolution_contracts import _accept_tree


def _setup(tmp_path: Path):  # type: ignore[no-untyped-def]
    instance, owner = initialize_local(tmp_path)
    _seed_claim_surface(instance, owner)
    coordinator = _coordinator(instance)
    actor = AuthenticatedActor(actor_id="owner")
    intent = coordinator.create(
        actor=actor,
        payload=_change_set(_claim(qualifier="first"), _claim(qualifier="second")),
        canonical_timestamp=TIMESTAMP,
    ).intent
    return instance, coordinator, actor, intent


def test_prepare_submit_reuses_lowering_but_evaluates_again(tmp_path: Path) -> None:
    _instance, coordinator, actor, intent = _setup(tmp_path)
    with (
        patch.object(
            preflight_module, "lower_authoring", wraps=preflight_module.lower_authoring
        ) as lowering,
        patch.object(
            preflight_module,
            "evaluate_proposal_tree",
            wraps=preflight_module.evaluate_proposal_tree,
        ) as evaluation,
    ):
        prepared = coordinator.preflight(intent.intent_id, actor=actor)
        submitted = coordinator.submit(intent.intent_id, actor=actor)
    assert prepared.verdict == "passed"
    assert submitted.status.candidate_digest is not None
    assert submitted.intent.last_preflight == prepared
    assert lowering.call_count == 1
    assert evaluation.call_count == 2


def test_replaced_payload_recomputes_and_changes_candidate(tmp_path: Path) -> None:
    _instance, coordinator, actor, intent = _setup(tmp_path)
    with patch.object(
        preflight_module, "lower_authoring", wraps=preflight_module.lower_authoring
    ) as lowering:
        first = coordinator.preflight(intent.intent_id, actor=actor)
        coordinator.replace_payload(
            intent.intent_id,
            actor=actor,
            payload=_change_set(
                _claim(qualifier="first", body="new evidence"), _claim(qualifier="second")
            ),
        )
        second = coordinator.preflight(intent.intent_id, actor=actor)
    assert lowering.call_count == 2
    assert first.certificate.candidate_tree_digest != second.certificate.candidate_tree_digest


def test_missing_generated_body_recovers_and_corruption_refuses(tmp_path: Path) -> None:
    instance, coordinator, actor, intent = _setup(tmp_path)
    with patch.object(
        preflight_module, "lower_authoring", wraps=preflight_module.lower_authoring
    ) as lowering:
        first = coordinator.preflight(intent.intent_id, actor=actor)
        entry = next(iter(prepared_lowering._caches[instance].values()))
        digest = entry.bodies[0]
        assert instance.body_store().erase(digest)
        assert coordinator.preflight(intent.intent_id, actor=actor) == first
        assert lowering.call_count == 2
        body_path = instance.body_store()._path(digest)
        body_path.write_bytes(b"corrupt")
        with pytest.raises(PlaybillCasError, match="content address"):
            coordinator.preflight(intent.intent_id, actor=actor)


def test_clear_or_eviction_rebuilds_identical_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, coordinator, actor, intent = _setup(tmp_path)
    first = coordinator.preflight(intent.intent_id, actor=actor)
    prepared_lowering._caches.pop(instance)
    with patch.object(
        preflight_module, "lower_authoring", wraps=preflight_module.lower_authoring
    ) as lowering:
        assert coordinator.preflight(intent.intent_id, actor=actor) == first
        monkeypatch.setattr(prepared_lowering, "MAX_ENTRIES", 1)
        another = coordinator.create(
            actor=actor, payload=_claim(qualifier="other"), canonical_timestamp=TIMESTAMP
        ).intent
        coordinator.preflight(another.intent_id, actor=actor)
        assert len(prepared_lowering._caches[instance]) == 1
        assert coordinator.preflight(intent.intent_id, actor=actor) == first
        assert lowering.call_count == 3


def test_receive_limits_and_actor_capabilities_are_not_reused(tmp_path: Path) -> None:
    instance, coordinator, actor, intent = _setup(tmp_path)
    assert coordinator.preflight(intent.intent_id, actor=actor).verdict == "passed"
    instance.bind_receive_limits(ProposalReceiveLimits(max_change_set_record_bytes=1))
    refused = coordinator.preflight(intent.intent_id, actor=actor)
    assert refused.verdict == "refused"
    instance.bind_receive_limits(ProposalReceiveLimits())
    with pytest.raises(ProposalAdmissionError, match="propose capability"):
        coordinator.submit(intent.intent_id, actor=actor.model_copy(update={"capabilities": ()}))


def test_working_selections_never_reuse_lowering(tmp_path: Path) -> None:
    instance, coordinator, actor, _intent = _setup(tmp_path)
    intent = coordinator.create(
        actor=actor,
        payload=_working_payload(occurrence_count=1),
        canonical_timestamp=TIMESTAMP,
    ).intent
    with patch.object(
        preflight_module, "lower_authoring", wraps=preflight_module.lower_authoring
    ) as lowering:
        coordinator.preflight(intent.intent_id, actor=actor)
        coordinator.preflight(intent.intent_id, actor=actor)
        assert lowering.call_count == 2


def test_changed_accepted_coordinate_recomputes_lowering(tmp_path: Path) -> None:
    instance, coordinator, actor, intent = _setup(tmp_path)
    with patch.object(
        preflight_module, "lower_authoring", wraps=preflight_module.lower_authoring
    ) as lowering:
        first = coordinator.preflight(intent.intent_id, actor=actor)
        tree = instance.tree_at(instance.accepted_coordinate().git_oid)
        subject = _shell("concurrent")
        tree[subject_path(subject.subject_kind, subject.subject_id)] = render_subject(subject)
        _accept_tree(
            instance,
            None,
            tree,
            timestamp="2026-08-21T12:01:00.000000Z",
            proposal_name="concurrent",
        )
        second = coordinator.preflight(intent.intent_id, actor=actor)
    assert lowering.call_count == 2
    assert first.certificate.accepted_coordinate != second.certificate.accepted_coordinate
    assert second.verdict == "passed"


def test_cached_containers_and_nested_input_values_are_not_shared(tmp_path: Path) -> None:
    instance, coordinator, actor, intent = _setup(tmp_path)
    first = preflight_module.compute_preflight(instance, intent=intent, actor=actor)
    assert first.lowered is not None
    first.lowered.proposed_tree.clear()
    first.lowered.resolved_authoring.clear()
    second = preflight_module.compute_preflight(instance, intent=intent, actor=actor)
    assert second.result == first.result
    assert second.lowered is not None and second.lowered.proposed_tree
    assert second.lowered.resolved_authoring
    # The payload commitment cannot be trusted as a cache key by itself.
    original = _claim(qualifier="nested")
    payload = original.model_copy(
        update={
            "statement": original.statement.model_copy(
                update={"object": LiteralClaimObject(value={"state": "ready"})}
            )
        }
    )
    changed = coordinator.create(actor=actor, payload=payload, canonical_timestamp=TIMESTAMP).intent
    third = preflight_module.compute_preflight(instance, intent=changed, actor=actor)
    value = changed.payload.statement.object.value
    assert isinstance(value, dict)
    value["state"] = "blocked"
    fourth = preflight_module.compute_preflight(instance, intent=changed, actor=actor)
    assert (
        third.result.certificate.candidate_tree_digest
        != fourth.result.certificate.candidate_tree_digest
    )
