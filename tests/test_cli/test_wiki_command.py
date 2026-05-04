"""Tests for wiki rendering CLI."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from cruxible_core.cli.instance import CruxibleInstance
from cruxible_core.feedback.types import FeedbackRecord, OutcomeRecord
from cruxible_core.graph.entity_graph import EntityGraph
from cruxible_core.graph.types import EntityInstance, RelationshipInstance
from cruxible_core.group.signature import compute_group_signature
from cruxible_core.group.types import CandidateGroup, CandidateMember
from cruxible_core.provider.types import ExecutionTrace
from cruxible_core.receipt.builder import ReceiptBuilder
from cruxible_core.wiki import WikiOptions, render_wiki
from cruxible_core.wiki.generator import SubjectRef, _humanize

WIKI_TEST_CONFIG = """\
version: "1.0"
name: kev_subject_wiki
description: Deterministic wiki test world
kind: world_model

contracts:
  exposure_input:
    fields:
      vulnerability_id:
        type: string
  asset_records:
    fields:
      items:
        type: json
  exposure_output:
    fields:
      assessments:
        type: json

entity_types:
  Asset:
    description: Internal asset tracked for remediation.
    properties:
      asset_id:
        type: string
        primary_key: true
      name:
        type: string
      environment:
        type: string
      internet_exposed:
        type: bool
      criticality:
        type: string
  Vulnerability:
    description: Public vulnerability tracked for triage.
    properties:
      vulnerability_id:
        type: string
        primary_key: true
      cve_id:
        type: string
      title:
        type: string
  BusinessService:
    description: Business service supported by internal assets.
    properties:
      service_id:
        type: string
        primary_key: true
      name:
        type: string
  Owner:
    description: Team responsible for remediation.
    properties:
      owner_id:
        type: string
        primary_key: true
      name:
        type: string

relationships:
  - name: asset_affected_by_vulnerability
    from: Asset
    to: Vulnerability
    description: Asset currently associated with the vulnerability.
    matching: {}
  - name: asset_requires_action_for_vulnerability
    from: Asset
    to: Vulnerability
    description: Asset currently requires remediation action.
    matching: {}
  - name: service_impacted_by_vulnerability
    from: BusinessService
    to: Vulnerability
    description: Service currently considered exposed through affected infrastructure.
    matching: {}
  - name: asset_supports_service
    from: Asset
    to: BusinessService
    description: Asset supports the business service.
  - name: asset_owned_by
    from: Asset
    to: Owner
    description: Asset is owned by the responsible team.

named_queries:
  assets_for_vulnerability:
    description: Find assets currently associated with a vulnerability.
    entry_point: Vulnerability
    traversal:
      - relationship: asset_affected_by_vulnerability
        direction: incoming
      - relationship: asset_supports_service
        direction: outgoing
    returns: list[Asset]

providers:
  load_software_inventory:
    kind: function
    description: Load software inventory facts for the asset.
    contract_in: exposure_input
    contract_out: asset_records
    ref: tests.providers:load_software_inventory
    version: "1.0.0"
    deterministic: true
    runtime: python
  assess_asset_exposure:
    kind: function
    description: Assess whether an asset currently requires action.
    contract_in: exposure_input
    contract_out: exposure_output
    ref: tests.providers:assess_asset_exposure
    version: "2.1.0"
    deterministic: true
    runtime: python

workflows:
  propose_asset_exposure:
    description: Build remediation conclusions for affected assets.
    canonical: false
    contract_in: exposure_input
    returns: exposure_output
    steps:
      - id: load_inventory
        provider: load_software_inventory
        input:
          vulnerability_id: $input.vulnerability_id
        as: inventory
      - id: assess_exposure
        provider: assess_asset_exposure
        input:
          vulnerability_id: $input.vulnerability_id
          inventory: $steps.inventory
        as: exposure
"""

LAYERED_REFERENCE_CONFIG = """\
version: "1.0"
name: reference_catalog
description: Upstream reference catalog.
kind: world_model

contracts:
  empty_input:
    fields: {}
  empty_output:
    fields: {}

entity_types:
  ReferenceItem:
    description: Reference item from the upstream catalog.
    properties:
      item_id:
        type: string
        primary_key: true
      name:
        type: string
  ReferenceParent:
    description: Parent reference object.
    properties:
      parent_id:
        type: string
        primary_key: true
      name:
        type: string

relationships:
  - name: reference_parent
    from: ReferenceItem
    to: ReferenceParent
    description: Known parent relationship.

named_queries:
  reference_lookup:
    description: Upstream query used by local workflows.
    entry_point: ReferenceItem
    traversal:
      - relationship: reference_parent
        direction: outgoing
    returns: list[ReferenceParent]

providers:
  upstream_provider:
    kind: function
    description: Upstream provider that should stay out of local scope unless used.
    contract_in: empty_input
    contract_out: empty_output
    ref: tests.providers:upstream_provider
    version: "1.0.0"

workflows:
  upstream_workflow:
    description: Upstream workflow that should stay out of local scope unless used.
    canonical: false
    contract_in: empty_input
    returns: empty_output
    steps:
      - id: call_upstream
        provider: upstream_provider
        input: {}
        as: upstream
"""

LAYERED_LOCAL_CONFIG_TEMPLATE = """\
version: "1.0"
name: local_projection
description: Local world model overlay.
kind: world_model
extends: __BASE_PATH__

entity_types:
  LocalSubject:
    description: Locally owned subject.
    properties:
      local_id:
        type: string
        primary_key: true
      name:
        type: string

relationships:
  - name: local_subject_references_item
    from: LocalSubject
    to: ReferenceItem
    description: Local subject points at an upstream reference item.
    matching: {}

named_queries:
  local_review:
    description: Local query surface.
    entry_point: LocalSubject
    traversal:
      - relationship: local_subject_references_item
        direction: outgoing
    returns: list[ReferenceItem]

providers:
  local_provider:
    kind: function
    description: Local provider surface.
    contract_in: empty_input
    contract_out: empty_output
    ref: tests.providers:local_provider
    version: "1.0.0"

workflows:
  local_workflow:
    description: Local workflow surface.
    canonical: false
    contract_in: empty_input
    returns: empty_output
    steps:
      - id: run_reference_query
        query: reference_lookup
        params: {}
        as: reference
      - id: call_local
        provider: local_provider
        input: {}
        as: local
"""


def _trace_timing() -> dict[str, object]:
    now = datetime.now(timezone.utc)
    return {"started_at": now, "finished_at": now, "duration_ms": 0.0}


def _assert_no_broken_wiki_links(wiki_root: Path) -> None:
    missing: list[str] = []
    for page in wiki_root.rglob("*.md"):
        for match in re.finditer(r"\[[^\]]+\]\(([^)#]+)(?:#[^)]+)?\)", page.read_text()):
            href = match.group(1)
            if "://" in href or href.startswith("mailto:"):
                continue
            target = (page.parent / href).resolve()
            try:
                target.relative_to(wiki_root.resolve())
            except ValueError:
                continue
            if not target.exists():
                missing.append(f"{page.relative_to(wiki_root)} -> {href}")
    assert not missing


def _build_test_graph() -> EntityGraph:
    graph = EntityGraph()
    graph.add_entity(
        EntityInstance(
            entity_type="Asset",
            entity_id="prod-web-01",
            properties={
                "asset_id": "prod-web-01",
                "name": "prod-web-01",
                "environment": "production",
                "internet_exposed": True,
                "criticality": "critical",
            },
        )
    )
    graph.add_entity(
        EntityInstance(
            entity_type="Vulnerability",
            entity_id="CVE-2021-41773",
            properties={
                "vulnerability_id": "CVE-2021-41773",
                "cve_id": "CVE-2021-41773",
                "title": "Apache HTTP Server 2.4.49 path traversal",
            },
        )
    )
    graph.add_entity(
        EntityInstance(
            entity_type="BusinessService",
            entity_id="svc-billing",
            properties={"service_id": "svc-billing", "name": "Billing"},
        )
    )
    graph.add_entity(
        EntityInstance(
            entity_type="Owner",
            entity_id="owner-seceng",
            properties={"owner_id": "owner-seceng", "name": "Security Engineering"},
        )
    )
    graph.add_relationship(
        RelationshipInstance(
            relationship_type="asset_affected_by_vulnerability",
            from_type="Asset",
            from_id="prod-web-01",
            to_type="Vulnerability",
            to_id="CVE-2021-41773",
            properties={"review_status": "accepted", "basis": "inventory_match"},
        )
    )
    graph.add_relationship(
        RelationshipInstance(
            relationship_type="asset_requires_action_for_vulnerability",
            from_type="Asset",
            from_id="prod-web-01",
            to_type="Vulnerability",
            to_id="CVE-2021-41773",
            properties={"review_status": "accepted", "priority": "urgent"},
        )
    )
    graph.add_relationship(
        RelationshipInstance(
            relationship_type="service_impacted_by_vulnerability",
            from_type="BusinessService",
            from_id="svc-billing",
            to_type="Vulnerability",
            to_id="CVE-2021-41773",
            properties={"review_status": "accepted", "basis": "dependency_chain"},
        )
    )
    graph.add_relationship(
        RelationshipInstance(
            relationship_type="asset_supports_service",
            from_type="Asset",
            from_id="prod-web-01",
            to_type="BusinessService",
            to_id="svc-billing",
            properties={"source": "cmdb"},
        )
    )
    graph.add_relationship(
        RelationshipInstance(
            relationship_type="asset_owned_by",
            from_type="Asset",
            from_id="prod-web-01",
            to_type="Owner",
            to_id="owner-seceng",
            properties={"source": "overlay_seed"},
        )
    )
    return graph


def _write_layered_wiki_project(project: Path) -> CruxibleInstance:
    project.mkdir()
    base = project / "reference.yaml"
    overlay = project / "config.yaml"
    base.write_text(LAYERED_REFERENCE_CONFIG)
    overlay.write_text(LAYERED_LOCAL_CONFIG_TEMPLATE.replace("__BASE_PATH__", str(base)))
    return CruxibleInstance.init(project, "config.yaml")


def _build_layered_graph() -> EntityGraph:
    graph = EntityGraph()
    graph.add_entity(
        EntityInstance(
            entity_type="LocalSubject",
            entity_id="local-1",
            properties={"local_id": "local-1", "name": "Local One"},
        )
    )
    graph.add_entity(
        EntityInstance(
            entity_type="ReferenceItem",
            entity_id="ref-1",
            properties={"item_id": "ref-1", "name": "Reference One"},
        )
    )
    graph.add_entity(
        EntityInstance(
            entity_type="ReferenceItem",
            entity_id="ref-2",
            properties={"item_id": "ref-2", "name": "Reference Two"},
        )
    )
    graph.add_entity(
        EntityInstance(
            entity_type="ReferenceParent",
            entity_id="parent-1",
            properties={"parent_id": "parent-1", "name": "Parent One"},
        )
    )
    graph.add_relationship(
        RelationshipInstance(
            relationship_type="local_subject_references_item",
            from_type="LocalSubject",
            from_id="local-1",
            to_type="ReferenceItem",
            to_id="ref-1",
            properties={"basis": "local-review"},
        )
    )
    graph.add_relationship(
        RelationshipInstance(
            relationship_type="reference_parent",
            from_type="ReferenceItem",
            from_id="ref-1",
            to_type="ReferenceParent",
            to_id="parent-1",
            properties={"source": "reference-catalog"},
        )
    )
    return graph


def _seed_reference_only_receipt(instance: CruxibleInstance) -> str:
    builder = ReceiptBuilder(
        query_name="reference_lookup",
        parameters={"item_id": "ref-2"},
    )
    lookup = builder.record_entity_lookup("ReferenceItem", "ref-2")
    builder.record_results(
        [{"entity_type": "ReferenceItem", "entity_id": "ref-2"}],
        parent_ids=[lookup],
    )
    receipt = builder.build(
        results=[{"entity_type": "ReferenceItem", "entity_id": "ref-2"}]
    )

    receipt_store = instance.get_receipt_store()
    try:
        receipt_store.save_receipt(receipt)
    finally:
        receipt_store.close()
    return receipt.receipt_id


def _seed_receipts_and_traces(instance: CruxibleInstance) -> tuple[str, str, str]:
    query_builder = ReceiptBuilder(
        query_name="assets_for_vulnerability",
        parameters={"vulnerability_id": "CVE-2021-41773"},
    )
    vulnerability_lookup = query_builder.record_entity_lookup("Vulnerability", "CVE-2021-41773")
    asset_step = query_builder.record_traversal(
        "Vulnerability",
        "CVE-2021-41773",
        "Asset",
        "prod-web-01",
        "asset_affected_by_vulnerability",
        {"review_status": "accepted"},
        parent_id=vulnerability_lookup,
    )
    query_builder.record_filter({"environment": "production"}, True, asset_step)
    service_step = query_builder.record_traversal(
        "Asset",
        "prod-web-01",
        "BusinessService",
        "svc-billing",
        "asset_supports_service",
        {"source": "cmdb"},
        parent_id=asset_step,
    )
    query_builder.record_results(
        [
            {"entity_type": "Asset", "entity_id": "prod-web-01"},
            {"entity_type": "BusinessService", "entity_id": "svc-billing"},
        ],
        parent_ids=[asset_step, service_step],
    )
    query_receipt = query_builder.build(
        results=[
            {"entity_type": "Asset", "entity_id": "prod-web-01"},
            {"entity_type": "BusinessService", "entity_id": "svc-billing"},
        ]
    )

    workflow_builder = ReceiptBuilder(
        query_name="propose_asset_exposure",
        parameters={"vulnerability_id": "CVE-2021-41773"},
        operation_type="workflow",
    )
    load_step = workflow_builder.record_plan_step(
        "load_inventory",
        "provider",
        detail={
            "provider_name": "load_software_inventory",
            "provider_version": "1.0.0",
            "trace_id": "TRC-load-001",
        },
    )
    assess_step = workflow_builder.record_plan_step(
        "assess_exposure",
        "provider",
        detail={
            "provider_name": "assess_asset_exposure",
            "provider_version": "2.1.0",
            "trace_id": "TRC-exposure-001",
        },
        parent_id=load_step,
    )
    workflow_builder.record_entity_lookup("Asset", "prod-web-01", parent_id=assess_step)
    workflow_builder.record_entity_lookup("Vulnerability", "CVE-2021-41773", parent_id=assess_step)
    workflow_builder.record_relationship_write(
        "Asset",
        "prod-web-01",
        "Vulnerability",
        "CVE-2021-41773",
        "asset_requires_action_for_vulnerability",
        is_update=False,
        parent_id=assess_step,
    )
    workflow_builder.record_relationship_write(
        "BusinessService",
        "svc-billing",
        "Vulnerability",
        "CVE-2021-41773",
        "service_impacted_by_vulnerability",
        is_update=False,
        parent_id=assess_step,
    )
    workflow_builder.record_results(
        [{"entity_type": "Asset", "entity_id": "prod-web-01"}],
        parent_ids=[assess_step],
    )
    workflow_receipt = workflow_builder.build(
        results=[{"entity_type": "Asset", "entity_id": "prod-web-01"}]
    )

    receipt_store = instance.get_receipt_store()
    try:
        receipt_store.save_receipt(query_receipt)
        receipt_store.save_receipt(workflow_receipt)
        receipt_store.save_trace(
            ExecutionTrace(
                trace_id="TRC-load-001",
                workflow_name="propose_asset_exposure",
                step_id="load_inventory",
                provider_name="load_software_inventory",
                provider_version="1.0.0",
                provider_ref="tests.providers:load_software_inventory",
                runtime="python",
                deterministic=True,
                side_effects=False,
                artifact_name="inventory-snapshot.json",
                artifact_sha256="abc123",
                input_payload={"vulnerability_id": "CVE-2021-41773"},
                output_payload={"items": [{"asset_id": "prod-web-01"}]},
                status="success",
                **_trace_timing(),
            )
        )
        receipt_store.save_trace(
            ExecutionTrace(
                trace_id="TRC-exposure-001",
                workflow_name="propose_asset_exposure",
                step_id="assess_exposure",
                provider_name="assess_asset_exposure",
                provider_version="2.1.0",
                provider_ref="tests.providers:assess_asset_exposure",
                runtime="python",
                deterministic=True,
                side_effects=False,
                input_payload={
                    "vulnerability_id": "CVE-2021-41773",
                    "inventory": {"asset_id": "prod-web-01"},
                },
                output_payload={"requires_action": True, "asset_id": "prod-web-01"},
                status="success",
                **_trace_timing(),
            )
        )
    finally:
        receipt_store.close()

    return query_receipt.receipt_id, workflow_receipt.receipt_id, "TRC-exposure-001"


def _seed_review_history(
    instance: CruxibleInstance,
    workflow_receipt_id: str,
    trace_id: str,
) -> str:
    signature = compute_group_signature(
        "asset_requires_action_for_vulnerability",
        {"asset_id": "prod-web-01", "vulnerability_id": "CVE-2021-41773"},
    )
    created_at = datetime.now(timezone.utc)
    group_store = instance.get_group_store()
    try:
        with group_store.transaction():
            resolution_id = group_store.save_resolution(
                "asset_requires_action_for_vulnerability",
                signature,
                "approve",
                "Confirmed during patch planning",
                "prod-web-01 requires action for CVE-2021-41773",
                {"asset_id": "prod-web-01", "vulnerability_id": "CVE-2021-41773"},
                {"priority": "urgent"},
                "human",
                trust_status="trusted",
                confirmed=True,
            )
            group_store.save_group(
                CandidateGroup(
                    group_id="GRP-asset-exposure-001",
                    relationship_type="asset_requires_action_for_vulnerability",
                    signature=signature,
                    status="resolved",
                    thesis_text="prod-web-01 requires action for CVE-2021-41773",
                    thesis_facts={
                        "asset_id": "prod-web-01",
                        "vulnerability_id": "CVE-2021-41773",
                    },
                    analysis_state={"priority": "urgent"},
                    integrations_used=["inventory"],
                    proposed_by="agent",
                    member_count=1,
                    review_priority="review",
                    source_workflow_name="propose_asset_exposure",
                    source_workflow_receipt_id=workflow_receipt_id,
                    source_trace_ids=[trace_id],
                    source_step_ids=["assess_exposure"],
                    resolution_id=resolution_id,
                    created_at=created_at,
                )
            )
            group_store.save_members(
                "GRP-asset-exposure-001",
                [
                    CandidateMember(
                        from_type="Asset",
                        from_id="prod-web-01",
                        to_type="Vulnerability",
                        to_id="CVE-2021-41773",
                        relationship_type="asset_requires_action_for_vulnerability",
                        properties={"priority": "urgent"},
                    )
                ],
            )
            group_store.save_group(
                CandidateGroup(
                    group_id="GRP-service-review-001",
                    relationship_type="service_impacted_by_vulnerability",
                    signature=compute_group_signature(
                        "service_impacted_by_vulnerability",
                        {"service_id": "svc-billing", "vulnerability_id": "CVE-2021-41773"},
                    ),
                    status="pending_review",
                    thesis_text="Billing is impacted through prod-web-01",
                    thesis_facts={
                        "service_id": "svc-billing",
                        "vulnerability_id": "CVE-2021-41773",
                    },
                    analysis_state={"priority": "review"},
                    integrations_used=["inventory"],
                    proposed_by="agent",
                    member_count=1,
                    review_priority="review",
                    source_workflow_name="propose_asset_exposure",
                    source_workflow_receipt_id=workflow_receipt_id,
                    source_trace_ids=[trace_id],
                    source_step_ids=["assess_exposure"],
                    created_at=created_at,
                )
            )
            group_store.save_members(
                "GRP-service-review-001",
                [
                    CandidateMember(
                        from_type="BusinessService",
                        from_id="svc-billing",
                        to_type="Vulnerability",
                        to_id="CVE-2021-41773",
                        relationship_type="service_impacted_by_vulnerability",
                    )
                ],
            )
    finally:
        group_store.close()

    feedback_store = instance.get_feedback_store()
    try:
        feedback_store.save_feedback(
            FeedbackRecord(
                receipt_id=workflow_receipt_id,
                action="approve",
                target=RelationshipInstance(
                    from_type="Asset",
                    from_id="prod-web-01",
                    relationship_type="asset_requires_action_for_vulnerability",
                    to_type="Vulnerability",
                    to_id="CVE-2021-41773",
                ),
                reason="Validated during patch planning",
                reason_code="confirmed_by_review",
                decision_context={
                    "surface_type": "workflow",
                    "surface_name": "propose_asset_exposure",
                },
            )
        )
    finally:
        feedback_store.close()

    return resolution_id


def _seed_outcomes(
    instance: CruxibleInstance,
    query_receipt_id: str,
    workflow_receipt_id: str,
    resolution_id: str,
    trace_id: str,
) -> None:
    feedback_store = instance.get_feedback_store()
    try:
        feedback_store.save_outcome(
            OutcomeRecord(
                receipt_id=query_receipt_id,
                anchor_type="receipt",
                outcome="correct",
                outcome_code="validated_in_triage",
                decision_context={
                    "surface_type": "query",
                    "surface_name": "assets_for_vulnerability",
                    "operation_type": "query",
                },
                lineage_snapshot={
                    "receipt": {"receipt_id": query_receipt_id, "operation_type": "query"},
                    "surface": {"type": "query", "name": "assets_for_vulnerability"},
                    "trace_set": {"trace_ids": [], "provider_names": [], "trace_count": 0},
                },
                detail={"note": "asset association held up during review"},
            )
        )
        feedback_store.save_outcome(
            OutcomeRecord(
                receipt_id=workflow_receipt_id,
                anchor_type="resolution",
                anchor_id=resolution_id,
                outcome="partial",
                outcome_code="scope_narrowed",
                decision_context={
                    "surface_type": "workflow",
                    "surface_name": "propose_asset_exposure",
                    "operation_type": "workflow",
                },
                lineage_snapshot={
                    "receipt": {
                        "receipt_id": workflow_receipt_id,
                        "operation_type": "workflow",
                    },
                    "surface": {"type": "workflow", "name": "propose_asset_exposure"},
                    "trace_set": {
                        "trace_ids": [trace_id],
                        "provider_names": ["assess_asset_exposure"],
                        "trace_count": 1,
                    },
                },
                relationship_type="asset_requires_action_for_vulnerability",
                detail={"note": "service impact expanded after later review"},
            )
        )
    finally:
        feedback_store.close()


def test_render_wiki_builds_subject_and_evidence_pages(tmp_path: Path) -> None:
    project = tmp_path / "wiki-project"
    project.mkdir()
    (project / "config.yaml").write_text(WIKI_TEST_CONFIG)
    instance = CruxibleInstance.init(project, "config.yaml")
    instance.save_graph(_build_test_graph())

    query_receipt_id, workflow_receipt_id, trace_id = _seed_receipts_and_traces(instance)
    resolution_id = _seed_review_history(instance, workflow_receipt_id, trace_id)
    _seed_outcomes(instance, query_receipt_id, workflow_receipt_id, resolution_id, trace_id)
    manual_subject = project / "wiki" / "manual" / "subjects" / "asset" / "prod-web-01.md"
    manual_subject.parent.mkdir(parents=True)
    manual_subject.write_text("Operator-maintained runbook note.")

    # Avoid relying on CLI cwd resolution; render directly against the seeded instance.
    written = render_wiki(
        instance,
        WikiOptions(
            output_dir=project / "wiki",
            focus=(SubjectRef("Asset", "prod-web-01"),),
        ),
    )

    assert written

    subject_page = project / "wiki" / "subjects" / "asset" / "prod-web-01.md"
    receipt_page = project / "wiki" / "evidence" / "receipts" / f"{query_receipt_id.lower()}.md"
    workflow_receipt_page = (
        project / "wiki" / "evidence" / "receipts" / f"{workflow_receipt_id.lower()}.md"
    )
    trace_dir = project / "wiki" / "evidence" / "traces"
    workflow_reference = project / "wiki" / "reference" / "workflows" / "propose-asset-exposure.md"
    provider_reference = project / "wiki" / "reference" / "providers" / "assess-asset-exposure.md"

    assert subject_page.exists()
    assert receipt_page.exists()
    assert not trace_dir.exists(), "Trace pages should no longer be generated"
    assert workflow_reference.exists()
    assert provider_reference.exists()

    subject_text = subject_page.read_text()
    receipt_text = receipt_page.read_text()
    workflow_receipt_text = workflow_receipt_page.read_text()
    workflow_reference_text = workflow_reference.read_text()

    assert "## How This Was Produced" in subject_text
    assert "### Workflow: [propose_asset_exposure]" in subject_text
    assert "### Query: [assets_for_vulnerability]" in subject_text
    assert "load_software_inventory" in subject_text
    assert "assess_asset_exposure" in subject_text
    assert subject_text.startswith("# prod-web-01\n\n## Info")
    assert "## Record" not in subject_text
    assert subject_text.index("## Info") < subject_text.index("## Graph Position")
    assert subject_text.index("## Graph Position") < subject_text.index("## At a Glance")
    assert "## At a Glance" in subject_text
    assert "## Relationship Summary" not in subject_text
    assert "## Conclusions Recorded" not in subject_text
    assert "## Outcome History" in subject_text
    assert "subgraph type_Vulnerability" in subject_text
    assert (
        'subject_Asset_prod_web_01 -. "Affected By / Requires Action For" .-> '
        "subject_Vulnerability_CVE_2021_41773"
    ) in subject_text
    assert (
        "- [Apache HTTP Server 2.4.49 path traversal]"
        "(../vulnerability/cve-2021-41773.md)\n"
        "  - Asset Affected By Vulnerability:"
    ) in subject_text
    assert "  - Asset Requires Action For Vulnerability:" in subject_text
    assert "validated_in_triage" in subject_text
    assert "scope_narrowed" in subject_text
    assert "## Full Evidence" in subject_text
    assert "../../evidence/receipts/" in subject_text
    assert "## Maintainer Notes" in subject_text
    assert "Operator-maintained runbook note." in subject_text
    assert "This generated page may be overwritten" in subject_text

    assert "## Scope" in receipt_text
    assert "## Workflow Steps" not in receipt_text
    assert "## Recorded Execution Steps" in workflow_receipt_text
    assert "Traversals" not in receipt_text
    assert "# Workflow: Propose Asset Exposure" in workflow_reference_text
    assert "## Role" in workflow_reference_text
    assert "- Read or compute workflow" in workflow_reference_text
    assert "## Input Context" in workflow_reference_text
    assert "## Result" in workflow_reference_text
    assert "- Result of step `exposure_output`" in workflow_reference_text
    assert "## Provider Sources" in workflow_reference_text
    assert "## Recent Executions" in workflow_reference_text
    assert "## Configured Step Details" in workflow_reference_text
    _assert_no_broken_wiki_links(project / "wiki")


def test_humanize_splits_camel_case_entity_names() -> None:
    assert _humanize("BusinessService") == "Business Service"
    assert _humanize("CompensatingControl") == "Compensating Control"
    assert _humanize("already_snake") == "Already Snake"
    assert _humanize("kebab-case") == "Kebab Case"
    assert _humanize("") == ""


def test_subject_page_heading_uses_stable_id_not_display_label(tmp_path: Path) -> None:
    project = tmp_path / "wiki-project"
    project.mkdir()
    (project / "config.yaml").write_text(WIKI_TEST_CONFIG)
    instance = CruxibleInstance.init(project, "config.yaml")
    graph = EntityGraph()
    graph.add_entity(
        EntityInstance(
            entity_type="Asset",
            entity_id="ASSET-99",
            properties={
                "asset_id": "ASSET-99",
                "name": "Pretty Asset Name",
                "environment": "production",
                "internet_exposed": False,
                "criticality": "low",
            },
        )
    )
    instance.save_graph(graph)

    render_wiki(
        instance,
        WikiOptions(
            output_dir=project / "wiki",
            focus=(SubjectRef("Asset", "ASSET-99"),),
        ),
    )

    subject_text = (project / "wiki" / "subjects" / "asset" / "asset-99.md").read_text()
    assert subject_text.startswith("# ASSET-99\n\n## Info")
    assert "- name: Pretty Asset Name" in subject_text


def test_workflow_reference_explains_apply_steps(tmp_path: Path) -> None:
    project = tmp_path / "canonical-workflow-wiki-project"
    project.mkdir()
    (project / "config.yaml").write_text(
        """\
version: "1.0"
name: canonical_workflow_wiki
kind: world_model

contracts:
  empty_input:
    fields: {}

entity_types:
  Asset:
    properties:
      asset_id:
        type: string
        primary_key: true

workflows:
  canonical_load:
    canonical: true
    contract_in: empty_input
    returns: apply_assets
    steps:
      - id: assets
        make_entities:
          entity_type: Asset
          items: []
          entity_id: $item.asset_id
          properties:
            asset_id: $item.asset_id
        as: assets
      - id: apply_assets
        apply_entities:
          entities_from: assets
        as: apply_assets
"""
    )
    instance = CruxibleInstance.init(project, "config.yaml")

    render_wiki(instance, WikiOptions(output_dir=project / "wiki", scope="all"))

    workflow_text = (
        project / "wiki" / "reference" / "workflows" / "canonical-load.md"
    ).read_text()
    assert "Apply steps commit previously built records or links" in workflow_text
    assert (
        "| apply_assets | Apply records to world model | assets | apply_assets |"
        in workflow_text
    )


def test_render_wiki_local_scope_projects_layered_world(tmp_path: Path) -> None:
    project = tmp_path / "layered-wiki-project"
    instance = _write_layered_wiki_project(project)
    instance.save_graph(_build_layered_graph())
    _seed_reference_only_receipt(instance)

    written = render_wiki(
        instance,
        WikiOptions(output_dir=project / "wiki", scope="local"),
    )

    assert written
    local_page = project / "wiki" / "subjects" / "localsubject" / "local-1.md"
    reference_page = project / "wiki" / "subjects" / "referenceitem" / "ref-1.md"
    receipt_only_reference = project / "wiki" / "subjects" / "referenceitem" / "ref-2.md"
    parent_page = project / "wiki" / "subjects" / "referenceparent" / "parent-1.md"

    assert local_page.exists()
    assert reference_page.exists()
    assert not receipt_only_reference.exists()
    assert not parent_page.exists()

    index_text = (project / "wiki" / "index.md").read_text()
    assert "- Wiki scope: local" in index_text
    assert "- Rendered local subjects: 1" in index_text
    assert "- Rendered upstream subjects: 1" in index_text
    assert "- Omitted upstream subjects: 2" in index_text

    local_text = local_page.read_text()
    assert "## Graph Position" in local_text
    assert "**Diagram legend:**" in local_text
    assert "Amber double rectangle = Upstream/reference neighbor" in local_text
    assert 'subgraph type_ReferenceItem["Reference Item (1)"]' in local_text
    assert "class subject_ReferenceItem_ref_1 upstreamNeighbor" in local_text
    assert "-. \"Local Subject References Item\" .->" in local_text
    assert "Reference item from the upstream catalog." in reference_page.read_text()

    reference_text = reference_page.read_text()
    assert "Parent One" in reference_text
    assert "referenceparent/parent-1.md" not in reference_text

    assert (project / "wiki" / "reference" / "queries" / "local-review.md").exists()
    assert (project / "wiki" / "reference" / "queries" / "reference-lookup.md").exists()
    assert (project / "wiki" / "reference" / "workflows" / "local-workflow.md").exists()
    assert (project / "wiki" / "reference" / "providers" / "local-provider.md").exists()
    assert not (project / "wiki" / "reference" / "workflows" / "upstream-workflow.md").exists()
    assert not (project / "wiki" / "reference" / "providers" / "upstream-provider.md").exists()


def test_render_wiki_evidence_scope_preserves_receipt_seed_behavior(tmp_path: Path) -> None:
    project = tmp_path / "layered-wiki-project"
    instance = _write_layered_wiki_project(project)
    instance.save_graph(_build_layered_graph())
    _seed_reference_only_receipt(instance)

    render_wiki(
        instance,
        WikiOptions(output_dir=project / "wiki", scope="evidence"),
    )

    assert (project / "wiki" / "subjects" / "referenceitem" / "ref-2.md").exists()


def test_render_wiki_focus_does_not_emit_scope_wide_reference_pages(tmp_path: Path) -> None:
    project = tmp_path / "layered-wiki-project"
    instance = _write_layered_wiki_project(project)
    instance.save_graph(_build_layered_graph())

    render_wiki(
        instance,
        WikiOptions(
            output_dir=project / "wiki",
            focus=(SubjectRef("LocalSubject", "local-1"),),
            scope="all",
        ),
    )

    assert (project / "wiki" / "subjects" / "localsubject" / "local-1.md").exists()
    assert (project / "wiki" / "subjects" / "referenceitem" / "ref-1.md").exists()
    assert not (project / "wiki" / "reference" / "queries" / "local-review.md").exists()
    assert not (project / "wiki" / "reference" / "workflows" / "local-workflow.md").exists()
    assert not (project / "wiki" / "reference" / "workflows" / "upstream-workflow.md").exists()


def test_render_wiki_pending_review_filters_members_for_current_subject(tmp_path: Path) -> None:
    project = tmp_path / "wiki-project"
    project.mkdir()
    (project / "config.yaml").write_text(WIKI_TEST_CONFIG)
    instance = CruxibleInstance.init(project, "config.yaml")
    graph = _build_test_graph()
    graph.add_entity(
        EntityInstance(
            entity_type="Asset",
            entity_id="corp-laptop-01",
            properties={
                "asset_id": "corp-laptop-01",
                "name": "corp-laptop-01",
                "environment": "corporate",
                "internet_exposed": False,
                "criticality": "low",
            },
        )
    )
    instance.save_graph(graph)

    _, workflow_receipt_id, trace_id = _seed_receipts_and_traces(instance)
    _seed_review_history(instance, workflow_receipt_id, trace_id)

    group_store = instance.get_group_store()
    try:
        created_at = datetime.now(timezone.utc)
        with group_store.transaction():
            group_store.save_group(
                CandidateGroup(
                    group_id="GRP-mixed-asset-review-001",
                    relationship_type="asset_requires_action_for_vulnerability",
                    signature=compute_group_signature(
                        "asset_requires_action_for_vulnerability",
                        {"vulnerability_id": "CVE-2021-41773", "subject": "mixed-assets"},
                    ),
                    status="pending_review",
                    thesis_text=(
                        "Mixed review group should only render relevant members per subject"
                    ),
                    thesis_facts={"vulnerability_id": "CVE-2021-41773"},
                    analysis_state={"priority": "review"},
                    integrations_used=["inventory"],
                    proposed_by="agent",
                    member_count=2,
                    review_priority="review",
                    source_workflow_name="propose_asset_exposure",
                    source_workflow_receipt_id=workflow_receipt_id,
                    source_trace_ids=[trace_id],
                    source_step_ids=["assess_exposure"],
                    created_at=created_at,
                )
            )
            group_store.save_members(
                "GRP-mixed-asset-review-001",
                [
                    CandidateMember(
                        from_type="Asset",
                        from_id="prod-web-01",
                        to_type="Vulnerability",
                        to_id="CVE-2021-41773",
                        relationship_type="asset_requires_action_for_vulnerability",
                    ),
                    CandidateMember(
                        from_type="Asset",
                        from_id="corp-laptop-01",
                        to_type="Vulnerability",
                        to_id="CVE-2021-41773",
                        relationship_type="asset_requires_action_for_vulnerability",
                    ),
                ],
            )
    finally:
        group_store.close()

    written = render_wiki(
        instance,
        WikiOptions(
            output_dir=project / "wiki",
            focus=(SubjectRef("Asset", "prod-web-01"),),
        ),
    )

    assert written
    subject_text = (project / "wiki" / "subjects" / "asset" / "prod-web-01.md").read_text()
    assert "prod-web-01" in subject_text
    assert "Mixed review group should only render relevant members per subject" in subject_text
    assert "corp-laptop-01" not in subject_text
