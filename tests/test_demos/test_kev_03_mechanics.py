"""The 0.3 mechanics the KEV kits adopt: outcome forcing, exposure paths, pinned evidence.

These are kit-adoption tests, not engine tests — the engine behaviour is pinned
under ``tests/test_outcome_contracts`` and ``tests/test_storage``. What is
proved here is that the SHIPPED kev-reference/kev-triage configs actually wire
those mechanics up: that accepting a tracked triage decision refuses without a
resolution contract, that exposure resolves as a traversable CVE -> product ->
host -> service path, and that reference claims cite the KEV feed by pinned
revision rather than by whatever the feed happens to say today.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from tests.support.kev_golden import build_kev_triage_instance

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.errors import DataValidationError
from cruxible_core.governance.actors import GovernedActorContext
from cruxible_core.graph.types import EntityInstance
from cruxible_core.service import (
    service_add_entities,
    service_dereference_source_evidence,
    service_list_resolution_contracts,
    service_list_source_artifacts,
    service_open_resolution_contract,
    service_query_surface,
)

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
CHECK_AT = NOW + timedelta(days=7)
EXPIRES_AT = NOW + timedelta(days=30)

# Seeded fleet coordinates the demo script also uses: ASSET-1 (prod-web-01) runs
# Apache HTTP Server 2.4.49, which CVE-2021-41773 affects, and SVC-1 (Billing)
# depends on ASSET-1.
DEMO_CVE = "CVE-2021-41773"
DEMO_PRODUCT = "apache-http-server"
DEMO_HOST = "ASSET-1"
DEMO_SERVICE = "SVC-1"
KEV_CATALOG_ARTIFACT_ID = "cisa_kev_catalog"
KEV_CATALOG_TITLE = "CISA Known Exploited Vulnerabilities catalog snapshot"


@pytest.fixture(scope="module")
def kev_triage(tmp_path_factory: pytest.TempPathFactory) -> CruxibleInstance:
    """One reviewed composed KEV instance shared by this module."""
    return build_kev_triage_instance(tmp_path_factory.mktemp("kev-03"), stage="review")


def _actor(actor_id: str) -> GovernedActorContext:
    return GovernedActorContext(
        actor_type="human_user",
        actor_id=actor_id,
        org_id="org-kev",
        operation_id=f"op-{actor_id}",
        timestamp=NOW,
    )


def _write_decision(
    instance: CruxibleInstance,
    decision_id: str,
    *,
    status: str,
    outcome_tracking: str,
    actor_id: str,
) -> None:
    """Write the decision record.

    Only ``status`` and ``outcome_tracking`` vary between the proposal and the
    acceptance: a contract is pinned to the content it was opened against, so an
    acceptance that also edits the record is refused as ratifying a promise made
    about something else.
    """
    service_add_entities(
        instance,
        [
            EntityInstance(
                entity_type="TriageDecision",
                entity_id=decision_id,
                properties={
                    "decision_id": decision_id,
                    "title": f"Patch {DEMO_CVE} on the Billing fleet",
                    "status": status,
                    "outcome_tracking": outcome_tracking,
                    "cve_id": DEMO_CVE,
                    "service_id": DEMO_SERVICE,
                    "remediation_type": "patch",
                    "rationale": "KEV-listed RCE reachable from the internet-exposed web tier.",
                    "decided_by": "triager",
                },
            )
        ],
        actor_context=_actor(actor_id),
    )


def _open_contract(instance: CruxibleInstance, decision_id: str) -> Any:
    return service_open_resolution_contract(
        instance,
        entity_type="TriageDecision",
        entity_id=decision_id,
        description=f"No host still exposed to {DEMO_CVE} one week after the patch window",
        check_at=CHECK_AT,
        expires_at=EXPIRES_AT,
        measurement={
            "kind": "query",
            "query_name": "exposed_services",
            "params": {"cve_id": DEMO_CVE},
            "expect": {"max_count": 0},
        },
        actor_context=_actor("triager"),
    ).contract


# --- Outcome forcing -------------------------------------------------------


def test_accepting_a_tracked_decision_refuses_without_a_contract(
    kev_triage: CruxibleInstance,
) -> None:
    """The kit's guard, not a test config: acceptance demands a prior commitment."""
    _write_decision(
        kev_triage,
        "TD-refuse",
        status="proposed",
        outcome_tracking="required",
        actor_id="triager",
    )

    with pytest.raises(DataValidationError) as excinfo:
        _write_decision(
            kev_triage,
            "TD-refuse",
            status="accepted",
            outcome_tracking="required",
            actor_id="reviewer",
        )

    message = str(excinfo.value.errors)
    assert "no eligible resolution contract" in message
    # The refusal has to teach the flow, not just deny the write.
    assert "resolution contract" in message.lower()

    stored = kev_triage.load_graph().get_entity("TriageDecision", "TD-refuse")
    assert stored is not None
    assert stored.properties["status"] == "proposed"


def test_accepting_succeeds_once_a_contract_is_open(kev_triage: CruxibleInstance) -> None:
    _write_decision(
        kev_triage,
        "TD-accept",
        status="proposed",
        outcome_tracking="required",
        actor_id="triager",
    )
    contract = _open_contract(kev_triage, "TD-accept")

    _write_decision(
        kev_triage,
        "TD-accept",
        status="accepted",
        outcome_tracking="required",
        actor_id="reviewer",
    )

    stored = kev_triage.load_graph().get_entity("TriageDecision", "TD-accept")
    assert stored is not None
    assert stored.properties["status"] == "accepted"

    listed = service_list_resolution_contracts(kev_triage).items
    activated = [item for item in listed if item.contract.contract_id == contract.contract_id]
    assert len(activated) == 1
    # The acceptance CONSUMED the contract: it is now on the clock, not just filed.
    assert activated[0].activation is not None


def test_a_decision_with_no_measurable_outcome_opts_out_explicitly(
    kev_triage: CruxibleInstance,
) -> None:
    """not_applicable is an authorized choice, not an absence of one."""
    _write_decision(
        kev_triage,
        "TD-scope",
        status="proposed",
        outcome_tracking="not_applicable",
        actor_id="triager",
    )
    _write_decision(
        kev_triage,
        "TD-scope",
        status="accepted",
        outcome_tracking="not_applicable",
        actor_id="reviewer",
    )

    stored = kev_triage.load_graph().get_entity("TriageDecision", "TD-scope")
    assert stored is not None
    assert stored.properties["status"] == "accepted"


# --- Exposure as a path ----------------------------------------------------


def test_exposed_services_traverses_cve_to_product_to_host_to_service(
    kev_triage: CruxibleInstance,
) -> None:
    result = service_query_surface(kev_triage, "exposed_services", {"cve_id": DEMO_CVE})

    services = {row.result.entity_id for row in result.items}
    assert DEMO_SERVICE in services, services

    billing = next(row for row in result.items if row.result.entity_id == DEMO_SERVICE)
    # Every hop of the exposure claim is on the row, so the answer is auditable
    # rather than asserted.
    hops = {step.alias: step for step in billing.path}
    assert set(hops) == {"affected_product", "installed_product", "service_dependency"}
    assert hops["affected_product"].to_id == DEMO_PRODUCT
    assert hops["installed_product"].from_id == DEMO_HOST
    assert hops["service_dependency"].from_id == DEMO_SERVICE


def test_open_triage_queue_lists_only_undecided_decisions(
    kev_triage: CruxibleInstance,
) -> None:
    _write_decision(
        kev_triage,
        "TD-queued",
        status="proposed",
        outcome_tracking="required",
        actor_id="triager",
    )

    result = service_query_surface(kev_triage, "open_triage_queue", {})
    queued = {row.entity_id for row in result.items}

    assert "TD-queued" in queued
    # TD-accept settled in an earlier test in this module; the queue is for what
    # is still open, so a settled decision must have left it.
    assert "TD-accept" not in queued


# --- Pinned feed evidence --------------------------------------------------


def test_reference_claims_cite_the_kev_feed_by_pinned_revision(
    kev_triage: CruxibleInstance,
) -> None:
    listed = service_list_source_artifacts(kev_triage).items
    artifacts = {item.source_artifact_id for item in listed}
    assert KEV_CATALOG_ARTIFACT_ID in artifacts

    graph = kev_triage.load_graph()
    edges = [
        edge
        for edge in graph.iter_relationships()
        if edge.relationship_type == "vulnerability_affects_product" and edge.from_id == DEMO_CVE
    ]
    assert edges

    pinned = [
        ref
        for edge in edges
        for ref in edge.metadata.evidence.evidence_refs
        if ref.artifact_id == KEV_CATALOG_ARTIFACT_ID
    ]
    assert pinned, "KEV claims must cite the registered feed snapshot"
    # The pin is the whole point: an unpinned ref dereferences against whatever
    # revision is current, so it could never answer "has my evidence changed?".
    assert all(ref.artifact_revision_id == f"{KEV_CATALOG_ARTIFACT_ID}@1" for ref in pinned)

    resolved = service_dereference_source_evidence(
        kev_triage,
        source_artifact_id=KEV_CATALOG_ARTIFACT_ID,
        artifact_revision_id=pinned[0].artifact_revision_id,
        heading_path=list(pinned[0].metadata["heading_path"]),
        block_selector=pinned[0].metadata["block_selector"],
    )
    assert resolved.status == "available"
    assert resolved.revision_unpinned is False
    assert DEMO_CVE in (resolved.body or "")


def test_a_changed_feed_writes_a_new_revision_and_leaves_the_pin_readable(
    tmp_path: Path,
) -> None:
    """Re-registering the feed supersedes it; settled claims keep citing what they saw."""
    from cruxible_core.service.source_artifacts import service_register_source_artifact

    instance = build_kev_triage_instance(tmp_path / "drift", stage="local")

    changed = service_register_source_artifact(
        instance,
        source_content=(
            f"# {KEV_CATALOG_TITLE}\n\n## {DEMO_CVE}\n\n- Vendor: Apache\n- Product: revised\n"
        ),
        source_artifact_id=KEV_CATALOG_ARTIFACT_ID,
        source_retention="archive",
        actor_context=_actor("feed-refresh"),
    )
    assert changed.artifact_revision_id == f"{KEV_CATALOG_ARTIFACT_ID}@2"
    assert changed.supersedes == f"{KEV_CATALOG_ARTIFACT_ID}@1"

    original = service_dereference_source_evidence(
        instance,
        source_artifact_id=KEV_CATALOG_ARTIFACT_ID,
        artifact_revision_id=f"{KEV_CATALOG_ARTIFACT_ID}@1",
        heading_path=[KEV_CATALOG_TITLE, DEMO_CVE],
        block_selector="section",
    )
    assert original.status == "available"
    assert original.artifact_revision_id == f"{KEV_CATALOG_ARTIFACT_ID}@1"
    assert "Product: revised" not in (original.body or "")
