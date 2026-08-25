"""End-to-end proof that projection observation reuses the one coverage scanner."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from cruxible_client import contracts as api
from cruxible_client.authoring.workspace import observe_playbill_next_workspace_with_coverage
from cruxible_client.contracts.captures import DirectForeignSourceSelectionV1
from cruxible_client.contracts.semantic import ContentSpan
from cruxible_core.playbill.coverage import adapter
from cruxible_core.playbill.coverage.adapter import WorkingSourceObservationV1
from cruxible_core.playbill.coverage.contracts import (
    CoverageAccessProfileV1,
    CoverageCardBudgetV1,
)
from cruxible_core.playbill.coverage.indexes import CoverageScanBudgetV1
from cruxible_core.playbill.instance import PlaybillInstance
from cruxible_core.playbill.projection import AcceptedCoordinate
from cruxible_core.playbill.service.documents import PlaybillAcceptedCoordinate
from cruxible_core.service.playbill_claims import service_propose_playbill_claim
from cruxible_core.service.playbill_coverage import service_resolve_playbill_coverage
from cruxible_core.service.playbill_next import (
    PlaybillNextRequestV1,
    PlaybillNextWorkspaceObservationV1,
    service_playbill_next,
)
from tests.test_client.test_playbill_projection_repin import _workspace
from tests.test_playbill._knowledge_loop_support import TIMESTAMP, activate, authoring
from tests.test_playbill._support import initialize_local

SOURCE_CONTENT = b"# Runbook\nstatus: ready\nfooter\n"
NEEDLE = b"status: ready\n"
PROFILE = {
    "tag": "playbill-coverage-access-profile-v1",
    "profile_id": "integration-next",
    "permitted_access_classes": ["instance", "public"],
    "disclose_restricted_existence": True,
}


class _DirectCoverageClient:
    def __init__(self, instance: PlaybillInstance) -> None:
        self.instance = instance
        self.calls = 0

    def resolve_playbill_coverage(
        self, instance_id: str, **values: Any
    ) -> api.PlaybillCoverageResult:
        self.calls += 1
        result = service_resolve_playbill_coverage(
            self.instance,
            instance_id=instance_id,
            observations=tuple(
                WorkingSourceObservationV1.model_validate(value) for value in values["observations"]
            ),
            at=(
                None
                if values["at"] is None
                else PlaybillAcceptedCoordinate.model_validate(values["at"].model_dump())
            ),
            budget=CoverageCardBudgetV1.model_validate(values["budget"]),
            scan_budget=CoverageScanBudgetV1.model_validate(values["scan_budget"]),
        )
        return api.PlaybillCoverageResult(
            coordinate=api.PlaybillAcceptedCoordinate.model_validate(result.at.model_dump()),
            result=result.model_dump(mode="json"),
        )


def _foreign_world(root: Path, *, whole_source: bool = False):  # type: ignore[no-untyped-def]
    workspace = root / "workspace"
    workspace.mkdir()
    source = _workspace(workspace)
    source.write_bytes(SOURCE_CONTENT)
    instance_root = root / "instance"
    instance_root.mkdir()
    instance, owner = initialize_local(instance_root)
    stored = instance.body_store().store(SOURCE_CONTENT)
    start = 0 if whole_source else SOURCE_CONTENT.index(NEEDLE)
    end = len(SOURCE_CONTENT) if whole_source else start + len(NEEDLE)
    proposal = service_propose_playbill_claim(
        instance,
        authoring=authoring("wi-42", "ready", with_claim_type=True).model_copy(
            update={
                "source_selection": DirectForeignSourceSelectionV1(
                    logical_source_identity="corpus.runbook",
                    span=ContentSpan(content_digest=stored.digest, start_byte=start, end_byte=end),
                    media_type="text/markdown",
                )
            }
        ),
        actor_id="owner",
        proposal_name="scanner-cited-runbook",
        timestamp=TIMESTAMP,
    )
    activate(instance, owner, proposal, sequence=1)
    return instance, source, workspace


def _result(instance: PlaybillInstance, client: _DirectCoverageClient, root: Path):  # type: ignore[no-untyped-def]
    coordinate = api.PlaybillAcceptedCoordinate.model_validate(
        AcceptedCoordinate.from_internal(instance.accepted_coordinate()).model_dump()
    )
    observed, pinned = observe_playbill_next_workspace_with_coverage(
        client,
        instance.descriptor.instance_id,
        root,
        coordinate=coordinate,
        access_profile=PROFILE,
    )
    assert pinned == coordinate
    result = service_playbill_next(
        instance,
        request=PlaybillNextRequestV1(
            at=AcceptedCoordinate.model_validate(pinned.model_dump()),
            evaluation_time=datetime(2026, 8, 16, 21, tzinfo=UTC),
            access_profile=CoverageAccessProfileV1.model_validate(PROFILE),
            workspace_observation=PlaybillNextWorkspaceObservationV1.model_validate(observed),
        ),
    )
    return observed, result


def test_one_existing_scanner_handles_relocation_duplicate_and_genuine_span_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, source, workspace = _foreign_world(tmp_path)
    client = _DirectCoverageClient(instance)
    original = adapter.build_working_occurrence_overlay
    invocations = 0

    def counted(*args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        nonlocal invocations
        invocations += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(adapter, "build_working_occurrence_overlay", counted)

    initial, first = _result(instance, client, workspace)
    assert all(item.reason != "citation_drifted" for item in first.items)
    original_occurrence = initial["source_observations"][0]["occurrences"][0]

    source.write_bytes(b"Unrelated heading\n" + SOURCE_CONTENT)
    moved, second = _result(instance, client, workspace)
    moved_occurrence = moved["source_observations"][0]["occurrences"][0]
    assert original_occurrence["identity_digest"] == moved_occurrence["identity_digest"]
    assert all(item.reason != "citation_drifted" for item in second.items)

    source.write_bytes(SOURCE_CONTENT.replace(NEEDLE, NEEDLE + NEEDLE))
    duplicated, ambiguous = _result(instance, client, workspace)
    assert duplicated["source_observations"][0]["scan_complete"] is False
    assert any(item.reason == "citation_source_unobserved" for item in ambiguous.items)
    assert all(item.reason != "citation_drifted" for item in ambiguous.items)

    source.write_bytes(SOURCE_CONTENT.replace(NEEDLE, b"status: blocked\n"))
    changed, drifted = _result(instance, client, workspace)
    assert changed["source_observations"][0]["scan_complete"] is True
    assert any(item.reason == "citation_drifted" for item in drifted.items)
    assert invocations == client.calls == 4


def test_whole_file_citation_drifts_when_any_uncited_prefix_changes(tmp_path: Path) -> None:
    instance, source, workspace = _foreign_world(tmp_path, whole_source=True)
    client = _DirectCoverageClient(instance)
    source.write_bytes(b"Unrelated heading\n" + SOURCE_CONTENT)

    _observed, result = _result(instance, client, workspace)

    assert any(item.reason == "citation_drifted" for item in result.items)
