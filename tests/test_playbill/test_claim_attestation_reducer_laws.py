"""Killing tests for the attestation door's immutable reducer laws."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from cruxible_client.contracts.artifacts import ArtifactLifecycle
from cruxible_client.contracts.captures import build_coordinator_self_source_capture
from cruxible_client.contracts.claims import (
    ClaimArtifactV3,
    ClaimRetirementAttributionV1,
    claim_artifact_digest,
    claim_path,
    parse_claim,
)
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.service.playbill_claim_attestations import service_append_claim_attestation
from cruxible_core.service.playbill_claims import (
    CaptureAdmissionAccountV1,
    _claim_law_evidence,
)
from cruxible_core.service.playbill_next import (
    PlaybillNextRequestV2,
    _attestation_claim_lineage,
    _AttestationLineageArtifact,
    service_playbill_next,
)
from tests.test_playbill.test_claim_attestation_service import RECORDED_AT, _request
from tests.test_playbill.test_claim_type_migrations import _accepted_claim_world


def _access() -> CoverageAccessProfileV1:
    return CoverageAccessProfileV1(
        profile_id="attestation-reducer-laws",
        permitted_access_classes=("instance", "public"),
    )


def _door_world(
    root: Path,
    *,
    stance: str = "contradict",
    capture_count: int = 1,
):  # type: ignore[no-untyped-def]
    instance, claim_id, owner = _accepted_claim_world(root)
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    captures = tuple(
        sorted(
            (
                build_coordinator_self_source_capture(
                    store=instance.body_store(),
                    actor_id="owner",
                    claim_id=claim_id,
                    body=f"new observation {index}\n".encode(),
                    observed_at=RECORDED_AT,
                    accepted_coordinate=coordinate,
                ).capture_digest
                for index in range(capture_count)
            ),
            key=lambda item: item.encode("ascii"),
        )
    )
    service_append_claim_attestation(
        instance,
        request=_request(
            instance,
            owner,
            claim_id,
            root,
            basis="new_capture",
            stance=stance,
            captures=captures,
        ),
        actor_id="owner",
        recorded_at=RECORDED_AT,
    )
    return instance, claim_id, owner, captures


def _door_rows(instance):  # type: ignore[no-untyped-def]
    reasons = {
        "claim_contradicting_evidence_available",
        "claim_new_evidence_supporting",
        "claim_new_evidence_unreviewed",
    }
    return tuple(
        item
        for item in service_playbill_next(
            instance,
            request=PlaybillNextRequestV2(
                evaluation_time=RECORDED_AT,
                access_profile=_access(),
            ),
        ).items
        if item.reason in reasons
    )


def _account(capture: str, status: str, suffix: str) -> CaptureAdmissionAccountV1:
    return CaptureAdmissionAccountV1(
        citation_id="sha256:" + suffix * 64,
        capture_digest=capture,
        citation_role="evidence" if status != "not_evidence" else "copy",
        citation_origin="independent",
        capture_contract_identity="CaptureContract:test",
        capture_contract_digest="sha256:" + "d" * 64,
        status=status,  # type: ignore[arg-type]
    )


def test_mechanical_rederivation_inherits_authority_until_authored_successor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, claim_id, _owner, captures = _door_world(tmp_path)
    capture = captures[0]
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    base = parse_claim(tree[claim_path(claim_id)], path=claim_path(claim_id))
    mechanical = base.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=claim_artifact_digest(base).tagged,
            )
        }
    )
    authored = mechanical.model_copy(
        update={
            "lifecycle": ArtifactLifecycle(
                predecessor_digest=claim_artifact_digest(mechanical).tagged,
            )
        }
    )
    digests = ("sha256:" + "1" * 64, "sha256:" + "2" * 64, "sha256:" + "3" * 64)
    artifacts = tuple(
        _AttestationLineageArtifact(claim=claim, artifact_digest=digest, tree=tree)
        for claim, digest in zip((base, mechanical, authored), digests, strict=True)
    )
    law = _claim_law_evidence(
        instance,
        path=claim_path(claim_id),
        at=instance.accepted_coordinate(),
    )
    stage = 1

    monkeypatch.setattr(
        "cruxible_core.service.playbill_next._attestation_claim_lineage",
        lambda *_args, **_kwargs: (artifacts[:stage], False),
    )
    monkeypatch.setattr(
        "cruxible_core.service.playbill_next._claim_law_evidence_by_artifact_index",
        lambda *_args, **_kwargs: {(claim_path(claim_id), digest): law for digest in digests},
    )
    monkeypatch.setattr(
        "cruxible_core.service.playbill_next._is_claim_type_rederivation",
        lambda claim, **_kwargs: claim is mechanical,
    )

    def accounts(_instance, *, claim, tree, law):  # type: ignore[no-untyped-def]
        del tree, law
        return (
            _account(
                capture,
                "not_admitted" if claim is base else "admitted",
                "a" if claim is base else "b",
            ),
        )

    monkeypatch.setattr("cruxible_core.service.playbill_next._claim_admission_accounts", accounts)
    assert len(_door_rows(instance)) == 1
    stage = 2
    assert len(_door_rows(instance)) == 1
    stage = 3
    assert _door_rows(instance) == ()


@pytest.mark.parametrize(
    ("statuses", "resolved"),
    [
        (("admitted", "not_evidence"), True),
        (("not_admitted", "not_evidence"), False),
        (("not_admitted", "not_admitted"), False),
    ],
)
def test_capture_resolution_is_admitted_if_any_and_never_copy_if_any(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    statuses: tuple[str, str],
    resolved: bool,
) -> None:
    instance, claim_id, _owner, captures = _door_world(tmp_path)
    capture = captures[0]
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    claim = parse_claim(tree[claim_path(claim_id)], path=claim_path(claim_id))
    digest = claim_artifact_digest(claim).tagged
    artifact = _AttestationLineageArtifact(claim=claim, artifact_digest=digest, tree=tree)
    law = _claim_law_evidence(
        instance,
        path=claim_path(claim_id),
        at=instance.accepted_coordinate(),
    )
    monkeypatch.setattr(
        "cruxible_core.service.playbill_next._attestation_claim_lineage",
        lambda *_args, **_kwargs: ((artifact,), False),
    )
    monkeypatch.setattr(
        "cruxible_core.service.playbill_next._claim_law_evidence_by_artifact_index",
        lambda *_args, **_kwargs: {(claim_path(claim_id), digest): law},
    )
    monkeypatch.setattr(
        "cruxible_core.service.playbill_next._claim_admission_accounts",
        lambda *_args, **_kwargs: tuple(
            _account(capture, status, suffix)
            for status, suffix in zip(statuses, ("a", "b"), strict=True)
        ),
    )
    assert (_door_rows(instance) == ()) is resolved


def test_multi_capture_partial_resolution_retains_only_unadmitted_membership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, claim_id, _owner, captures = _door_world(tmp_path, capture_count=2)
    admitted_capture, retained_capture = captures
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    claim = parse_claim(tree[claim_path(claim_id)], path=claim_path(claim_id))
    digest = claim_artifact_digest(claim).tagged
    artifact = _AttestationLineageArtifact(claim=claim, artifact_digest=digest, tree=tree)
    law = _claim_law_evidence(
        instance,
        path=claim_path(claim_id),
        at=instance.accepted_coordinate(),
    )
    monkeypatch.setattr(
        "cruxible_core.service.playbill_next._attestation_claim_lineage",
        lambda *_args, **_kwargs: ((artifact,), False),
    )
    monkeypatch.setattr(
        "cruxible_core.service.playbill_next._claim_law_evidence_by_artifact_index",
        lambda *_args, **_kwargs: {(claim_path(claim_id), digest): law},
    )
    monkeypatch.setattr(
        "cruxible_core.service.playbill_next._claim_admission_accounts",
        lambda *_args, **_kwargs: (_account(admitted_capture, "admitted", "a"),),
    )
    rows = _door_rows(instance)
    assert [row.detail["capture_digest"] for row in rows] == [retained_capture]


def test_unsure_and_later_events_never_erase_new_capture_memberships(tmp_path: Path) -> None:
    instance, claim_id, owner, first = _door_world(tmp_path, stance="unsure")
    coordinate = AcceptedCoordinate.from_internal(instance.accepted_coordinate())
    second = build_coordinator_self_source_capture(
        store=instance.body_store(),
        actor_id="owner",
        claim_id=claim_id,
        body=b"later observation\n",
        observed_at=RECORDED_AT,
        accepted_coordinate=coordinate,
    )
    request = _request(
        instance,
        owner,
        claim_id,
        tmp_path,
        basis="new_capture",
        stance="support",
        captures=(second.capture_digest,),
    )
    service_append_claim_attestation(
        instance,
        request=request,
        actor_id="owner",
        recorded_at=RECORDED_AT,
    )
    rows = _door_rows(instance)
    assert {row.detail["capture_digest"] for row in rows} == {first[0], second.capture_digest}
    assert {row.reason for row in rows} == {
        "claim_new_evidence_supporting",
        "claim_new_evidence_unreviewed",
    }


def test_terminal_retirement_resolves_and_incomplete_lineage_retains_typed_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance, claim_id, _owner, _captures = _door_world(tmp_path)
    tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    claim = parse_claim(tree[claim_path(claim_id)], path=claim_path(claim_id))
    digest = claim_artifact_digest(claim).tagged
    live = _AttestationLineageArtifact(claim=claim, artifact_digest=digest, tree=tree)
    monkeypatch.setattr(
        "cruxible_core.service.playbill_next._attestation_claim_lineage",
        lambda *_args, **_kwargs: ((live,), True),
    )
    monkeypatch.setattr(
        "cruxible_core.service.playbill_next._claim_law_evidence_by_artifact_index",
        lambda *_args, **_kwargs: {},
    )
    incomplete = _door_rows(instance)
    assert len(incomplete) == 1
    assert incomplete[0].detail["lineage_status"] == "incomplete"

    retired = ClaimArtifactV3(
        identity=claim.identity,
        statement=claim.statement,
        backing=claim.backing,
        pins=claim.pins,
        lifecycle=ArtifactLifecycle(state="retired", predecessor_digest=digest),
        retirement=ClaimRetirementAttributionV1(reason="was-rescinded"),
    )
    retired_artifact = _AttestationLineageArtifact(
        claim=retired,
        artifact_digest=claim_artifact_digest(retired).tagged,
        tree=tree,
    )
    monkeypatch.setattr(
        "cruxible_core.service.playbill_next._attestation_claim_lineage",
        lambda *_args, **_kwargs: ((live, retired_artifact), False),
    )
    assert _door_rows(instance) == ()


@dataclass(frozen=True)
class _Generation:
    oid: str


class _LineageInstance:
    def __init__(
        self,
        trees: dict[str, dict[str, bytes]],
        history: tuple[_Generation, ...],
    ) -> None:
        self._trees = trees
        self._history = history

    def accepted_history(self) -> tuple[_Generation, ...]:
        return self._history

    def tree_at(self, oid: str) -> dict[str, bytes]:
        return self._trees[oid]


def test_lineage_walk_hits_256_cap_and_reports_incomplete(tmp_path: Path) -> None:
    instance, claim_id, _owner = _accepted_claim_world(tmp_path)
    path = claim_path(claim_id)
    base_tree = instance.tree_at(instance.accepted_coordinate().git_oid)
    claim = parse_claim(base_tree[path], path=path)
    trees: dict[str, dict[str, bytes]] = {}
    history: list[_Generation] = []
    from cruxible_client.contracts.claims import render_claim

    for sequence in range(258):
        oid = f"{sequence:040x}"
        trees[oid] = {path: render_claim(claim)}
        history.append(_Generation(oid=oid))
        claim = claim.model_copy(
            update={
                "lifecycle": ArtifactLifecycle(
                    predecessor_digest=claim_artifact_digest(claim).tagged,
                )
            }
        )
    target = history[-1]
    coordinate = instance.accepted_coordinate().model_copy(update={"git_oid": target.oid})
    lineage, incomplete = _attestation_claim_lineage(
        _LineageInstance(trees, tuple(history)),  # type: ignore[arg-type]
        coordinate=coordinate,
        claim_identity=claim_id,
    )
    assert len(lineage) == 257
    assert incomplete is True
