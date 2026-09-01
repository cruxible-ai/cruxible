"""End-to-end authority boundary for reference-shaped literal curation."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cruxible_core.playbill.actor_context import GovernedActorContext
from cruxible_core.playbill.coverage.contracts import CoverageAccessProfileV1
from cruxible_core.service.playbill_curation import (
    PlaybillCurationListRequestV1,
    service_list_playbill_curation,
)
from tests.test_playbill.test_claim_type_migrations import _accepted_affects_package_world

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)


def test_affects_package_literal_is_operational_curation_only(tmp_path: Path) -> None:
    instance, claim_id, _owner = _accepted_affects_package_world(tmp_path)
    accepted_before = instance.accepted_coordinate()
    tree_before = instance.tree_at(accepted_before.git_oid)

    result = service_list_playbill_curation(
        instance,
        request=PlaybillCurationListRequestV1(
            evaluation_time=NOW,
            access_profile=CoverageAccessProfileV1(profile_id="reference-literal-test"),
        ),
        actor_context=GovernedActorContext(
            actor_type="human_user",
            actor_id="curator",
            org_id="org-test",
            operation_id="op-reference-literal",
            timestamp=NOW,
        ),
    )

    matches = tuple(
        item
        for item in result.items
        if item.pattern_kind == "playbill.curation.literal_subject_reference.v1"
        and item.subject.qualified == f"Claim:{claim_id}"
    )
    assert len(matches) == 1
    assert matches[0].detail["literal_value"] == "demo-package"
    assert matches[0].detail["matching_subject_kinds"] == ["package"]
    assert instance.accepted_coordinate() == accepted_before
    assert instance.tree_at(accepted_before.git_oid) == tree_before
